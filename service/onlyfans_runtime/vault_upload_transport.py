"""Account-scoped native adapter over Fastt's existing upload protocol methods.

No OFClient is constructed. Only a worker with a current durable upload claim
can obtain this adapter. Fan messages, immediate posts and unrelated deletes
have no route here. Presigned targets are admitted from signed/create only.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit, unquote

from of_client import OFClient, OFAPIError, API_BASE, VAULT_MARKER
from .provider_connector import NativeProviderConnector
from .store import Refused, _id


class _Reply:
    def __init__(self, status, raw=b"{}", headers=None):
        self.status_code, self._raw = status, raw
        self.headers = {str(key).lower(): value for key, value in (headers or {}).items()}
        self.url = "/native-vault-upload"
        self.ok = 200 <= status < 300
        self.text = "Provider upload request refused" if not self.ok else ""

    def json(self):
        return json.loads(self._raw)


class _UploadHTTP:
    proxies = {}

    def __init__(self, owner):
        self.owner = owner

    def close(self):
        pass

    def put(self, url, *, data, headers, timeout):
        return self.owner._external("PUT", url, data=data, headers=headers, timeout=timeout)

    def post(self, url, *, multipart, headers, timeout):
        return self.owner._external("POST", url, multipart=multipart, headers=headers, timeout=timeout)


class NativeVaultUpload:
    # These are the SAME protocol functions, not a copied uploader or a legacy
    # client instance. The two injected multipart hooks keep egress scoped.
    vault_media_lookup_hash = OFClient.vault_media_lookup_hash
    request_signed_upload = OFClient.request_signed_upload
    finish_signed_upload = OFClient.finish_signed_upload
    convert_register = OFClient.convert_register
    _upload_args = OFClient._upload_args
    _send_with = staticmethod(OFClient._send_with)
    upload_media = OFClient.upload_media
    materialize_to_vault = OFClient.materialize_to_vault
    _claim_uploaded = OFClient._claim_uploaded
    upload_to_vault = OFClient.upload_to_vault

    def __init__(self, session, *, authorize, job_id, known_posts=()):
        if not callable(authorize) or not re.fullmatch(r"[a-f0-9-]{36}", job_id):
            raise Refused("upload authority required")
        self.transport = NativeProviderConnector(session)
        self.authorize = authorize
        self.user_id, self.user_agent = session.owner_id, session.user_agent
        self.timeout_s, self.http = 10, _UploadHTTP(self)
        self._VAULT_MARKER = VAULT_MARKER + " [native:" + job_id + "]"
        self._posts = {str(_id(int(value))) for value in known_posts}
        self._puts = set()
        self._keys = set()

    def _require_upload_transport(self):
        self.transport._ready()
        self.transport._handle.current()
        self.authorize()

    def _upload_part_session(self):
        self._require_upload_transport()
        return _UploadHTTP(self)

    def _proxy_retry(self, thunk, *, what):
        # The common protocol already owns bounded idempotent PUT retries.
        # A carrier with an uncertain outcome is never blindly repeated here.
        self._require_upload_transport()
        return thunk()

    @staticmethod
    def _s3(url, key):
        try:
            target = urlsplit(url)
            return (type(url) is str and len(url) <= 8192 and url.isascii()
                and target.scheme == "https" and target.hostname == "of2transcoder.s3-accelerate.amazonaws.com"
                and target.port in {None, 443} and not target.username and not target.password
                and not target.fragment and unquote(target.path) == "/" + key
                and not any(ord(c) < 33 or ord(c) == 127 for c in url) and "\\" not in url)
        except (TypeError, ValueError):
            return False

    def _api(self, method, url, *, params=None, json_body=None):
        self._require_upload_transport()
        if type(url) is not str or not url.startswith(API_BASE + "/"):
            raise Refused("unsupported upload API target")
        path = url[len("https://onlyfans.com"):]
        if urlsplit(path).query or urlsplit(path).fragment:
            raise Refused("unsupported upload API query")
        route = path[len("/api2/v2"):]
        allowed = False
        if method == "GET":
            allowed = (route in {"/users/me", "/vault/media/hash", "/schedules/later/post"}
                or bool(re.fullmatch(r"/posts/[1-9][0-9]*", route)) and route.rsplit("/", 1)[1] in self._posts)
        elif method == "POST":
            allowed = route in {"/upload/signed/create", "/upload/signed/finish"}
            if route == "/posts":
                value = json_body
                try:
                    date = datetime.fromisoformat(value["postedAt"].replace("Z", "+00:00"))
                    allowed = (set(value) == {"text", "mediaFiles", "price", "postedAt"}
                        and value["text"] == self._VAULT_MARKER and value["price"] == 0
                        and type(value["mediaFiles"]) is list and len(value["mediaFiles"]) == 1
                        and datetime.now(timezone.utc) + timedelta(days=28) < date
                        < datetime.now(timezone.utc) + timedelta(days=32))
                except (KeyError, TypeError, ValueError):
                    allowed = False
        elif method == "DELETE":
            allowed = bool(re.fullmatch(r"/posts/[1-9][0-9]*", route)) and route.rsplit("/", 1)[1] in self._posts
        if not allowed:
            raise Refused("unsupported upload API operation")
        if method == "POST" and route == "/upload/signed/create":
            if not isinstance(json_body, dict) or not re.fullmatch(r"upload/[^/]+/[^/]+/[^/]+", json_body.get("key", "")):
                raise Refused("unsupported upload key")
        if method == "POST" and route == "/upload/signed/finish" and json_body.get("key") not in self._keys:
            raise Refused("unrecorded multipart upload")
        if params:
            path += "?" + urlencode(params)
        raw_body = json.dumps(json_body, separators=(",", ":")).encode() if json_body is not None else b""
        status, raw = self.transport._signed_request(method, path, raw_body,
            max_response_bytes=1024 * 1024, accepted_statuses=(200, 201, 204, 400, 404, 409, 429), with_status=True)
        reply = _Reply(status, raw)
        if not reply.ok:
            message = "Provider upload request refused"
            if status == 400 and b"allow 10 seconds" in raw:
                message = "Please allow 10 seconds"
            raise OFAPIError(message, response=reply)
        value = reply.json()
        if method == "POST" and route == "/upload/signed/create":
            key = json_body.get("key")
            if not isinstance(key, str) or not re.fullmatch(r"upload/[^/]+/[^/]+/[^/]+", key):
                raise Refused("unsupported upload key")
            urls = [entry.get("putUrl") for entry in value.get("keys", [])]
            if value.get("putUrl"):
                urls.append(value["putUrl"])
            if not urls or not all(self._s3(target, key) for target in urls):
                raise Refused("unsupported presigned upload target")
            if value.get("getUrl") and not self._s3(value["getUrl"], key):
                raise Refused("unsupported presigned upload source")
            self._keys.add(key)
            self._puts.update(urls)
        if method == "POST" and route == "/posts":
            self._posts.add(str(_id(value.get("id"))))
        if method == "GET" and route == "/schedules/later/post":
            for post in value.get("list", []) if isinstance(value, dict) else value:
                from vault_upload import _marker_text
                if self._VAULT_MARKER in _marker_text(post):
                    self._posts.add(str(_id(post.get("id"))))
        return reply

    def get(self, url, *, params=None):
        try:
            return self._api("GET", url, params=params)
        except OFAPIError as error:
            return error.response

    def get_json(self, url, **kwargs):
        return self._api("GET", url, **kwargs).json()

    def post_json(self, url, **kwargs):
        return self._api("POST", url, **kwargs).json()

    def me(self):
        self.transport.verify_identity()
        return self.get_json(API_BASE + "/users/me")

    def create_post(self, text, *, media_files, posted_at, auto_tag):
        return self.post_json(API_BASE + "/posts", json_body={"text": text,
            "mediaFiles": media_files, "price": 0, "postedAt": posted_at})

    def get_post(self, post_id):
        return self.get_json(API_BASE + "/posts/" + str(_id(int(post_id))))

    def delete_post(self, post_id):
        return self._api("DELETE", API_BASE + "/posts/" + str(_id(int(post_id)))).json()

    def schedules_later_post(self, *, limit=100, offset=0):
        return self.get_json(API_BASE + "/schedules/later/post", params={"limit": limit, "offset": offset})

    def _external(self, method, url, *, headers, timeout, data=None, multipart=None):
        self._require_upload_transport()
        if method == "PUT":
            if url not in self._puts or type(data) is not bytes or len(data) > 5 * 1024 * 1024:
                raise Refused("unsupported upload body or destination")
            headers = {"Content-Type": headers["Content-Type"]}
        elif method == "POST":
            if url != "https://convert.onlyfans.com/file/upload" or multipart is None:
                raise Refused("unsupported upload conversion")
            headers = {**headers, "User-Agent": self.user_agent}
        else:
            raise Refused("unsupported external upload operation")
        from curl_cffi import requests
        chunks, size = [], 0
        def receive(chunk):
            nonlocal size
            size += len(chunk)
            if size > 1024 * 1024:
                raise Refused("upload response exceeded its budget")
            chunks.append(chunk)
            return len(chunk)
        try:
            with requests.Session(impersonate="chrome", trust_env=False, retry=0) as session:
                response = session.request(method, url, headers=headers,
                    **({"data": data} if method == "PUT" else {"multipart": multipart}),
                    proxy=self.transport._handle.proxy_url, timeout=min(timeout, 300),
                    allow_redirects=False, verify=True, discard_cookies=True,
                    accept_encoding="identity", content_callback=receive)
                return _Reply(response.status_code, b"".join(chunks) or b"{}", dict(response.headers))
        except Exception:
            raise Refused("native upload transport outcome is uncertain") from None
