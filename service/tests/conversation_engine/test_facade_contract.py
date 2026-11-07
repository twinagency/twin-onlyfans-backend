import unittest
from unittest.mock import patch

from onlyfans_read import contract
from onlyfans_commands import settings


class ExistingFacadeContractTests(unittest.TestCase):
    def test_versions_and_namespaces_are_frozen(self):
        self.assertEqual(contract.SCHEMA_VERSION, "of-preview.v2")
        self.assertEqual(contract.SCHEMA_VERSION_HEADER, "X-OnlyFans-Schema-Version")
        self.assertEqual(settings.SCHEMA, "of-controls.v1")
        self.assertEqual(settings.PREFIX, "/onlyfans/control/v1")
        self.assertTrue(contract.is_service_path("/onlyfans/api/v1/accounts"))
        self.assertFalse(contract.is_service_path("/onlyfans/api/v10/accounts"))

    def test_conversation_and_cursor_are_scoped_to_the_account(self):
        self.assertEqual(contract.conversation_id_for("of-a", 42), "of-a:42")
        self.assertEqual(contract.fan_id_from_conversation_id("of-a:42", "of-a"), 42)
        self.assertIsNone(contract.fan_id_from_conversation_id("of-a:42", "of-b"))
        cursor = contract.encode_cursor(account_id="of-a", last_activity_at=None, fan_id=42)
        self.assertEqual(contract.decode_cursor(cursor, account_id="of-a"), (None, 42))
        with self.assertRaises(ValueError):
            contract.decode_cursor(cursor, account_id="of-b")

    def test_unknown_and_queued_are_not_sent(self):
        self.assertIn("unknown", contract.DELIVERY_OUTCOMES)
        self.assertIn("unknown", contract.UNRESOLVED_OUTCOMES)
        self.assertIn("queued", contract.UNRESOLVED_OUTCOMES)
        self.assertNotIn("sent", contract.UNRESOLVED_OUTCOMES)

    def test_control_requires_a_purpose_specific_token_and_explicit_manual_enablement(self):
        synthetic = {"ONLYFANS_PROCESS_ROLE": "api", "ONLYFANS_CONTROL_TOKEN": "control-synthetic",
                     "ONLYFANS_CONTROL_TENANT": "tenant-a"}
        with patch.dict("os.environ", synthetic, clear=True):
            self.assertEqual(settings.configured(), ("control-synthetic", "tenant-a"))
            self.assertIsNone(settings.manual_configured())
            with patch.dict("os.environ", {"ONLYFANS_MANUAL_ENABLED": "1"}):
                self.assertIsNotNone(settings.manual_configured())
            with patch.dict("os.environ", {"ONLYFANS_API_TOKEN": "control-synthetic"}):
                self.assertIsNone(settings.configured())


if __name__ == "__main__":
    unittest.main()
