import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from conversation_engine.evidence import AbsenceCoverage, CoverageStatus, EvidenceTarget, EvidenceWindow
from conversation_engine.identity import ActionIdentity, ConversationKey
from conversation_engine.recovery import AttemptEvidence, NextStep, Outcome, decide_recovery
from conversation_engine.sales import SalesMode, SalesPermission


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.key = ConversationKey("tenant-a", "of-a", "creator-a", "fan-42")
        self.action = ActionIdentity(self.key, 1, "fans_api", "acct_first", "action-1", "key-1", "a" * 64)

    def attempt(self, attempt_id, outcome, *, action=None, receipt=None):
        return AttemptEvidence(action or self.action, attempt_id, outcome, "synthetic-provider-evidence", receipt)

    def decide(self, items, *, complete=True, budget=True):
        return decide_recovery(self.action, items, history_complete=complete,
                               approved_retry_budget_available=budget).next_step

    def test_opaque_ids_remain_distinct_and_reject_implicit_numeric_conversion(self):
        self.assertNotEqual(self.key, replace(self.key, creator_id="creator-b"))
        self.assertNotEqual(self.key, replace(self.key, account_id="of-b"))
        with self.assertRaises(ValueError):
            replace(self.key, fan_id=42)
        with self.assertRaises(ValueError):
            replace(self.action, binding_epoch=True)
        with self.assertRaises(ValueError):
            replace(self.action, payload_sha256="A" * 64)

    def test_unknown_survives_a_later_not_sent_response_and_retry_permission(self):
        attempts = [self.attempt("first", Outcome.UNKNOWN), self.attempt("second", Outcome.NOT_SENT)]
        self.assertEqual(self.decide(attempts), NextStep.RECONCILE)
        self.assertEqual(self.decide(attempts[::-1]), NextStep.RECONCILE)

    def test_incomplete_or_missing_history_cannot_prove_safe_retry(self):
        safe = self.attempt("one", Outcome.NOT_SENT)
        self.assertEqual(self.decide([safe], complete=False), NextStep.INVESTIGATE)
        self.assertEqual(self.decide([]), NextStep.INVESTIGATE)
        self.assertEqual(self.decide([safe], budget=False), NextStep.HOLD)
        self.assertEqual(self.decide([safe]), NextStep.RETRY_SAME_ACTION)

    def test_replay_is_deduplicated_but_conflicting_evidence_is_not(self):
        safe = self.attempt("one", Outcome.NOT_SENT)
        self.assertEqual(self.decide([safe, safe]), NextStep.RETRY_SAME_ACTION)
        self.assertEqual(self.decide([safe, self.attempt("one", Outcome.UNKNOWN)]), NextStep.INVESTIGATE)

    def test_receipt_projection_and_uncertainty_are_distinct(self):
        accepted = self.attempt("one", Outcome.ACCEPTED, receipt="msg-1")
        self.assertEqual(self.decide([accepted]), NextStep.PROJECT_RECEIPT)
        self.assertEqual(self.decide([accepted, self.attempt("two", Outcome.UNKNOWN)]), NextStep.RECONCILE)
        self.assertEqual(self.decide([accepted, self.attempt("two", Outcome.ACCEPTED, receipt="msg-2")]),
                         NextStep.RECONCILE)
        with self.assertRaises(ValueError):
            self.attempt("bad", Outcome.ACCEPTED)

    def test_key_mismatch_stops_and_changed_payload_or_binding_never_retries(self):
        self.assertEqual(self.decide([self.attempt("one", Outcome.KEY_MISMATCH)]), NextStep.STOP)
        for changed in (replace(self.action, payload_sha256="b" * 64),
                        replace(self.action, binding_epoch=2),
                        replace(self.action, provider="other_provider"),
                        replace(self.action, connection_id="acct_reconnected"),
                        replace(self.action, conversation=replace(self.key, creator_id="creator-b"))):
            with self.subTest(changed=changed):
                self.assertEqual(self.decide([self.attempt("one", Outcome.NOT_SENT, action=changed)]),
                                 NextStep.INVESTIGATE)

    def test_sales_refusal_persists_until_explicit_scoped_reopening(self):
        paused = SalesPermission(self.key).decline("refusal-event")
        self.assertEqual(paused.mode, SalesMode.PAUSED)
        self.assertFalse(paused.permits_offer(self.key, "item-a"))
        reopened = paused.reopen_for("renewed-interest", frozenset({"item-a"}))
        self.assertTrue(reopened.permits_offer(self.key, "item-a"))
        self.assertFalse(reopened.permits_offer(self.key, "item-b"))
        self.assertFalse(reopened.permits_offer(replace(self.key, account_id="of-b"), "item-a"))
        self.assertFalse(reopened.decline("second-refusal").permits_offer(self.key, "item-a"))
        with self.assertRaises(ValueError):
            paused.reopen_for("unclear-interest", frozenset())

    def test_absence_requires_exact_scope_complete_fresh_authorized_coverage(self):
        now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
        target = EvidenceTarget(self.key, 1, "fans_api", "item-a:v1", "purchase", "offer-1")
        window = EvidenceWindow(now - timedelta(days=7), now - timedelta(hours=1))
        coverage = AbsenceCoverage(target, window, "approved-ledger", now, now + timedelta(minutes=5),
                                   CoverageStatus.COMPLETE, True)
        self.assertTrue(coverage.establishes_absence(target, window, now=now, records_found=False))
        variants = [replace(coverage, source_authorized_for_absence=False),
                    replace(coverage, status=CoverageStatus.INCOMPLETE),
                    replace(coverage, status=CoverageStatus.CONFLICTING)]
        for candidate in variants:
            self.assertFalse(candidate.establishes_absence(target, window, now=now, records_found=False))
        self.assertFalse(coverage.establishes_absence(target, window, now=now + timedelta(minutes=6), records_found=False))
        self.assertFalse(coverage.establishes_absence(replace(target, binding_epoch=2), window, now=now, records_found=False))
        self.assertFalse(coverage.establishes_absence(replace(target, provider="other_provider"), window, now=now, records_found=False))
        self.assertFalse(coverage.establishes_absence(replace(target, evidence_kind="delivery"), window, now=now, records_found=False))
        self.assertFalse(coverage.establishes_absence(replace(target, subject_id="offer-2"), window, now=now, records_found=False))
        self.assertFalse(coverage.establishes_absence(replace(target, conversation=replace(self.key, fan_id="fan-43")), window, now=now, records_found=False))
        self.assertFalse(coverage.establishes_absence(target, window, now=now, records_found=True))
        wider = EvidenceWindow(window.start - timedelta(seconds=1), window.end)
        self.assertFalse(coverage.establishes_absence(target, wider, now=now, records_found=False))


if __name__ == "__main__":
    unittest.main()
