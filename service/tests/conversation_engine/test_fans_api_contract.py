import unittest

from conversation_engine.identity import ActionIdentity, ConversationKey
from conversation_engine.recovery import AttemptEvidence, NextStep, Outcome, decide_recovery
from fans_api_adapter.history import parse_history_page, validate_history_next_page
from fans_api_adapter.send_outcome import classify_send_error
from fans_api_adapter.webhooks import verify_signature


class SyntheticFansApiTests(unittest.TestCase):
    def test_only_specific_documented_errors_prove_this_attempt_not_sent(self):
        cases = [
            (503, {"error": "SERVICE_UNAVAILABLE", "request_outcome": "not_sent"}, Outcome.NOT_SENT),
            (503, {"error": "SERVICE_UNAVAILABLE", "request_outcome": "unknown"}, Outcome.UNKNOWN),
            (503, {"error": "SERVICE_UNAVAILABLE"}, Outcome.UNKNOWN),
            (503, {"error": "IDEMPOTENCY_UNAVAILABLE"}, Outcome.NOT_SENT),
            (503, {"error": "IDEMPOTENCY_UNAVAILABLE", "request_outcome": "unknown"}, Outcome.UNKNOWN),
            (409, {"error": "IDEMPOTENCY_CONFLICT"}, Outcome.CONFLICT),
            (422, {"error": "IDEMPOTENCY_KEY_MISMATCH"}, Outcome.KEY_MISMATCH),
            (500, {"error": "SERVICE_UNAVAILABLE", "request_outcome": "not_sent"}, Outcome.UNKNOWN),
            (200, {"data": {"id": 123}}, Outcome.UNKNOWN),
            (503, None, Outcome.UNKNOWN),
            (None, None, Outcome.UNKNOWN),
        ]
        for status, body, expected in cases:
            with self.subTest(status=status, body=body):
                self.assertEqual(classify_send_error(status, body), expected)

    def test_unavailable_idempotency_does_not_resolve_older_unknown(self):
        action = ActionIdentity(ConversationKey("tenant", "of-a", "creator", "fan"), 1,
                                "fans_api", "acct_1", "action", "key", "a" * 64)
        previous = AttemptEvidence(action, "first", Outcome.UNKNOWN, "synthetic-response")
        current = AttemptEvidence(action, "second", classify_send_error(503, {"error": "IDEMPOTENCY_UNAVAILABLE"}),
                                  "synthetic-response")
        decision = decide_recovery(action, [previous, current], history_complete=True,
                                   approved_retry_budget_available=True)
        self.assertEqual(decision.next_step, NextStep.RECONCILE)

    def test_signature_uses_exact_raw_bytes_and_rejects_invalid_inputs(self):
        raw = b"The quick brown fox jumps over the lazy dog"
        signature = "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
        self.assertTrue(verify_signature(raw, signature, b"key"))
        self.assertTrue(verify_signature(raw, signature.upper(), b"key"))
        self.assertFalse(verify_signature(raw + b" ", signature, b"key"))
        self.assertFalse(verify_signature(raw, signature, b"wrong"))
        self.assertFalse(verify_signature(raw, signature, b""))
        self.assertFalse(verify_signature(raw, "sha256=" + signature, b"key"))
        self.assertFalse(verify_signature(raw, None, b"key"))

    def test_short_page_keeps_its_next_page_and_missing_cursor_is_not_complete(self):
        url = "https://app.onlyfansapi.com/api/acct_1/chats/42/messages?first_id=123"
        page = parse_history_page({"data": [{"id": 123}], "_pagination": {"next_page": url}},
                                  connection_id="acct_1", chat_id="42")
        self.assertEqual(len(page.messages), 1)
        self.assertEqual(page.next_page, url)
        last = parse_history_page({"data": [], "_pagination": {"next_page": None}},
                                  connection_id="acct_1", chat_id="42")
        self.assertIsNone(last.next_page)
        with self.assertRaises(ValueError):
            parse_history_page({"data": [], "_pagination": {}}, connection_id="acct_1", chat_id="42")
        with self.assertRaises(ValueError):
            parse_history_page({"data": [], "_pagination": {"next_page": None}}, connection_id="../bad", chat_id="42")

    def test_next_page_cannot_leave_the_exact_account_and_chat(self):
        path = "/api/acct_1/chats/42/messages"
        rejected = [
            "http://app.onlyfansapi.com" + path,
            "https://evil.invalid" + path,
            "https://app.onlyfansapi.com.evil.invalid" + path,
            "https://app.onlyfansapi.com@evil.invalid" + path,
            "https://user@app.onlyfansapi.com" + path,
            "https://app.onlyfansapi.com:444" + path,
            "https://app.onlyfansapi.com/api/acct_2/chats/42/messages",
            "https://app.onlyfansapi.com/api/acct_1/chats/43/messages",
            "https://app.onlyfansapi.com/api/acct_1/chats/42/messages/../other",
            "https://app.onlyfansapi.com" + path + "#fragment",
            "//app.onlyfansapi.com" + path,
        ]
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_history_next_page(url, connection_id="acct_1", chat_id="42")
        with self.assertRaises(ValueError):
            validate_history_next_page("https://app.onlyfansapi.com" + path, connection_id="../acct_1", chat_id="42")


if __name__ == "__main__":
    unittest.main()
