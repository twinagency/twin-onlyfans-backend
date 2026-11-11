"""Coverage is derived from cursors, retained gaps and counters, never from elapsed time or a stored label.

Real PostgreSQL, mocked wire. States are complete / partial / unknown. Nothing here says "unowned".
"""
import json
from datetime import timedelta
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_commercial_preparation_pg import TENANT, ACTOR
from history_support import (Provider, answer, card, cb_page, cb_row, chat_page, fail, fan_message, key, next_url, prepared,
                             rows, run, tx_page, tx_row)
from test_history_import_pg import (cursor_of, drive, gaps_of, hist, models, move, source_of, json_cursor)
from conversation_engine import preparation as prep
from onlyfans_runtime.store import Refused, Store, utcnow

FAN, OTHER = 401, 402


def view(store, account):
    return hist().coverage_view(store, TENANT, account)


def at_end(database, account):
    return all(json_cursor(source_of(database, account, n))["end_reached"] for n in ("transactions", "chargebacks"))


def read_everything(store, database, account, wire, fans=(FAN,)):
    for fan in fans:
        hist().enlist_fan(store, account, fan, priority=0)
    drive(store, account, wire, lambda: at_end(database, account) and all(cursor_of(database, account, f).status == "complete" for f in fans))


def see_webhook(database, account, at=None):
    from db.onlyfans_engine_models import OfFansWebhook
    with Session(database[1]) as s, s.begin():
        s.add(OfFansWebhook(account_id=account, epoch=1, event_key="evt-1", event_type="messages.received", payload_sha256="0" * 64,
                            raw_body=b"{}", status="accepted", received_at=at or utcnow()))


def test_nothing_is_complete_before_anything_is_read_and_the_view_says_so(database):
    store, account, cfg = prepared(database)
    try:
        shown = view(store, account)
        assert shown["state"] == "unknown" and shown["revision"] == 1
        assert {n: v["state"] for n, v in shown["sources"].items()} == {
            "vault_window": "unknown", "chats": "unknown", "transactions": "unknown", "chargebacks": "unknown"}
        assert shown["fans"] == {"known": 0, "complete": 0, "partial": 0, "unknown": 0}
        view_ = prep.view(store, TENANT, account)
        assert view_["historyCoverage"] == "unknown" and view_["ownershipCoverage"] == "unknown"
        assert view_["history"]["state"] == "unknown"
    finally:
        store.close()


def test_an_account_without_an_import_still_reports_pending(database):
    store, account, cfg = prepared(database, begin=False)
    try:
        assert hist().coverage_view(store, TENANT, account) == {"configured": False}
        shown = prep.view(store, TENANT, account)
        assert shown["historyCoverage"] == shown["ownershipCoverage"] == "pending" and "history" not in shown
    finally:
        store.close()


def test_a_fan_is_complete_only_after_end_of_pages_for_chat_transactions_and_chargebacks_with_webhooks(database):
    store, account, cfg = prepared(database, webhooks=False)
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([fan_message(7001, FAN)], next_url(cfg, FAN, 7000)), chat_page([fan_message(6999, FAN)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        run(store, account, wire)                                     # one chat page; the cursor is mid-history
        mid = hist().fan_coverage(store, TENANT, account, FAN)
        assert mid["state"] == "partial" and "history_incomplete" in mid["reasons"] and mid["asOf"] is None
        drive(store, account, wire, lambda: at_end(database, account) and cursor_of(database, account).status == "complete")
        done = hist().fan_coverage(store, TENANT, account, FAN)
        assert done["state"] == "partial" and done["reasons"] == ["webhooks_unverified"], "history alone is not coverage of the import interval"
        assert view(store, account)["sources"]["transactions"]["state"] == "partial"
        see_webhook(database, account)
        full = hist().fan_coverage(store, TENANT, account, FAN)
        assert full["state"] == "complete" and full["reasons"] == [] and full["asOf"] is not None
        assert view(store, account)["sources"]["transactions"]["state"] == "complete"
        assert view(store, account)["fans"] == {"known": 1, "complete": 1, "partial": 0, "unknown": 0}
    finally:
        store.close()


def test_an_agency_webhook_attestation_is_an_accepted_alternative_to_a_seen_webhook(database):
    store, account, cfg = prepared(database, webhooks=True)
    wire = Provider(cfg)
    try:
        read_everything(store, database, account, wire)
        assert hist().fan_coverage(store, TENANT, account, FAN)["state"] == "complete"
        assert view(store, account)["webhooks"] == {"attested": True, "seenSinceBoundary": False}
        # a webhook that predates the import boundary is not evidence about the interval
        see_webhook(database, account, at=utcnow() - timedelta(days=3))
        assert view(store, account)["webhooks"]["seenSinceBoundary"] is False
    finally:
        store.close()


def test_elapsed_time_alone_never_changes_coverage(database):
    store, account, cfg = prepared(database, webhooks=False)
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([fan_message(7101, FAN)], next_url(cfg, FAN, 7100))]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        run(store, account, wire)
        before = (view(store, account)["state"], hist().fan_coverage(store, TENANT, account, FAN)["state"])
        for table in ("of_fans_import_source", "of_fans_history_cursor"):   # thirty days pass
            move(database, account, table, updated_at=utcnow() - timedelta(days=30), created_at=utcnow() - timedelta(days=30))
        move(database, account, "of_fans_import_source", boundary_at=utcnow() - timedelta(days=30), last_read_at=utcnow() - timedelta(days=30))
        after = (view(store, account)["state"], hist().fan_coverage(store, TENANT, account, FAN)["state"])
        assert before == after == ("partial", "partial")
        assert hist().fan_coverage(store, TENANT, account, FAN)["state"] != "complete"
    finally:
        store.close()


def test_a_stored_label_or_a_forged_cursor_is_never_trusted(database):
    store, account, cfg = prepared(database)
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        move(database, account, "of_fans_import_source", state="complete", completed_at=utcnow())
        with database[1].begin() as c:
            c.execute(text("UPDATE of_fans_history_cursor SET status='complete', completed_at=now() WHERE account_id=:a"), {"a": account})
        shown = view(store, account)
        assert {n: v["state"] for n, v in shown["sources"].items() if n != "vault_window"} == {
            "chats": "unknown", "transactions": "unknown", "chargebacks": "unknown"}, "stored state labels are only a mirror"
        forged = hist().fan_coverage(store, TENANT, account, FAN)
        assert forged["state"] != "complete" and "history_incomplete" in forged["reasons"], "a cursor with no page read is not at the end of anything"
    finally:
        store.close()


def test_an_open_gap_blocks_completion_for_that_fan_only_and_resolution_restores_it(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([fan_message(7201, FAN), fan_message(7202, FAN, price=3)])]
    wire.chats[OTHER] = [chat_page([fan_message(7301, OTHER)])]
    try:
        read_everything(store, database, account, wire, fans=(FAN, OTHER))
        assert hist().fan_coverage(store, TENANT, account, OTHER)["state"] == "complete"
        blocked = hist().fan_coverage(store, TENANT, account, FAN)
        assert blocked["state"] == "partial" and blocked["reasons"] == ["row_gap_open"]
        gap = gaps_of(database, account, kind="row")[0]
        assert view(store, account)["gaps"] == {"openPages": 0, "openRows": 1, "exhausted": 0}
        done = hist().dismiss_row_gap(store, TENANT, account, gap.gap_id, command_key=key(31), actor=ACTOR, reason="Reviewed: fan tip row")
        assert done["outcome"] == "reviewed"
        assert hist().dismiss_row_gap(store, TENANT, account, gap.gap_id, command_key=key(31), actor=ACTOR, reason="Reviewed: fan tip row")["replayed"] is True
        with pytest.raises(Refused):
            hist().dismiss_row_gap(store, TENANT, account, gap.gap_id, command_key=key(32), actor=ACTOR, reason="Again")
        assert hist().fan_coverage(store, TENANT, account, FAN)["state"] == "complete"
        kept = gaps_of(database, account, kind="row")[0]
        assert kept.resolved_by == ACTOR and kept.resolution == "reviewed", "the quarantined row and its review stay on record"
    finally:
        store.close()


def test_a_failed_page_blocks_completion_until_its_own_retry_succeeds(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [lambda: answer(200, {"data": "nope"}), chat_page([fan_message(7401, FAN)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        drive(store, account, wire, lambda: at_end(database, account) and gaps_of(database, account, kind="page"))
        shown = hist().fan_coverage(store, TENANT, account, FAN)
        assert shown["state"] == "partial" and "history_gapped" in shown["reasons"] and "page_gap_open" in shown["reasons"]
        assert view(store, account)["gaps"] == {"openPages": 1, "openRows": 0, "exhausted": 1}
        gap = gaps_of(database, account, kind="page")[0]
        with pytest.raises(Refused):                      # a page cannot be waved through; only a successful read closes it
            hist().dismiss_row_gap(store, TENANT, account, gap.gap_id, command_key=key(41), actor=ACTOR, reason="Skip it")
        hist().retry_gap(store, TENANT, account, gap.gap_id, command_key=key(42), actor=ACTOR, reason="Integration reviewed")
        drive(store, account, wire, lambda: cursor_of(database, account).status == "complete")
        assert hist().fan_coverage(store, TENANT, account, FAN)["state"] == "complete"
    finally:
        store.close()


def test_the_chat_source_is_never_complete_while_the_fan_universe_is_unenumerated(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    try:
        read_everything(store, database, account, wire)
        shown = view(store, account)
        assert shown["sources"]["chats"]["state"] == "partial" and "universe_unenumerated" in shown["sources"]["chats"]["reasons"]
        assert shown["sources"]["transactions"]["state"] == "complete" and shown["fans"]["complete"] == 1
        assert shown["state"] == "partial", "no verified chat-list call exists, so account-level history is never claimed complete"
        view_ = prep.view(store, TENANT, account)
        assert view_["historyCoverage"] == "partial" and view_["ownershipCoverage"] == "partial"
        assert hist().fan_coverage(store, TENANT, account, 999)["state"] == "unknown", "a fan nobody listed is unknown, not clear"
        assert hist().fan_coverage(store, TENANT, account, 999)["reasons"] == ["fan_not_listed"]
    finally:
        store.close()


def test_the_vault_window_is_reported_separately_and_is_not_history_coverage(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    try:
        run(store, account, wire)
        shown = view(store, account)
        assert shown["sources"]["vault_window"]["state"] == "complete" and shown["sources"]["vault_window"]["selected"] == 1
        assert hist().fan_coverage(store, TENANT, account, FAN)["state"] == "unknown"
    finally:
        store.close()


def test_reconcile_counts_webhook_then_page_page_then_webhook_conflicts_and_late_rows(database):
    from fans_api_adapter.events import FansEventIntake
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.tx = [tx_page([tx_row("t1", FAN), tx_row("t2", FAN, amount=20.0), tx_row("t3", FAN, created="2999-01-01T00:00:00Z")])]
    try:
        move(database, account, "of_fans_import_source", next_read_at=utcnow() + timedelta(hours=1))   # bind, but read no history yet
        run(store, account, wire)
        move(database, account, "of_fans_import_source", next_read_at=None)
        intake = FansEventIntake(store)
        assert intake.ingest_transaction_row(account, tx_row("t1", FAN), source="webhook")[1] == "transaction_new"      # webhook first
        assert intake.ingest_transaction_row(account, tx_row("t2", FAN, amount=99.0), source="webhook")[1] == "transaction_new"
        hist().enlist_fan(store, account, FAN, priority=3)
        drive(store, account, wire, lambda: json_cursor(source_of(database, account, "transactions"))["end_reached"])
        tx = source_of(database, account, "transactions")
        assert (tx.rows_seen, tx.rows_new, tx.rows_duplicate, tx.rows_conflict, tx.rows_late) == (3, 1, 1, 1, 1)
        assert tx.rows_seen == tx.rows_new + tx.rows_duplicate + tx.rows_conflict + tx.rows_ignored + tx.rows_quarantined
        from db.onlyfans_fans_intake_models import OfFansEvidence
        with Session(database[1]) as s:
            evidence = {e.dedupe_key: e for e in s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account))}
        assert set(evidence) == {"tx:t1", "tx:t2", "tx:t3"} and evidence["tx:t2"].status == "held", "one row per key; the conflict is held"
        # page first, webhook second: still one row, outcome duplicate
        assert intake.ingest_transaction_row(account, tx_row("t3", FAN, created="2999-01-01T00:00:00Z"), source="webhook")[1] == "transaction_duplicate"
        assert view(store, account)["reconcile"] == {"duplicates": 1, "conflicts": 1, "late": 1, "quarantined": 0, "heldProjections": 0}
    finally:
        store.close()


def test_a_held_provider_event_is_surfaced_not_ignored(database):
    from db.onlyfans_engine_models import OfFansWebhook
    from db.onlyfans_fans_intake_models import OfFansProjection
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    try:
        run(store, account, wire)
        see_webhook(database, account)
        with Session(database[1]) as s, s.begin():
            s.add(OfFansProjection(account_id=account, epoch=1, event_key="evt-1", status="held", detail="tips_received_material", attempts=1, updated_at=utcnow()))
        assert view(store, account)["reconcile"]["heldProjections"] == 1
    finally:
        store.close()


def test_measurement_is_persisted_per_account_with_requests_credits_rate_and_completeness(database):
    store, account, cfg = prepared(database)
    other_store, other, other_cfg = prepared(database, 2)
    wire, other_wire = Provider(cfg), Provider(other_cfg)
    wire.chats[FAN] = [fail(429, {"Retry-After": "60", "x-ofapi-credits-balance": "990"}),
                       lambda: answer(200, {"data": [fan_message(7501, FAN)], "_pagination": {"next_page": None}}, {"x-ofapi-credits-balance": "985"})]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        drive(store, account, wire, lambda: wire.count("chats") >= 1 and at_end(database, account))
        move(database, account, "of_fans_history_cursor", retry_not_before=None)
        drive(store, account, wire, lambda: cursor_of(database, account).status == "complete")
        measured = view(store, account)["measurement"]
        chats = measured["sources"]["chats"]
        assert (chats["requests"], chats["ok"], chats["rateLimited"], chats["failed"], chats["dispatchedUnrecorded"]) == (2, 1, 1, 0, 0)
        assert chats["creditsReserved"] == 4 and chats["rowsRead"] == 1
        assert (chats["balanceFirst"], chats["balanceLast"]) == (990, 985)
        assert measured["sources"]["vault_window"]["requests"] == 1 and measured["sources"]["transactions"]["ok"] == 1
        assert measured["total"]["requests"] == sum(v["requests"] for v in measured["sources"].values())
        assert measured["requestsPerHour"] >= measured["total"]["requests"] / 2
        assert measured["completeness"] == {"complete": 3, "partial": 1, "unknown": 0}
        assert view(other_store, other)["measurement"]["total"]["requests"] == 0, "measurement is per account"
        # a request that was reserved but whose outcome was never recorded is visible, not hidden
        prep.PreparationRequestGuard(store, TENANT, account, 1)("GET", "https://api.fansapi.com/api/%s/transactions?limit=100&startDate=2016-01-01+00%%3A00%%3A00" % cfg["provider_account"], True)
        assert view(store, account)["measurement"]["sources"]["transactions"]["dispatchedUnrecorded"] == 1
    finally:
        store.close(); other_store.close()


def test_the_view_never_contains_provider_bodies_tokens_or_message_text(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([fan_message(7601, FAN, "my secret password is hunter2"), "garbage-row-body"])]
    try:
        read_everything(store, database, account, wire)
        dumped = json.dumps(prep.view(store, TENANT, account)) + json.dumps(view(store, account))
        for leak in ("hunter2", "garbage-row-body", "synthetic-preparation", "Bearer"):
            assert leak not in dumped
    finally:
        store.close()
