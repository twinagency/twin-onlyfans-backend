"""First-seen message facts: the paid-card terms a creator message carried, with NULL meaning unknown.

Real PostgreSQL, mocked wire. Previews, locked text and queue origin are captured before message
normalisation discards them (ingress hashes and replay behaviour are unchanged). A priced message is a fact,
not a sale: nothing here says anyone bought anything.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_commercial_preparation_pg import TENANT, ACTOR
from history_support import Provider, card, chat_page, creator_message, fan_message, prepared, run, tx_page, tx_row
from test_history_import_pg import cursor_of, drive, hist, models, source_of
from onlyfans_runtime.store import Refused

FAN = 401
SECRET = b"facts-secret"


def facts(database, account):
    from db.onlyfans_history_models import OfFansMessageFact
    with Session(database[1]) as s:
        out = {f.message_id: f for f in s.scalars(select(OfFansMessageFact).where(OfFansMessageFact.account_id == account))}
        s.expunge_all()
        return out


def import_page(store, database, account, cfg, messages, fan=FAN):
    wire = Provider(cfg)
    wire.chats[fan] = [chat_page(messages)]
    hist().enlist_fan(store, account, fan, priority=0)
    drive(store, account, wire, lambda: cursor_of(database, account, fan).status == "complete")
    return wire


def test_a_card_is_recorded_with_its_previews_lock_queue_origin_and_opening(database):
    store, account, cfg = prepared(database)
    try:
        import_page(store, database, account, cfg, [
            card(9001, cfg, [111, 112, 113], 15, previews=[111], opened=True, lockedText=False, isFromQueue=True)])
        fact = facts(database, account)[9001]
        assert (fact.fan_id, fact.price_cents, json.loads(fact.media_ids_json), json.loads(fact.preview_ids_json)) == (FAN, 1500, [111, 112, 113], [111])
        assert (fact.locked_text, fact.from_queue, fact.is_opened, fact.is_tip, fact.via, fact.conflicted) == (False, True, True, None, "history", False)
        assert fact.opened_at == datetime(2026, 10, 2, 9, 0) and len(fact.raw_sha256) == 64 and fact.sent_at == datetime(2026, 10, 1, 10, 10)
        assert fact.price_cents == 1500, "the whole card price is stored; a bundle price is never divided"
    finally:
        store.close()


def test_a_missing_previews_key_is_unknown_and_an_empty_list_is_a_fact(database):
    store, account, cfg = prepared(database)
    try:
        import_page(store, database, account, cfg, [
            card(9101, cfg, [111, 112], 15),                                   # no previews key at all
            card(9102, cfg, [121, 122], 15, previews=[]),                      # the creator showed no free preview
            card(9103, cfg, [131, 132], 15, previews=[131, 999]),              # not a subset of the media: unsupported
            card(9104, cfg, [141], 15, previews="all")])                       # wrong type
        got = facts(database, account)
        assert got[9101].preview_ids_json is None and got[9101].locked_text is None and got[9101].from_queue is None and got[9101].is_opened is None
        assert got[9102].preview_ids_json == "[]"
        assert got[9103].preview_ids_json is None and got[9104].preview_ids_json is None
    finally:
        store.close()


def test_only_priced_media_messages_are_facts_and_a_tip_flag_is_kept(database):
    store, account, cfg = prepared(database)
    try:
        import_page(store, database, account, cfg, [
            fan_message(9201, FAN, "hi"),
            creator_message(9202, cfg, "free pic", media=[{"id": 111}]),                    # free media: not a purchase
            creator_message(9203, cfg, "locked text only", price=10),                      # priced, no media
            card(9204, cfg, [151], 9, isTip=True, opened=True)])
        got = facts(database, account)
        assert set(got) == {9204} and got[9204].is_tip is True
    finally:
        store.close()


def test_reobserving_a_card_is_idempotent_records_a_later_opening_and_flags_a_conflict(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    first = card(9301, cfg, [111, 112], 15, previews=[111])
    opened = card(9301, cfg, [111, 112], 15, previews=[111], opened=True)
    changed = card(9301, cfg, [111, 112, 113], 15, previews=[111])
    wire.chats[FAN] = [chat_page([first]), chat_page([first]), chat_page([opened]), chat_page([changed])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        for n in range(4):
            before = wire.count("chats")
            drive(store, account, wire, lambda: wire.count("chats") > before)
            fact = facts(database, account)[9301]
            with database[1].begin() as c:       # let the same chat be read again
                c.execute(text("UPDATE of_fans_history_cursor SET status='needed', completed_at=NULL, next_page=NULL WHERE account_id=:a"), {"a": account})
            if n == 0:
                assert fact.is_opened is None and fact.conflicted is False
            if n == 1:
                assert fact.conflicted is False and len(facts(database, account)) == 1
            if n == 2:
                assert fact.is_opened is True and fact.opened_at is not None and fact.conflicted is False
            if n == 3:
                assert fact.conflicted is True and json.loads(fact.media_ids_json) == [111, 112], "first-seen terms are kept; the disagreement is flagged"
                assert fact.is_opened is True
    finally:
        store.close()


def test_recording_a_fact_does_not_change_the_message_or_ingress_the_importer_wrote(database):
    from db.models import Message
    from db.onlyfans_models import OfIngress
    store, account, cfg = prepared(database)
    try:
        import_page(store, database, account, cfg, [card(9401, cfg, [111, 112], 15, previews=[111], opened=True)])
        with Session(database[1]) as s:
            message = s.get(Message, (account, FAN, 9401))
            assert message.price_cents == 1500 and json.loads(message.media_ids) == [111, 112] and message.is_paid is None
            payload = json.loads(s.scalars(select(OfIngress).where(OfIngress.account_id == account, OfIngress.message_id == 9401)).one().payload)
            assert set(payload) == {"fan_id", "message_id", "created_at", "direction", "body", "media_ids", "price_cents"}
    finally:
        store.close()


def signed(intake, body, key):
    raw = json.dumps(body, separators=(",", ":")).encode()
    return intake.accept(raw, hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), SECRET, key)


def test_live_webhooks_record_the_card_and_its_opening(database):
    from fans_api_adapter.events import FansEventIntake
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    when = datetime.now(timezone.utc).isoformat()
    try:
        run(store, account, wire)
        intake = FansEventIntake(store, human_grace_seconds=3600)
        sent = {"event": "messages.sent", "account_id": cfg["provider_account"], "payload": {
            "id": 9501, "toUser": {"id": FAN}, "fromUser": {"id": int(cfg["creator_id"])}, "text": "locked", "createdAt": when,
            "media": [{"id": 211}, {"id": 212}], "price": 25, "previews": [211], "lockedText": False, "isFromQueue": False}}
        assert signed(intake, sent, "evt-sent")["outcome"] == "accepted"
        fact = facts(database, account)[9501]
        assert (fact.fan_id, fact.price_cents, fact.via, json.loads(fact.preview_ids_json), fact.is_opened) == (FAN, 2500, "webhook", [211], None)
        unlocked = {"event": "messages.ppv.unlocked", "account_id": cfg["provider_account"], "payload": {
            "id": "n-9501", "message_id": "9501", "chat_id": str(FAN), "user_id": cfg["creator_id"], "amountGross": 25,
            "amountNet": 20, "createdAt": when, "fan": {"id": str(FAN)}}}
        assert signed(intake, unlocked, "evt-unlock")["outcome"] == "accepted"
        assert facts(database, account)[9501].is_opened is True
        assert signed(intake, unlocked, "evt-unlock")["outcome"] == "duplicate"
        # a queued broadcast is a card too: a fan may unlock it later
        queued = {**sent, "payload": {**sent["payload"], "id": 9502, "isFromQueue": True}}
        signed(intake, queued, "evt-queued")
        assert facts(database, account)[9502].from_queue is True
    finally:
        store.close()


def test_a_malformed_card_never_breaks_intake_and_simply_has_no_fact(database):
    store, account, cfg = prepared(database)
    try:
        import_page(store, database, account, cfg, [
            card(9601, cfg, [111, 112], 15, previews=[111]),
            {**card(9602, cfg, [121], 15), "media": [{"id": 121}, {"noid": True}]}])    # malformed media list: row quarantined
        assert set(facts(database, account)) == {9601}
        assert cursor_of(database, account).status == "complete"
    finally:
        store.close()
