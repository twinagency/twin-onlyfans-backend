"""Vault page rejections carry a fixed reason code and never provider content. Synthetic data, no I/O."""
import copy
import json
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.provider_connector import NativeReadTransientError
from onlyfans_runtime.vault_management import parse_vault_page, vault_failure_reason, _log_vault_reject

OWNER = 7001
SECRET = "SECRET-CAPTION-TOKEN"


def media(mid=1, **changes):
    row = {"id": mid, "type": "photo", "createdAt": "2026-10-21T00:00:00Z", "isReady": True,
           "width": 640, "height": 800, "files": {"full": {"url": "https://cdn.example/f"}}}
    row.update(changes)
    return row


def raw(rows, more=False, **extra):
    return json.dumps({"list": rows, "hasMore": more, **extra}).encode()


def reason_of(payload):
    try:
        parse_vault_page(payload, owner_id=OWNER)
    except NativeProtocolError as problem:
        return vault_failure_reason(problem), getattr(problem, "row", None), str(problem)
    raise AssertionError("page was accepted")


class VaultRejectReasons(unittest.TestCase):
    def test_valid_page_still_accepted(self):
        rows, more = parse_vault_page(raw([media(1), media(2, type="audio", width=None)]), owner_id=OWNER)
        self.assertEqual((len(rows), more), (2, False))

    def test_each_check_reports_its_reason(self):
        cases = {
            "page:type_stream": [media(type="stream")],
            "page:type_other": [media(type=SECRET + "!")],
            "page:createdAt": [media(createdAt=None)],
            "page:updatedAt": [media(updatedAt="nope")],
            "page:flag_isReady": [media(isReady="yes")],
            "page:userId_mismatch": [media(userId=OWNER + 1)],
            "page:user_id_mismatch": [media(user={"id": OWNER + 1})],
            "page:user_not_object": [media(user=5)],
            "page:width": [media(width=0)],
            "page:height": [media(height=1.5)],
            "page:duration": [media(duration=-1)],
            "page:files_not_object": [media(files=[])],
            "page:variant_not_object": [media(files={"full": "x"})],
            "page:url_not_https": [media(files={"full": {"url": "http://cdn.example/x"}})],
            "page:url_not_string": [media(files={"full": {"url": 5}})],
            "page:url_credentials": [media(files={"full": {"url": "https://u:p@cdn.example/x"}})],
            "page:url_fragment": [media(files={"full": {"url": "https://cdn.example/x#f"}})],
            "page:listStates_item": [media(listStates=[1])],
            "page:id_invalid": [media(mid="1")],
            "page:id_duplicate": [media(1), media(1)],
            "page:row_not_object": [5],
        }
        for expected, rows in cases.items():
            with self.subTest(expected):
                code, _, _ = reason_of(raw(rows))
                self.assertEqual(code, expected)

    def test_page_level_reasons_and_row_index(self):
        self.assertEqual(reason_of(raw([media(1), media(2, width=0)]))[:2], ("page:width", 1))
        self.assertEqual(reason_of(raw([media(1)], more=True))[0], "page:hasMore_short_page")
        self.assertEqual(reason_of(raw([media(i) for i in range(1, 26)]))[0], "page:page_over_limit")
        self.assertEqual(reason_of(json.dumps({"list": []}).encode())[0], "page:missing_list_or_hasMore")
        self.assertEqual(reason_of(raw({}, False))[0], "page:list_not_array")
        self.assertEqual(reason_of(json.dumps({"list": [], "hasMore": 1}).encode())[0], "page:hasMore_not_bool")

    def test_load_failures(self):
        self.assertEqual(reason_of(b"not json")[0], "page:load_json")
        self.assertEqual(reason_of(b'{"list":[],"list":[],"hasMore":false}')[0], "page:load_dup_key")
        self.assertEqual(reason_of(b"[]")[0], "page:load_not_object")
        self.assertEqual(reason_of(b" " * (1024 * 1024 + 1))[0], "page:load_size")

    def test_owner_mismatch_stays_strict(self):
        with self.assertRaises(NativeProtocolError):
            parse_vault_page(raw([media(userId=OWNER + 1)]), owner_id=OWNER)
        with self.assertRaises(NativeProtocolError):
            parse_vault_page(raw([media(user={"id": OWNER + 1})]), owner_id=OWNER)

    def test_reason_never_contains_provider_content(self):
        payload = raw([media(caption=SECRET, type=SECRET, files={"full": {"url": "http://x/" + SECRET}})])
        code, _, message = reason_of(payload)
        self.assertNotIn(SECRET, code + message)
        self.assertLessEqual(len(("unsupported_vault_response:" + code)), 64)

    def test_transport_reasons_use_fixed_labels(self):
        err = NativeProtocolError("native HTTPS response exceeded its byte budget"); err.stage = "identity"
        self.assertEqual(vault_failure_reason(err), "identity:bytes")
        err = NativeProtocolError("native HTTPS status is not delivery evidence"); err.stage = "page"
        err.read_failure = {"httpStatus": 404}
        self.assertEqual(vault_failure_reason(err), "page:http_404")
        err = NativeProtocolError("native HTTPS response must be JSON")
        self.assertEqual(vault_failure_reason(err), "read:not_json")
        self.assertEqual(vault_failure_reason(NativeProtocolError("cookie=" + SECRET)), "read:other")

    def test_log_line_is_bounded_and_clean(self):
        err = NativeProtocolError("x"); err.reason, err.row = "width", 3
        with self.assertLogs("onlyfans_runtime.vault_management", level="WARNING") as logs:
            _log_vault_reject("of-123", 48, err, "page:width")
        line = logs.output[0]
        self.assertIn('"reason": "page:width"', line)
        self.assertIn('"row": 3', line)
        with self.assertLogs("onlyfans_runtime.vault_management", level="WARNING") as logs:
            _log_vault_reject("bad account " + SECRET, 0, err, "page:width")
        self.assertNotIn(SECRET, logs.output[0])


if __name__ == "__main__":
    unittest.main()
