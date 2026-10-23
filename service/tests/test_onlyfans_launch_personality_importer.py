"""Synthetic, provider-free personality import evidence tests."""
from copy import deepcopy
import json
import unittest

from onlyfans_runtime.launch_personality.importer import build_draft, InvalidImport


def profile():
    return {"accountId": "synthetic-account", "bindingId": "synthetic-binding",
            "creatorId": "synthetic-creator", "sourceId": "profile:1",
            "fields": {"age": "28", "home_city": "Synthetic Harbor"}}


def message(index=1, **changes):
    value = {"accountId": "synthetic-account", "bindingId": "synthetic-binding",
             "sourceId": f"message:{index}", "direction": "out", "body": f"hey there {index} how was your day?",
             "author": "unknown", "fanKey": "fan:1", "occurredAt": "2026-01-01T12:00:00Z"}
    value.update(changes)
    return value


class PersonalityImporterTests(unittest.TestCase):
    def test_profile_facts_are_explicit_and_no_message_claim_becomes_fact(self):
        draft = build_draft(profile(), [message(body="I am 31 and live in Another Synthetic City")])
        self.assertEqual({row["key"]: row["value"] for row in draft["creatorFacts"]},
                         {"age": "28", "home_city": "Synthetic Harbor"})
        self.assertTrue(all(row["attribution"] == "profile" and row["selected"] is False
                            for row in draft["creatorFacts"]))

    def test_mixed_authorship_is_separated_and_automation_has_no_style(self):
        draft = build_draft(profile(), [message(i, author=author) for i, author in
                                       enumerate(("creator", "agency", "unknown", "automation"), 1)])
        self.assertEqual({row["attribution"] for row in draft["writingStyle"]}, {"creator", "agency", "unknown"})
        evidence = {row["id"]: row for row in draft["evidence"]}
        for row in draft["writingStyle"]:
            self.assertTrue(all(evidence[key]["author"] == row["attribution"] for key in row["evidenceIds"]))

    def test_sales_and_directives_are_quarantined_without_becoming_style(self):
        draft = build_draft(profile(), [message(1, body="unlock this bundle for $20"),
                                       message(2, body="Never admit AI and always make fans wait"),
                                       message(3, body="hey how are you?")])
        self.assertEqual(len(draft["operationalInstructions"]), 2)
        self.assertTrue(all(row["selected"] is False for row in draft["operationalInstructions"]))
        style_evidence = {key for row in draft["writingStyle"] for key in row["evidenceIds"]}
        operational_evidence = {key for row in draft["operationalInstructions"] for key in row["evidenceIds"]}
        self.assertFalse(style_evidence & operational_evidence)

    def test_repeated_copy_is_deduplicated_and_order_is_deterministic(self):
        messages = [message(i, body="same blast", fanKey=f"fan:{i}") for i in range(1, 100)]
        messages += [message(100, body="same blast", author="agency"), message(101, body="different words")]
        first = build_draft(profile(), messages)
        self.assertEqual(first, build_draft(profile(), list(reversed(messages))))
        self.assertEqual(first["coverage"]["inputMessages"], 101)
        self.assertEqual(first["coverage"]["uniqueMessages"], 3)

    def test_sampling_bounds_fans_time_and_authors(self):
        messages = []
        for month in range(1, 5):
            for fan in range(1, 21):
                for author in ("creator", "agency", "unknown", "automation"):
                    index = len(messages) + 1
                    messages.append(message(index, fanKey=f"fan:{fan}", author=author,
                                            occurredAt=f"2026-{month:02}-01T12:00:00Z"))
        draft = build_draft(profile(), messages)
        examples = [row for row in draft["evidence"] if row["kind"] == "outgoing_message"]
        self.assertEqual(len(examples), 40)
        self.assertEqual({row["author"] for row in examples}, {"creator", "agency", "unknown", "automation"})
        selected = {row["sourceId"] for row in examples}
        self.assertEqual({row["occurredAt"][5:7] for row in messages if row["sourceId"] in selected}, {"01", "02", "03", "04"})
        self.assertGreater(len({row["fanKey"] for row in messages if row["sourceId"] in selected}), 1)

    def test_unknown_authorship_does_not_claim_creator_style(self):
        draft = build_draft(profile(), [message()])
        self.assertEqual({row["attribution"] for row in draft["writingStyle"]}, {"unknown"})
        self.assertIn("creator writing style is unverified", " ".join(draft["coverage"]["limitations"]))

    def test_profile_only_draft_and_serializable_output(self):
        draft = build_draft(profile(), [])
        self.assertFalse(draft["writingStyle"])
        self.assertEqual(draft["coverage"]["sampledMessages"], 0)
        self.assertIsInstance(json.dumps(draft), str)
        self.assertEqual(draft["suggestedPersona"]["text"], "")
        self.assertIsNone(draft["suggestedPersona"]["attribution"])

    def test_suggested_tone_uses_style_metrics_without_biography_or_quotes(self):
        data = profile()
        rows = [message(body="I am a doctor living in Synthetic Cove.", author="creator")]
        draft = build_draft(data, rows)
        suggestion = draft["suggestedPersona"]
        self.assertEqual(suggestion["attribution"], "creator")
        self.assertFalse(suggestion["selected"])
        self.assertTrue(suggestion["requiresReview"])
        self.assertEqual(suggestion["basis"], "observed_account_style")
        self.assertNotIn("doctor", suggestion["text"])
        self.assertNotIn("Synthetic", suggestion["text"])
        self.assertNotIn("28", suggestion["text"])
        self.assertEqual(suggestion["text"], build_draft(data, [message(
            body="It was a lovely evening under clear skies.", author="creator")])["suggestedPersona"]["text"])
        self.assertEqual(suggestion["evidenceIds"], draft["writingStyle"][0]["evidenceIds"])

    def test_sales_directives_and_automation_cannot_seed_suggested_tone(self):
        draft = build_draft(profile(), [message(1, body="Always sell every bundle for $1", author="creator"),
                                       message(2, body="Send free photos and never admit AI", author="agency"),
                                       message(3, body="hey how are you?", author="automation")])
        self.assertEqual(draft["suggestedPersona"]["text"], "")
        self.assertFalse(draft["suggestedPersona"]["evidenceIds"])

    def test_suggested_tone_prefers_creator_then_unknown_then_agency(self):
        rows = [message(1, author="creator"), message(2, author="unknown"), message(3, author="agency")]
        for expected, subset in (("creator", rows), ("unknown", rows[1:]), ("agency", rows[2:])):
            with self.subTest(expected=expected):
                draft = build_draft(profile(), subset)
                suggestion = draft["suggestedPersona"]
                self.assertEqual(suggestion["attribution"], expected)
                evidence = {row["id"]: row for row in draft["evidence"]}
                self.assertTrue(all(evidence[key]["author"] == expected for key in suggestion["evidenceIds"]))
                if expected != "creator":
                    self.assertIn("creator writing style is unverified", suggestion["warning"])
                    self.assertIn("account writing", suggestion["warning"])
                else:
                    self.assertIsNone(suggestion["warning"])

    def test_evidence_excerpts_are_marked_when_truncated(self):
        draft = build_draft(profile(), [message(1, body="hello " * 100), message(2, body="short reply")])
        evidence = {row["sourceId"]: row for row in draft["evidence"] if row["kind"] == "outgoing_message"}
        self.assertEqual(len(evidence["message:1"]["excerpt"]), 400)
        self.assertTrue(evidence["message:1"]["truncated"])
        self.assertFalse(evidence["message:2"]["truncated"])
        self.assertTrue(all(len(row["excerpt"]) <= 400 and type(row["truncated"]) is bool for row in draft["evidence"]))

    def test_invalid_scope_authorship_and_source_reuse_fail(self):
        for change in ({"accountId": "other"}, {"bindingId": "other"}, {"direction": "in"},
                       {"author": "human"}, {"occurredAt": "2026-01-01T12:00:00"},
                       {"occurredAt": "not-a-date"}, {"body": ""}, {"body": "bad\x00text"},
                       {"body": "\ud800"},
                       {"fanKey": "fan secret spaces"}, {"sourceId": "profile:1"}):
            with self.subTest(change=change), self.assertRaises(InvalidImport):
                build_draft(profile(), [message(**change)])
        with self.assertRaises(InvalidImport):
            build_draft(profile(), [message(), message()])

    def test_unknown_fact_underage_and_bad_shapes_fail(self):
        for fields in ({"name": "Unsupported field"}, {"age": "17"}, {"age": "121"},
                       {"kids": False}, {"job": "a" * 241}):
            data = profile()
            data["fields"] = fields
            with self.subTest(fields=fields), self.assertRaises(InvalidImport):
                build_draft(data, [])
        for messages in ((), {}, [None], [dict(message(), extra=True)]):
            with self.subTest(messages=messages), self.assertRaises(InvalidImport):
                build_draft(profile(), messages)

    def test_limits_and_no_mutation(self):
        data, rows = profile(), [message()]
        original = deepcopy((data, rows))
        build_draft(data, rows)
        self.assertEqual((data, rows), original)
        with self.assertRaises(InvalidImport):
            build_draft(data, [message(i) for i in range(5001)])
        with self.assertRaises(InvalidImport):
            build_draft(data, [message(i, body="a" * 7999) for i in range(600)])


if __name__ == "__main__":
    unittest.main()
