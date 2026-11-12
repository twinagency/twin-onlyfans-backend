"""Step 6 / Q13: what the import actually established decides what the commercial worker may sell, fan by fan.

Preparation (read-only import through the guarded fake provider, one failed page that resumes at its saved cursor) -> promotion ->
launch -> signed webhooks -> real worker. Nothing is seeded: no grant, no backfill_complete, no ownership fact. The import does NOT
continue after launch (a launched scope is no longer 'configured'); coverage is whatever the preparation phase proved, and elapsed
time never substitutes for it.

  FAN_A  complete history, an OLDER opened paid card (owns media 31)      -> the owned single is not re-offered, a different product is
  FAN_B  one quarantined row -> an open row gap                            -> no card until a person reviews the gap, then the card
  FAN_C  inbound message already imported, then the same message arrives live (overlap) -> one message, at most one reply/card
  FAN_D  never read                                                        -> chat only, never a card
"""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from commercial_worker_fixture import FAN_A, FAN_B, FAN_C, FAN_D, OPERATOR, build_world, key
from conversation_engine.writer_context import set_active_profiles
from db.models import Message
from history_support import card as history_card, fan_message
from test_commercial_matrix_pg import approve_live, card_to, to_fan
from test_commercial_preparation_pg import ACTOR, TENANT
from test_commercial_sale_rules_pg import catalogue, grants, send_ok
from test_history_import_pg import gaps_of, hist
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_engine_real_worker_pg import iso


def test_q13_import_coverage_decides_the_sale_per_fan_and_readiness_is_never_claimed_complete(database, tmp_path, monkeypatch):
    def history(cfg):
        return {FAN_A: [fan_message(6101, FAN_A, 'hi', created='2026-09-01T10:00:00Z'),
                        history_card(6102, cfg, [31], 1200, previews=[], opened=True, created='2026-09-01T10:10:00Z')],      # older purchase
                FAN_B: [fan_message(6201, FAN_B, 'hi'), fan_message(6202, FAN_B, 'tip row', price=3)],                      # quarantined row
                FAN_C: [fan_message(6301, FAN_C, 'show me the pool video', created='2026-11-12T01:00:00Z')]}
    w = build_world(database, tmp_path, monkeypatch, i=13, history=history, read=(FAN_A, FAN_B, FAN_C))
    try:
        catalogue(w)
        w.launch()
        coverage = hist().coverage_view(w.store, TENANT, w.account)
        assert coverage['state'] != 'complete', 'the fan universe was never enumerated; time or a launch cannot make it complete'
        assert coverage['fans']['complete'] == 2 and coverage['fans']['partial'] == 1 and coverage['gaps']['openRows'] == 1, coverage['fans']

        # A: owns media 31 from the retained older card
        staged, tick = w.converse('show me the beach photo', FAN_A)
        send_ok(w, tick)
        assert card_to(w, FAN_A) == [] and 'APPROVED OFFER: none' in w.model_prompts[-1]
        staged, tick = w.converse('send me the pool video', FAN_A)
        send_ok(w, tick)
        assert [c['body']['mediaFiles'] for c in card_to(w, FAN_A)] == [['41']]

        # B: an open row gap blocks the paid action for that fan only; the reply still goes out
        staged, tick = w.converse('show me the beach photo', FAN_B)
        send_ok(w, tick)
        assert card_to(w, FAN_B) == [] and len(to_fan(w, FAN_B)) == 1 and grants(w, FAN_B) == []
        gap = gaps_of(database, w.account, kind='row')[0]
        hist().dismiss_row_gap(w.store, TENANT, w.account, gap.gap_id, command_key=key(0x1301), actor=ACTOR, reason='Reviewed: a fan tip row')
        staged, tick = w.converse('show me the beach photo', FAN_B)
        send_ok(w, tick)
        assert [c['body']['price'] for c in card_to(w, FAN_B)] == [12.0], 'after a person reviews the gap the same fan is served'

        # D: never read -> no coverage -> chat only
        staged, tick = w.converse('show me the beach photo', FAN_D)
        send_ok(w, tick)
        assert card_to(w, FAN_D) == [] and len(to_fan(w, FAN_D)) == 1 and grants(w, FAN_D) == []

        # C: the imported message arrives live as well: one stored message, at most one reply and one card
        result = w.deliver(w.inbound(6301, 'show me the pool video', FAN_C), 'overlap-6301')
        if result.get('outcome') == 'awaiting_approval':
            w.approve_turn(result)
            w.unthrottle()
            w.pilot.tick()
        w.deliver(w.inbound(6301, 'show me the pool video', FAN_C), 'overlap-6301-again')
        w.unthrottle()
        w.pilot.tick()
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(Message).where(
                Message.account_id == w.account, Message.fan_id == FAN_C, Message.message_id == 6301)) == 1
        assert len(card_to(w, FAN_C)) <= 1 and len(to_fan(w, FAN_C)) <= 1
        # the other fans' history is untouched by the overlap and no fan outside the audience appeared
        assert len(card_to(w, FAN_A)) == 1 and len(card_to(w, FAN_B)) == 1
    finally:
        set_active_profiles(None)
        w.store.close()
