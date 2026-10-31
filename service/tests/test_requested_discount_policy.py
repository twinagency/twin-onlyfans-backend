"""Requested-discount policy: synthetic fixtures, no provider calls."""
import json
from datetime import timedelta
from pathlib import Path
from random import Random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from automations import upsell, _price_ask
from native_pilot import postsale
from native_pilot.service import Service
from test_native_pilot_postsale import build, config, offer, inbound


class RequestedDiscountTests(unittest.TestCase):
    def test_price_default_and_ceiling(self):
        self.assertEqual(upsell.RESEND_AFTER_M, 0)
        self.assertEqual(upsell.discount_price(670, Random(1), 'a'), 570)
        self.assertEqual(upsell.discount_price(1000, Random(1), 'a'), 850)
        for pct in (0.20, 0.50, 1, float('inf')):
            price = upsell.discount_price(670, Random(1), 'a', discount_pct=pct)
            self.assertGreaterEqual(price, 536)
            self.assertLessEqual(price, 570)
        self.assertEqual(upsell.discount_price(670, Random(1), 'a', discount_pct=0.20), 536)
        self.assertEqual(upsell.discount_price(670, Random(1), 'a', min_cents=600), 600)
        self.assertEqual(upsell.discount_price(301, Random(1), 'a'), 300)
        self.assertEqual(upsell.discount_price(300, Random(1), 'a'), 300)

    def test_one_request_without_second_message(self):
        from datetime import datetime
        now = datetime(2026, 10, 31, 5, 6, 36)
        said = [(now, 'Can you please make it cheaper baby and I will buy it')]
        objection, blocked = _price_ask.objection_state(said)
        self.assertIsNone(blocked)
        self.assertTrue(_price_ask.beat_after(said, objection))
        ctx = upsell.DiscountCtx(last_inbound_text=said[0][1], objection_at=now,
                                last_inbound_at=now, cuts_last_30d=8, asks_last_30d=8)
        self.assertEqual(upsell.may_discount(ctx), (True, 'ok'))
        self.assertEqual(upsell.may_discount(upsell.DiscountCtx()), (False, 'unprompted'))
        self.assertEqual(upsell.may_discount(upsell.DiscountCtx(
            objection_at=now, discount_count_this_item=1)), (False, 'already_cut'))
        self.assertEqual(upsell.may_discount(upsell.DiscountCtx(
            objection_at=now, spend_regret=True)), (False, 'distress'))

    def scan(self, body=None, *, prior=False, contextual=False, paid=False):
        with tempfile.TemporaryDirectory() as root:
            L, f = build(Path(root), 'requested')
            now = L.NOW
            config(f, L, {postsale.SETTING_DISCOUNT: True})
            if prior:
                offer(f, L, 1, offered=now - timedelta(hours=3))
            offer(f, L, 2, price=670, offered=now - timedelta(seconds=28),
                  status='delivered' if paid else 'open')
            if contextual:
                f.sql('CREATE TABLE of_contextual_offer(account_id TEXT,fan_id INTEGER,message_id INTEGER)')
                f.sql('INSERT INTO of_contextual_offer VALUES(?,?,?)', (L.ACCT, L.FAN, 5002))
            if body:
                inbound(f, L, 6000, body, now)
            queued = []
            svc = Service.__new__(Service)
            svc.path, svc.db = f.path, L.ACCT
            svc.followups = SimpleNamespace(add=lambda fan, kind, payload, when:
                                            queued.append((fan, payload)) or 'job')
            svc._postsale_in_scope = lambda fan: True
            svc.outcome = lambda *args: None
            svc.ledger = SimpleNamespace(followups_retire=lambda **kwargs: None)
            with patch('native_pilot.service.runner.now', return_value=now), \
                 patch('native_pilot.service.runner.emit'):
                svc.scan_postsale()
            return queued

    def test_fresh_request_queues_latest_card_only(self):
        rows = self.scan('Can you make it cheaper and I will buy it', prior=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], {'discount_offer': 2})

    def test_contextual_request_qualifies(self):
        self.assertEqual(len(self.scan('any discount?', contextual=True)), 1)

    def test_silence_paid_declined_and_distress_do_not_queue(self):
        for body in (None, 'hey babe', 'no thanks', 'im broke rn', 'spent too much, cheaper?'):
            self.assertEqual(self.scan(body), [], body)
        self.assertEqual(self.scan('any discount?', paid=True), [])
        self.assertEqual(self.scan(contextual=True), [])


if __name__ == '__main__':
    unittest.main()
