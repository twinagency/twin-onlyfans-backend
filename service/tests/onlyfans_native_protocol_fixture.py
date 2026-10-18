"""Owned, synthetic native-client dialect. Never contacts the real platform."""
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

from of_signer import sign

OWNER = 8001


def native_message(identity=1, *, fan=101, owner=OWNER, outgoing=False,
                   text="<p>Synthetic inbound</p>", media=(), price=0):
    return {"id": identity, "fromUser": {"id": owner if outgoing else fan},
            "toUser": {"id": fan if outgoing else owner}, "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat(), "media": [{"id": value} for value in media],
            "mediaCount": len(media), "price": price, "responseType": "message",
            "lockedText": False, "isTip": False, "isFromQueue": False, "isOpened": False}


class NativePeer:
    def __init__(self, *, history=None, page_size=100, mode="accepted", on_request=None, mutate=None):
        self.history = {fan: list(rows) for fan, rows in (history or {101: [native_message()]}).items()}
        self.page_size, self.mode, self.on_request, self.mutate = page_size, mode, on_request, mutate
        self.records, self.lock = [], threading.Lock()
        peer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond("GET")

            def do_POST(self):
                self.respond("POST")

            def respond(self, method):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = {key.lower(): value for key, value in self.headers.items()}
                parsed = urlsplit(self.path); query = parse_qs(parsed.query)
                record = {"method": method, "path": self.path, "headers": headers, "raw": raw}
                if method == "POST":
                    record["json"] = json.loads(raw)
                with peer.lock:
                    peer.records.append(record)
                    count = sum(row["method"] == "POST" for row in peer.records)
                if peer.on_request:
                    peer.on_request(record)
                rules = {"static_param": "twin-local-fixture-only", "start": "fixture", "end": "offline",
                         "checksum_indexes": list(range(32)), "checksum_constant": 17}
                expected = sign(self.path, OWNER, rules, time_ms=int(headers.get("time", "0")))
                assert headers.get("sign") == expected["sign"] and headers.get("user-id") == str(OWNER)
                assert "cookie" not in headers and "authorization" not in headers
                status = 200
                if parsed.path == "/api2/v2/users/me":
                    value = {"id": OWNER + int(peer.mode == "wrong_owner")}
                elif parsed.path == "/api2/v2/chats":
                    offset = int(query["offset"][0]); size = min(int(query["limit"][0]), peer.page_size)
                    fans = sorted(peer.history)
                    value = {"list": [{"withUser": {"id": fan}} for fan in fans[offset:offset+size]],
                             "hasMore": len(fans) > offset + size}
                else:
                    fan = int(parsed.path.split("/")[4])
                    if method == "GET":
                        size = min(int(query["limit"][0]), peer.page_size)
                        before = int(query.get("id", [str(2**63-1)])[0])
                        rows = sorted((row for row in peer.history.get(fan, []) if row["id"] < before),
                                      key=lambda row: row["id"], reverse=True)
                        value = {"list": rows[:size], "hasMore": len(rows) > size}
                    else:
                        body = record["json"]
                        value = native_message(9000+count, fan=fan, outgoing=True, text=body["text"],
                                               media=body["mediaFiles"], price=body["price"])
                        value["previews"] = body["previews"]
                        peer.history.setdefault(fan, []).append(dict(value))
                if peer.mutate:
                    value = peer.mutate(record, value)
                if method == "POST":
                    mutations = {"wrong_text": ("text", "<p>changed</p>"), "wrong_owner_post": ("fromUser", {"id": 999}),
                                 "wrong_recipient": ("toUser", {"id": 102}), "wrong_price": ("price", "0.01"),
                                 "wrong_media": ("media", [{"id": 99}]), "wrong_previews": ("previews", []),
                                 "locked": ("lockedText", True), "opened": ("isOpened", True)}
                    if peer.mode in mutations:
                        key, item = mutations[peer.mode]; value[key] = item
                    if peer.mode == "missing_previews":
                        value.pop("previews", None)
                    if peer.mode == "missing_id":
                        value.pop("id", None)
                    if peer.mode == "drop":
                        self.close_connection = True
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        self.connection.close(); return
                    if peer.mode == "slow":
                        time.sleep(.4)
                    status = {"redirect": 307, "401": 401, "403": 403, "429": 429, "500": 500}.get(peer.mode, 200)
                body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                if method == "POST" and peer.mode == "duplicate_key":
                    body = b'{"id":true,' + body[1:]
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("X-Twin-Rehearsal-Peer", self.headers.get("X-Twin-Rehearsal-Peer", ""))
                    self.send_header("Connection", "close")
                    self.send_header("Set-Cookie", "must_not_persist=fixture; Path=/")
                    if status == 307:
                        self.send_header("Location", "/should-never-be-followed")
                    self.end_headers(); self.wfile.write(body)
                except (OSError, BrokenPipeError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)

    @property
    def posts(self):
        return [row for row in self.records if row["method"] == "POST"]


@contextmanager
def native_peer(**kwargs):
    peer = NativePeer(**kwargs); peer.thread.start()
    try:
        yield peer
    finally:
        peer.server.shutdown(); peer.server.server_close(); peer.thread.join(2)
