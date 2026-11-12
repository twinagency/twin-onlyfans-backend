"""Convert an explicitly submitted browser capture inside the existing worker.

Reuses the legacy signer's pure derivation. No legacy session files, registry,
activation, executable browser code or arbitrary URLs. The one provider I/O is
`fetch_asset` (also used API-side by the curl/Browserbase capture build): two fixed
hosts, through the account's proxy, no redirects/env proxies, byte-capped.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from of_signer import derive_rules_from_chunk, sign
from .provider_connector import NativeProviderConnector, _header, _deadline, session_from_capture
from .store import Refused

SCHEMA = "of-browser.v1"


def validate_browser_payload(payload, *, fresh=True):
    try:
        if type(payload) is not dict or set(payload) not in ({"ownerId", "browser", "expiresAt"}, {"ownerId", "browser", "expiresAt", "networkFingerprint"}):
            raise ValueError()
        if 'networkFingerprint' in payload and (type(payload['networkFingerprint']) is not str or not re.fullmatch(r'[a-f0-9]{64}',payload['networkFingerprint'])):
            raise ValueError()
        owner = payload["ownerId"]
        if type(owner) is not int or not 0 < owner <= 9007199254740991:
            raise ValueError()
        expiry = datetime.fromisoformat(payload["expiresAt"].replace("Z", "+00:00"))
        _deadline(expiry)
        b = payload["browser"]
        if (type(b) is not dict or set(b) != {"schemaVersion", "headers", "cookies", "staticParam", "samples"}
                or b["schemaVersion"] != SCHEMA or len(json.dumps(b).encode()) > 98304):
            raise ValueError()
        h = b["headers"]
        if (type(h) is not dict or set(h) != {"user_id", "user_agent", "x_bc", "x_of_rev", "x_hash"}
                or h["user_id"] != str(owner) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", h["x_of_rev"])):
            raise ValueError()
        for name in ("user_agent", "x_bc", "x_of_rev"):
            _header(h[name], name)
        if h["x_hash"] is not None:
            _header(h["x_hash"], "hash")
        _header(b["staticParam"], "static parameter")
        rows = b["cookies"]
        if type(rows) is not list or not 1 <= len(rows) <= 64:
            raise ValueError()
        names, size = set(), 0
        for row in rows:
            if (type(row) is not dict or set(row) != {"name", "value", "domain", "path"}
                    or type(row["name"]) is not str or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", row["name"])
                    or row["name"] in names or row["domain"] not in {"onlyfans.com", ".onlyfans.com"} or row["path"] != "/"):
                raise ValueError()
            value = _header(row["value"], "cookie", 8192)
            if any(c in value for c in ';,"\\') or any(c.isspace() for c in value):
                raise ValueError()
            names.add(row["name"])
            size += len(row["name"]) + len(value) + 3
        if size > 16386 or "auth_id" not in names or next(r["value"] for r in rows if r["name"] == "auth_id") != str(owner):
            raise ValueError()
        samples = b["samples"]
        if type(samples) is not list or not 2 <= len(samples) <= 4:
            raise ValueError()
        seen, prefixes = set(), set()
        now = datetime.now(timezone.utc).timestamp() * 1000
        for sample in samples:
            if type(sample) is not dict or set(sample) != {"url", "sign", "time"}:
                raise ValueError()
            url = _header(sample["url"], "sample URL", 2048)
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.netloc != "onlyfans.com" or parsed.fragment
                    or not parsed.path.startswith("/api2/v2/") or "\\" in url or ".." in parsed.path.split("/")):
                raise ValueError()
            if type(sample["time"]) is not str or not re.fullmatch(r"[0-9]{13}", sample["time"]):
                raise ValueError()
            if fresh and not -60000 <= now - int(sample["time"]) <= 15 * 60 * 1000:
                raise ValueError()
            parts = _header(sample["sign"], "sample signature").split(":")
            if (len(parts) != 4 or not parts[0] or not parts[3] or not re.fullmatch(r"[a-f0-9]{40}", parts[1])
                    or not re.fullmatch(r"[a-f0-9]{1,8}", parts[2]) or parts[1] in seen):
                raise ValueError()
            seen.add(parts[1]); prefixes.add((parts[0], parts[3]))
        if len(prefixes) != 1:
            raise ValueError()
        return b
    except Exception:
        raise Refused("invalid browser connection capture") from None


def fetch_asset(kind, *, owner, rev, user_agent, proxy):
    """Fixed provider GETs only, through `proxy`; no session cookies, env proxies, redirects, retries or logs."""
    from curl_cffi import requests
    if kind == "rules":
        url, maximum = f"https://static2.onlyfans.com/static/prod/f/{rev}/2313.js", 2 * 1024 * 1024
    elif kind == "hash":
        url, maximum = f"https://cdn2.onlyfans.com/hash/?u={owner}", 4096
    else:
        raise Refused("unsupported connection asset")
    chunks, size = [], 0
    def receive(chunk):
        nonlocal size
        size += len(chunk)
        if size > maximum:
            raise Refused("connection asset exceeded its budget")
        chunks.append(chunk)
        return len(chunk)
    try:
        with requests.Session(impersonate="chrome", trust_env=False, retry=0) as client:
            result = client.get(url, headers={"User-Agent": user_agent, "Referer": "https://onlyfans.com/", "Accept-Encoding": "identity"},
                proxy=proxy, timeout=8, allow_redirects=False, verify=True, accept_encoding="identity", content_callback=receive, discard_cookies=True)
            if result.status_code != 200 or result.headers.get("content-encoding", "identity").lower().strip() != "identity":
                raise ValueError()
            return b"".join(chunks).decode("utf-8", errors="strict")
    except Exception:
        raise Refused("connection asset unavailable") from None


def normalize_browser_payload(payload, *, proxy):
    NativeProviderConnector._ready()
    b = validate_browser_payload(payload, fresh=False)
    h, owner = b["headers"], payload["ownerId"]
    code = fetch_asset("rules", owner=owner, rev=h["x_of_rev"], user_agent=h["user_agent"], proxy=proxy)
    samples = [{"sha1Hex": s["sign"].split(":")[1], "checksumValue": int(s["sign"].split(":")[2], 16)} for s in b["samples"]]
    derived = derive_rules_from_chunk(code, samples)
    if not derived:
        raise Refused("browser signing rules unavailable")
    first = b["samples"][0]["sign"].split(":")
    rules = {"static_param": b["staticParam"], "start": first[0], "end": first[3],
        "checksum_indexes": derived["checksum_indexes"], "checksum_constant": derived["checksum_constant"]}
    # Check the complete digest too: the legacy verifier checks the checksum only.
    if any(sign(s["url"], owner, rules, int(s["time"]))["sign"] != s["sign"] for s in b["samples"]):
        raise Refused("browser signing proof does not match")
    x_hash = h["x_hash"] or fetch_asset("hash", owner=owner, rev=h["x_of_rev"], user_agent=h["user_agent"], proxy=proxy).strip()
    capture = {"headers": {k: h[k] for k in ("user_id", "user_agent", "x_bc", "x_of_rev")},
        "cookies": b["cookies"], "signing": {"rules": rules}}
    normalized = {"ownerId": owner, "capture": capture, "xHash": x_hash, "expiresAt": payload["expiresAt"]}
    session_from_capture(account_id="of-" + str(owner), owner_id=owner, policy_revision=1, capture=capture,
        x_hash=x_hash, proxy_url=proxy, expires_at=datetime.fromisoformat(payload["expiresAt"].replace("Z", "+00:00")))
    return normalized
