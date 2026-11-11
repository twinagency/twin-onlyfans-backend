"""The history import runner behind the preparation worker.

Real PostgreSQL, mocked wire. Counterexamples first: a malformed row used to fail a whole run and lose its
progress, a rate-limited history page had no persisted due time, and nothing could read history at all.
"""
from datetime import timedelta
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_commercial_preparation_pg import TENANT, ACTOR
from history_support import (BASE, Provider, answer, card, cb_page, cb_row, chat_page, creator_message, fail, fan_message, key,
                             next_url, prepared, rows, run, tx_page, tx_row)
from conversation_engine import preparation as prep
from onlyfans_runtime.store import Refused, Store, utcnow

FAN = 401


def models():
    from db import onlyfans_history_models as m
    return m


def hist():
    from fans_api_adapter import history_import
    return history_import


def cursor_of(database, account, fan=FAN):
    return rows(database, models().OfFansHistoryCursor, account, models().OfFansHistoryCursor.fan_id == fan)[0]


def source_of(database, account, source):
    return [r for r in rows(database, models().OfFansImportSource, account) if r.source == source][0]


def gaps_of(database, account, **where):
    return [g for g in rows(database, models().OfFansImportGap, account) if all(getattr(g, k) == v for k, v in where.items())]


def move(database, account, table, **values):
    """Make a persisted due time due: tests move the stored time, never an in-process clock."""
    sets = ",".join(f"{k}=:{k}" for k in values)
    with database[1].begin() as c:
        c.execute(text(f"UPDATE {table} SET {sets} WHERE account_id=:a"), {"a": account, **values})


def due_now(database, account):
    past = utcnow() - timedelta(seconds=5)
    move(database, account, "of_fans_history_cursor", retry_not_before=None)
    move(database, account, "of_fans_import_source", next_read_at=None)


def finish_vault(store, account, provider):
    """First pass: identity, the (single page) vault window and whatever history is already eligible."""
    result = run(store, account, provider)
    assert result["vault"]["complete"]
    return result


def drive(store, account, provider, until, limit=12):
    """Run passes until a persisted condition holds. Never a clock: only stored state decides."""
    for _ in range(limit):
        if until():
            return
        run(store, account, provider)
    assert until(), "condition not reached within the pass limit"


def invariant(row):
    return row.rows_seen == row.rows_new + row.rows_duplicate + row.rows_conflict + row.rows_ignored + row.rows_quarantined


# ---- nothing could read history before ------------------------------------------------------------------

def test_a_listed_fan_is_read_through_the_preparation_guard_and_ingested(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([fan_message(9001, FAN, "hey"), card(9002, cfg, [111, 112], 1500, previews=[111], opened=True)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        result = finish_vault(store, account, wire)
        assert result["state"] == "partial", "transactions and chargebacks are still unread"
        assert wire.count("chats") == 1 and wire.kinds("chats")[0][2] == {"limit": "100", "order": "desc"}
        cur = cursor_of(database, account)
        assert (cur.status, cur.pages, cur.messages_seen, cur.next_page) == ("complete", 1, 2, None)
        assert cur.first_page_at is not None and cur.completed_at is not None and cur.newest_message_id == 9002
        src = source_of(database, account, "chats")
        assert (src.pages_ok, src.rows_seen, src.rows_new, src.rows_quarantined) == (1, 2, 2, 0) and invariant(src)
        assert src.requests_used == 1
        assert prep.view(store, TENANT, account)["providerRequestsUsed"] == 3   # identity, vault page, chats page
        from db.models import Message
        with Session(database[1]) as s:
            assert {m.message_id for m in s.scalars(select(Message).where(Message.account_id == account))} == {9001, 9002}
    finally:
        store.close()


def test_reads_are_get_only_and_nothing_is_sent_or_modelled(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    methods = []

    def spy(req):
        methods.append(req.method)
        return wire(req)
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        for _ in range(4):
            run(store, account, spy)
        assert set(methods) == {"GET"} and len(methods) > 4
        assert {c[0] for c in wire.calls} == {"identity", "vault", "chats", "transactions", "chargebacks"}
    finally:
        store.close()


# ---- quarantine instead of aborting ---------------------------------------------------------------------

def test_a_malformed_row_is_quarantined_and_the_page_completes(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    bad_price = fan_message(9102, FAN, "tip?", price=5)                      # a fan message with a price: refused by intake
    no_direction = {"id": 9103, "text": "x", "createdAt": "2026-10-01T10:02:00Z", "media": []}   # neither side proven
    not_an_object = "garbage"
    wire.chats[FAN] = [chat_page([fan_message(9101, FAN), bad_price, no_direction, not_an_object, fan_message(9104, FAN, "last")])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        finish_vault(store, account, wire)
        cur = cursor_of(database, account)
        assert cur.status == "complete" and cur.pages == 1 and cur.messages_seen == 5, "the page completed despite three bad rows"
        src = source_of(database, account, "chats")
        assert (src.rows_seen, src.rows_new, src.rows_quarantined, src.pages_ok, src.pages_failed) == (5, 2, 3, 1, 0) and invariant(src)
        quarantined = gaps_of(database, account, kind="row")
        assert len(quarantined) == 3 and all(g.source == "chats" and g.fan_id == FAN and g.resolved_at is None for g in quarantined)
        assert {g.reason_code for g in quarantined} == {"row_refused", "row_malformed"}
        assert all(g.raw_sha256 and len(g.raw_sha256) == 64 and g.exhausted is False for g in quarantined)
        for g in quarantined:                               # hashes and fixed codes only: never a provider body
            assert "garbage" not in repr((g.detail, g.cursor_ref, g.reason_code))
        from db.models import Message
        with Session(database[1]) as s:
            assert {m.message_id for m in s.scalars(select(Message).where(Message.account_id == account))} == {9101, 9104}
    finally:
        store.close()


def test_a_resumed_import_continues_at_the_saved_cursor_not_page_one(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    second = next_url(cfg, FAN, 9200)
    wire.chats[FAN] = [chat_page([fan_message(9301, FAN), fan_message(9302, FAN, price=7)], second),
                       chat_page([fan_message(9201, FAN)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        finish_vault(store, account, wire)
        cur = cursor_of(database, account)
        assert cur.status == "running" and cur.next_page == second and cur.pages == 1
        assert len(gaps_of(database, account, kind="row")) == 1
        store.close()                                       # a process restart
        store = Store(database[0])
        drive(store, account, wire, lambda: cursor_of(database, account).status == "complete")
        chats = wire.kinds("chats")
        assert len(chats) == 2 and chats[0][2] == {"limit": "100", "order": "desc"} and chats[1][2].get("last_id") == "9200", "page one was not re-read"
        cur = cursor_of(database, account)
        assert cur.status == "complete" and cur.pages == 2
        assert len(gaps_of(database, account, kind="row")) == 1, "the quarantined row was kept, not repeated"
    finally:
        store.close()


def test_malformed_transaction_and_chargeback_rows_are_quarantined_not_fatal(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.tx = [tx_page([tx_row("t1", FAN), tx_row("t2", FAN, amount="free"), "oops", {"amount": 3}, tx_row("t3", 402, 20.0),
                        {**tx_row("t4", FAN), "type": "subscription"}])]
    wire.cb = [cb_page([cb_row("t1", FAN), {"payment": "broken"}])]
    try:
        finish_vault(store, account, wire)
        drive(store, account, wire, lambda: json_cursor(source_of(database, account, "chargebacks"))["end_reached"] and json_cursor(source_of(database, account, "transactions"))["end_reached"])
        tx = source_of(database, account, "transactions")
        assert (tx.rows_seen, tx.rows_new, tx.rows_ignored, tx.rows_quarantined, tx.pages_ok) == (6, 2, 1, 3, 1) and invariant(tx)
        assert json_cursor(tx)["end_reached"] is True
        cb = source_of(database, account, "chargebacks")
        assert (cb.rows_seen, cb.rows_new, cb.rows_quarantined) == (2, 1, 1) and invariant(cb)
        assert {(g.source, g.kind) for g in gaps_of(database, account)} == {("transactions", "row"), ("chargebacks", "row")}
        from db.onlyfans_fans_intake_models import OfFansEvidence
        with Session(database[1]) as s:
            kinds = sorted((e.kind, e.dedupe_key) for e in s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account)))
        assert kinds == [("chargeback", "cb:t1"), ("transaction", "tx:t1"), ("transaction", "tx:t3")]
    finally:
        store.close()


def json_cursor(row):
    import json
    return json.loads(row.cursor_json)


def test_transactions_and_chargebacks_paginate_to_the_provider_end(database, monkeypatch):
    hist_module = hist()
    monkeypatch.setattr(hist_module, "PAGE_SIZE", 2)
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.tx = [tx_page([tx_row("t1", FAN), tx_row("t2", FAN)], "m2"), tx_page([tx_row("t3", FAN)])]
    wire.cb = [cb_page([cb_row("t1", FAN), cb_row("t2", FAN)]), cb_page([cb_row("t3", FAN)])]
    try:
        finish_vault(store, account, wire)
        drive(store, account, wire, lambda: all(json_cursor(source_of(database, account, n))["end_reached"] for n in ("transactions", "chargebacks")))
        tx, cb = source_of(database, account, "transactions"), source_of(database, account, "chargebacks")
        assert json_cursor(tx)["end_reached"] is True and json_cursor(tx)["marker"] is None
        assert json_cursor(cb)["end_reached"] is True and json_cursor(cb)["offset"] == 2
        assert [c[2].get("marker") for c in wire.kinds("transactions")] == [None, "m2"]
        assert [c[2].get("offset") for c in wire.kinds("chargebacks")] == ["0", "2"]
        assert all(c[2]["startDate"] == "2016-01-01 00:00:00" for c in wire.kinds("transactions"))
        assert (tx.pages_ok, tx.rows_new) == (2, 3) and (cb.pages_ok, cb.rows_new) == (2, 3)
        before = len(wire.calls)
        assert run(store, account, wire)["state"] == "complete"
        assert len(wire.calls) == before, "a finished import performs no provider read, not even identity"
    finally:
        store.close()


# ---- classified failures, persisted due time, retained pages -----------------------------------------------

@pytest.mark.parametrize("read", ["chats", "transactions", "chargebacks"])
def test_429_with_retry_after_persists_a_due_time_and_makes_no_get_until_due(database, read):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [fail(429, {"Retry-After": "120"}), chat_page([fan_message(9401, FAN)])]
    wire.tx = [fail(429, {"Retry-After": "120"}), tx_page([tx_row("t1", FAN)])]
    wire.cb = [fail(429, {"Retry-After": "120"}), cb_page([])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0 if read == "chats" else 3)
        reads = lambda: wire.count(read)
        drive(store, account, wire, reads)
        assert reads() == 1
        pages = gaps_of(database, account, kind="page", source=read)
        assert len(pages) == 1 and pages[0].reason_code == "provider_rate_limited" and pages[0].http_status == 429
        assert pages[0].attempts == 1 and pages[0].exhausted is False and pages[0].resolved_at is None
        if read == "chats":
            cur = cursor_of(database, account)
            assert (cur.status, cur.next_page, cur.retry_attempts, cur.pages) == ("needed", None, 1, 0)
            assert 115 <= (cur.retry_not_before - utcnow()).total_seconds() <= 121
        else:
            src = source_of(database, account, read)
            assert src.retry_attempts == 1 and 115 <= (src.next_read_at - utcnow()).total_seconds() <= 121
            assert json_cursor(src)["end_reached"] is False
        for _ in range(3):                                     # let every other source finish first
            run(store, account, wire)
        calls = len(wire.calls)
        for _ in range(3):
            run(store, account, wire)
        later = [c for c in wire.calls[calls:] if c[0] == read]
        assert later == [], "no read of the rate-limited source before its persisted due time"
        # a restart keeps the same obligation
        store.close()
        store = Store(database[0])
        run(store, account, wire)
        assert [c for c in wire.calls[calls:] if c[0] == read] == []
        due_now(database, account)
        drive(store, account, wire, lambda: reads() == 2)
        page = gaps_of(database, account, kind="page", source=read)[0]
        assert page.resolved_at is not None and page.resolved_by == "retry_succeeded", "the failed page is kept and resolved by its own retry"
    finally:
        store.close()


def test_a_missing_retry_after_uses_the_same_bounded_backoff_as_the_vault(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [fail(503)]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        seen = []
        for _ in range(4):
            before = wire.count("chats")
            drive(store, account, wire, lambda: wire.count("chats") > before)
            cur = cursor_of(database, account)
            seen.append((cur.retry_attempts, round((cur.retry_not_before - utcnow()).total_seconds() / 10) * 10))
            due_now(database, account)
        assert seen == [(1, 30), (2, 60), (3, 120), (4, 240)]
    finally:
        store.close()


def test_five_transient_failures_leave_an_exhausted_retained_gap_and_stop_polling(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [fail(500)]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        for attempt in range(1, 6):
            drive(store, account, wire, lambda: wire.count("chats") >= attempt)
            due_now(database, account)
        gap = gaps_of(database, account, kind="page", source="chats")[0]
        assert (gap.attempts, gap.exhausted, gap.reason_code) == (5, True, "provider_unavailable") and gap.resolved_at is None
        cur = cursor_of(database, account)
        assert cur.status == "gapped" and cur.next_page is None and cur.pages == 0, "the cursor was never advanced past the failed page"
        calls = len(wire.calls)
        for _ in range(3):
            run(store, account, wire)
        assert [c for c in wire.calls[calls:] if c[0] == "chats"] == []
        # only an explicit operator command re-opens it
        wire.chats[FAN] = [chat_page([fan_message(9501, FAN)])]
        first = hist().retry_gap(store, TENANT, account, gap.gap_id, command_key=key(61), actor=ACTOR, reason="Provider recovered")
        assert first["replayed"] is False
        again = hist().retry_gap(store, TENANT, account, gap.gap_id, command_key=key(61), actor=ACTOR, reason="Provider recovered")
        assert again["replayed"] is True
        drive(store, account, wire, lambda: cursor_of(database, account).status == "complete")
        assert gaps_of(database, account, kind="page")[0].resolved_by == "retry_succeeded"
    finally:
        store.close()


def test_a_failed_page_survives_a_crash_between_the_get_and_the_commit(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    second = next_url(cfg, FAN, 9600)
    wire.chats[FAN] = [chat_page([fan_message(9601, FAN)], second), fail(502), chat_page([fan_message(9599, FAN)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        drive(store, account, wire, lambda: gaps_of(database, account, kind="page"))
        gap = gaps_of(database, account, kind="page")[0]
        assert gap.cursor_ref == second, "the failed page is identified by the continuation that must be retried"
        store.close(); store = Store(database[0])
        due_now(database, account)
        drive(store, account, wire, lambda: cursor_of(database, account).status == "complete")
        cur = cursor_of(database, account)
        assert cur.status == "complete" and cur.pages == 2
        assert wire.kinds("chats")[-1][2].get("last_id") == "9600"
    finally:
        store.close()


def test_auth_rejection_holds_the_account_until_a_new_operator_command(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [fail(403), chat_page([fan_message(9701, FAN)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        held = run(store, account, wire)
        assert held["state"] == "held" and held["history"]["failure"] == "auth"
        shown = prep.view(store, TENANT, account)
        assert shown["lastAttempt"]["outcome"] == "identity_failed" and shown["lastAttempt"]["cause"] == "provider_forbidden"
        gap = gaps_of(database, account, kind="page")[0]
        assert gap.reason_code == "provider_forbidden" and gap.exhausted is False and gap.attempts == 1
        calls = len(wire.calls)
        assert run(store, account, wire)["state"] == "held" and len(wire.calls) == calls
        prep.configure(store, TENANT, account, cfg, expected_revision=1, command_key=key(7), actor=ACTOR, reason="Credential replaced")
        drive(store, account, wire, lambda: cursor_of(database, account).status == "complete")
        assert gaps_of(database, account, kind="page")[0].resolved_by == "retry_succeeded", "the same page was retried after the explicit command"
    finally:
        store.close()


def test_a_schema_failure_is_retained_for_review_and_other_sources_continue(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [lambda: answer(200, {"data": "nope"})]
    wire.tx = [tx_page([tx_row("t1", FAN)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        drive(store, account, wire, lambda: json_cursor(source_of(database, account, "transactions"))["end_reached"])
        gap = gaps_of(database, account, kind="page", source="chats")[0]
        assert (gap.reason_code, gap.exhausted, gap.http_status) == ("provider_response_invalid", True, 200)
        assert cursor_of(database, account).status == "gapped"
        assert json_cursor(source_of(database, account, "transactions"))["end_reached"] is True
        assert wire.count("chats") == 1
        assert prep.view(store, TENANT, account)["lastAttempt"]["outcome"] not in ("identity_failed", "schema"), "a history schema gap holds one cursor, not the account"
    finally:
        store.close()


def test_a_spent_source_stops_without_a_gap_and_other_sources_keep_going(database):
    store, account, cfg = prepared(database, chats=(1, 2))
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([fan_message(9801, FAN)], next_url(cfg, FAN, 9800)), chat_page([fan_message(9799, FAN)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        drive(store, account, wire, lambda: json_cursor(source_of(database, account, "chargebacks"))["end_reached"] and
              json_cursor(source_of(database, account, "transactions"))["end_reached"] and source_of(database, account, "chats").last_error == "source_budget_exhausted")
        assert wire.count("chats") == 1
        assert gaps_of(database, account) == [], "an exhausted allowance is not a provider failure"
        src = source_of(database, account, "chats")
        assert src.last_error == "source_budget_exhausted" and src.requests_used == 1
        assert json_cursor(source_of(database, account, "transactions"))["end_reached"] is True
        from fans_api_adapter import history_import
        from history_support import spec
        history_import.configure(store, TENANT, account, spec(chats=(5, 10)), expected_revision=1,
                                 command_key=key(8), actor=ACTOR, reason="More allowance")
        assert source_of(database, account, "chats").last_error is None
        drive(store, account, wire, lambda: cursor_of(database, account).status == "complete")
    finally:
        store.close()


def test_min_interval_is_a_per_source_rate_limit(database):
    store, account, cfg = prepared(database, interval=3600)
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([fan_message(9901, FAN)], next_url(cfg, FAN, 9900)), chat_page([fan_message(9899, FAN)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        for _ in range(8):
            run(store, account, wire)
        assert wire.count("chats") == 1 and wire.count("transactions") == 1 and wire.count("chargebacks") == 1
        assert (source_of(database, account, "chats").next_read_at - utcnow()).total_seconds() > 3500
        calls = len(wire.calls)
        assert run(store, account, wire)["state"] == "delayed" and len(wire.calls) == calls, "pacing needs no identity read"
        move(database, account, "of_fans_import_source", next_read_at=utcnow() - timedelta(seconds=1))
        run(store, account, wire)
        assert wire.count("chats") == 2
    finally:
        store.close()


def test_a_live_fan_is_read_before_the_rest_and_sources_take_turns(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    try:
        hist().enlist_fan(store, account, 777, priority=3)       # a lifetime backlog fan
        hist().enlist_fan(store, account, 778, priority=2)       # a purchaser
        hist().enlist_fan(store, account, 779, priority=0)       # messaging right now
        for _ in range(6):
            run(store, account, wire)
        order = [c[0] + (":%s" % c[1] if c[0] == "chats" else "") for c in wire.calls if c[0] in ("chats", "transactions", "chargebacks")]
        assert order[0] == "chats:779", order
        assert set(order[:4]) >= {"transactions", "chargebacks"}, "a live fan does not starve the account-level sources"
        assert order.index("chats:779") < order.index("chats:778") < order.index("chats:777")
    finally:
        store.close()


def test_intake_backpressure_holds_the_cursor_without_burning_attempts(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([fan_message(9951, FAN)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        with database[1].begin() as c:
            c.execute(text("UPDATE of_runtime SET pending_events=1000 WHERE account_id=:a"), {"a": account})
        run(store, account, wire)
        cur = cursor_of(database, account)
        assert (cur.status, cur.pages, cur.retry_attempts, cur.last_error) == ("needed", 0, 0, "intake_backpressure")
        assert cur.retry_not_before is not None and gaps_of(database, account) == []
        with database[1].begin() as c:
            c.execute(text("UPDATE of_runtime SET pending_events=0 WHERE account_id=:a"), {"a": account})
        due_now(database, account)
        drive(store, account, wire, lambda: cursor_of(database, account).status == "complete")
    finally:
        store.close()


def test_a_stop_during_the_read_commits_nothing(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    inner_script = [chat_page([fan_message(9961, FAN)])]
    wire.chats[FAN] = inner_script

    def stopping(req):
        if "/chats/" in req.url.path:
            prep.stop(store, TENANT, account, expected_revision=1, command_key=key(9), actor=ACTOR, reason="Stop in flight")
        return wire(req)
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        run(store, account, stopping)
        cur = cursor_of(database, account)
        assert (cur.status, cur.pages, cur.next_page) == ("needed", 0, None)
        assert gaps_of(database, account) == []
        from db.models import Message
        with Session(database[1]) as s:
            assert s.scalars(select(Message).where(Message.account_id == account)).first() is None
        assert run(store, account, wire)["state"] == "stopped"
    finally:
        store.close()


# ---- accounts stay independent ---------------------------------------------------------------------------

def test_ten_accounts_with_identical_fan_and_message_ids_stay_isolated_and_one_held_account_does_not_starve_others(database):
    stores, wires = [], []
    try:
        for i in range(1, 11):
            store, account, cfg = prepared(database, i)
            wire = Provider(cfg)
            wire.chats[FAN] = [chat_page([fan_message(5001, FAN, "same ids everywhere"), card(5002, cfg, [111, 112], 1500, previews=[111], opened=True)])]
            wire.tx = [tx_page([tx_row("same-tx", FAN)])]
            if i == 1:
                wire.chats[FAN] = [fail(403)]
            if i == 2:
                wire.chats[FAN] = [fail(429, {"Retry-After": "600"})]
            stores.append((store, account)); wires.append(wire)
            hist().enlist_fan(store, account, FAN, priority=0)
        for _ in range(5):
            for (store, account), wire in zip(stores, wires):
                run(store, account, wire)
        by_account = {account: cursor_of(database, account) for _, account in stores}
        assert by_account["prep-1"].status == "needed" and by_account["prep-2"].status == "needed"
        assert {a: c.status for a, c in by_account.items() if a not in ("prep-1", "prep-2")} == {f"prep-{i}": "complete" for i in range(3, 11)}
        assert prep.view(stores[0][0], TENANT, "prep-1")["lastAttempt"]["outcome"] == "identity_failed"
        cur2 = cursor_of(database, "prep-2")
        assert cur2.retry_attempts == 1 and 590 <= (cur2.retry_not_before - utcnow()).total_seconds() <= 601
        from db.models import Message
        from db.onlyfans_fans_intake_models import OfFansEvidence
        with Session(database[1]) as s:
            for i in range(3, 11):
                account = f"prep-{i}"
                assert {m.message_id for m in s.scalars(select(Message).where(Message.account_id == account))} == {5001, 5002}
                assert [e.dedupe_key for e in s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account, OfFansEvidence.kind == "transaction"))] == ["tx:same-tx"]
        for i in range(1, 11):
            src = source_of(database, f"prep-{i}", "chats")
            assert src.requests_used == 1, (i, src.requests_used)
        assert len(gaps_of(database, "prep-1")) == 1 and len(gaps_of(database, "prep-2")) == 1 and gaps_of(database, "prep-3") == []
    finally:
        for store, _ in stores:
            store.close()


def test_prepare_one_reports_complete_only_when_nothing_automatic_is_left(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    try:
        first = run(store, account, wire)
        assert first["vault"]["complete"] and first["state"] == "partial", "an unfinished history import is not a finished preparation"
        for _ in range(4):
            last = run(store, account, wire)
        assert last["state"] == "complete"
        before = len(wire.calls)
        assert run(store, account, wire)["state"] == "complete" and len(wire.calls) == before
        # a fan who starts messaging later is picked up on demand
        from db.models import Fan
        with Session(database[1]) as s, s.begin():
            s.add(Fan(account_id=account, fan_id=555))
        wire.chats[555] = [chat_page([fan_message(5551, 555)])]
        for _ in range(3):
            run(store, account, wire)
        assert cursor_of(database, account, 555).status == "complete"
    finally:
        store.close()


def test_accounts_without_a_configured_import_behave_exactly_as_before(database):
    store, account, cfg = prepared(database, begin=False)
    wire = Provider(cfg)
    try:
        result = run(store, account, wire)
        assert result["state"] == "complete" and "history" not in result
        assert {c[0] for c in wire.calls} == {"identity", "vault"}
        assert prep.view(store, TENANT, account)["historyCoverage"] == "pending"
    finally:
        store.close()
