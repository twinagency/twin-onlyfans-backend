"""Vault response limits use synthetic metadata only; no provider or database I/O."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from onlyfans_runtime.native_protocol import MAX_BYTES, NativeProtocolError, load
from onlyfans_runtime.vault_management import parse_vault_page
from onlyfans_runtime.provider_connector import NativeProviderConnector, session_from_capture
from onlyfans_runtime.store import Refused

VAULT_LIMIT = 1024 * 1024
OWNER = 7001

def page():
    url = "https://cdn.onlyfans.com/synthetic?token=" + "x" * 7400
    rows = [{"id": i, "type": "photo", "createdAt": "2026-10-21T00:00:00Z",
             "isReady": True, "files": {"full": {"url": url}, "thumb": {"url": url}}}
            for i in range(1, 25)]
    return json.dumps({"list": rows, "hasMore": True}).encode()

def session():
    capture = {"headers": {"user_id": str(OWNER), "x_bc": "synthetic-bc",
        "x_of_rev": "synthetic-rev", "user_agent": "Synthetic Test"},
        "signing": {"rules": {"static_param": "synthetic", "start": "fixture", "end": "offline",
            "checksum_indexes": list(range(32)), "checksum_constant": 17}},
        "cookies": [{"name": "sess", "value": "synthetic-session", "domain": ".onlyfans.com", "path": "/"}]}
    return session_from_capture(account_id="acc-a", owner_id=OWNER, policy_revision=1,
        capture=capture, x_hash="synthetic-hash", proxy_url="http://fixed.example:8080",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

class TestVaultResponseLimits(unittest.TestCase):
    def test_live_sized_vault_page(self):
        raw = page()
        self.assertGreater(len(raw), 350000)
        rows, more = parse_vault_page(raw, owner_id=OWNER)
        self.assertEqual(len(rows), 24)
        self.assertTrue(more)

    def test_default_message_limit_remains_64_kib(self):
        self.assertEqual(MAX_BYTES, 65536)
        with self.assertRaises(NativeProtocolError):
            load(page())

    def test_extended_json_still_refuses_duplicate_nonfinite_and_bad_utf8(self):
        for raw in [b'{"padding":"' + b"x" * 70000 + b'","a":1,"a":2}',
                    b'{"padding":"' + b"x" * 70000 + b'","a":NaN}', b'{"a":"\xff"}']:
            with self.subTest(size=len(raw)), self.assertRaises(NativeProtocolError):
                load(raw, max_bytes=VAULT_LIMIT)

    def test_vault_cap_and_scope_remain_enforced(self):
        with self.assertRaises(NativeProtocolError):
            parse_vault_page(b" " * (VAULT_LIMIT + 1), owner_id=OWNER)
        value = json.loads(page())
        value["list"][0]["userId"] = OWNER + 1
        with self.assertRaises(NativeProtocolError):
            parse_vault_page(json.dumps(value).encode(), owner_id=OWNER)
        value["list"][0]["userId"] = OWNER
        value["list"][1]["id"] = value["list"][0]["id"]
        with self.assertRaises(NativeProtocolError):
            parse_vault_page(json.dumps(value).encode(), owner_id=OWNER)

    def test_invalid_json_budgets_are_refused(self):
        for limit in [True, 0, -1, VAULT_LIMIT + 1, None]:
            with self.subTest(limit=limit), self.assertRaises(NativeProtocolError):
                load(b"{}", max_bytes=limit)

    def test_wire_cap_is_specific_to_vault_and_keeps_proxy_and_one_attempt(self):
        records = []
        payload = [page()]
        class FakeSession:
            def __init__(self, **kwargs): records.append({"session": kwargs})
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def request(self, method, url, **kwargs):
                records.append({"method": method, "url": url, "options": kwargs})
                kwargs["content_callback"](payload[0])
                return SimpleNamespace(status_code=200, headers={"content-type": "application/json"})
        fake = SimpleNamespace(requests=SimpleNamespace(Session=FakeSession))
        with patch.dict(os.environ, {"ONLYFANS_PROCESS_ROLE": "worker"}), patch.dict(sys.modules, {"curl_cffi": fake}):
            wire = NativeProviderConnector(session())
            verified = []
            with patch.object(wire, "verify_identity", lambda: verified.append(True)):
                self.assertEqual(wire.vault_page(offset=0), payload[0])
            self.assertEqual(verified, [True])
            request = next(row for row in records if "url" in row)
            self.assertEqual(request["method"], "GET")
            self.assertEqual(request["options"]["proxy"], "http://fixed.example:8080")
            self.assertFalse(request["options"]["allow_redirects"])
            self.assertTrue(request["options"]["verify"])
            self.assertEqual(sum("url" in row for row in records), 1)
            records.clear()
            with self.assertRaises(NativeProtocolError):
                wire._call("GET", "/api2/v2/users/me")
            self.assertEqual(sum("url" in row for row in records), 1)
            payload[0] = b"x" * (VAULT_LIMIT + 1)
            with patch.object(wire, "verify_identity", lambda: None), self.assertRaises(NativeProtocolError):
                wire.vault_page(offset=0)

    def test_operator_can_choose_a_smaller_vault_budget(self):
        for limit in [True, 0, VAULT_LIMIT + 1]:
            with self.subTest(limit=limit), self.assertRaises(Refused):
                NativeProviderConnector(session(), max_vault_response_bytes=limit)
        self.assertEqual(NativeProviderConnector(session(), max_vault_response_bytes=100)._vault_max_bytes, 100)

if __name__ == "__main__":
    unittest.main()
