"""Neutral regressions for classification data crossing the display boundary.

No model, provider, database or credentials are used. These tests exercise the
real caption module and preserve the separate exact-match caption behavior.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / "automations" / "pack_claim.py"
_SPEC = importlib.util.spec_from_file_location("substitute_caption_pack_claim", _MODULE_PATH)
claim_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = claim_module
_SPEC.loader.exec_module(claim_module)


class _NonDisplayableSubject:
    def __str__(self):
        raise AssertionError("internal metadata must not be stringified for display")


class SubstituteCaptionSubjectTests(unittest.TestCase):
    def test_internal_third_person_labels_do_not_reach_the_caption(self):
        kinds = ["video"]
        action = "me waving hello"
        expected = claim_module.substitute_clause(kinds, None, action)
        for label in (
            "custom greeting mentioning his name",
            "personalized birthday message for him",
            "resolver_note: customer_name_missing",
        ):
            with self.subTest(label=label):
                actual = claim_module.substitute_clause(kinds, label, action)
                self.assertEqual(actual, expected)
                self.assertNotIn(label, actual.text)

    def test_unexpected_subject_types_are_never_rendered(self):
        expected = claim_module.substitute_clause(["photo"], None)
        for subject in (
            {"internal": "customer_name_missing"},
            ["personalized", "greeting"],
            42,
            _NonDisplayableSubject(),
        ):
            with self.subTest(subject_type=type(subject).__name__):
                self.assertEqual(
                    claim_module.substitute_clause(["photo"], subject), expected
                )

    def test_missing_subject_keeps_mismatch_description_and_media_counts(self):
        kinds = ["photo", "video", "audio"]
        action = "me waving hello"
        for subject in (None, "", "   "):
            with self.subTest(subject=subject):
                claim = claim_module.substitute_clause(kinds, subject, action)
                self.assertTrue(claim_module.is_substitute_claim(claim.text))
                self.assertIn("what you asked for", claim.text)
                self.assertIn(action, claim.text)
                self.assertIn("1 pic + 1 vid + 1 voice note", claim.text)
                self.assertEqual(claim.n, len(kinds))

    def test_direct_exact_match_caption_is_unchanged(self):
        claim = claim_module.ask_clause(["photo", "video"], "mountain views")
        self.assertEqual(claim, claim_module.Claim("1 pic + 1 vid of mountain views", 2))
        self.assertFalse(claim_module.is_substitute_claim(claim.text))

    def test_action_and_optional_reply_still_use_existing_guards(self):
        claim = claim_module.substitute_clause(["photo"], "internal label", "$99")
        self.assertNotIn("$99", claim.text)
        self.assertTrue(claim_module.is_substitute_claim(claim.text))
        self.assertEqual(claim_module.compose_caption(claim, "costs $99"), claim.text)
        caption = claim_module.compose_caption(claim, "Thanks for asking.")
        self.assertTrue(claim_module.is_substitute_claim(caption))
        self.assertNotIn("internal label", caption)
        self.assertEqual(claim.n, 1)


if __name__ == "__main__":
    unittest.main()
