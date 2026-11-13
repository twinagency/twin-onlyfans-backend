"""W6HR-1: the newest fan row the reader skipped (tip/unsent/deleted) is new input, once.

Pages come from the real connector parse; every production page-commit site
runs on disposable PostgreSQL with synthetic data only. No provider I/O.
"""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from onlyfans_native_protocol_fixture import native_message
from test_onlyfans_foundation_pg import database, due
from test_onlyfans_launch_provider_budget_pg import native
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, current, prepare
from test_onlyfans_vault_response_limits import OWNER
from test_worker_read_resilience import wire
from db.models import AccountAiConfig, Message, OfConversationState
from db.onlyfans_models import OfRecovery, OfRuntime, OfWork
from onlyfans_runtime.launch_history_memory.recovery import HistoryRecovery
from onlyfans_runtime.recent_inbox import INTERVAL_SECONDS, poll_recent
from onlyfans_runtime.native_planning import NativeTextPlanner, SyntheticNativeModel
from onlyfans_runtime.store import PAGE_LIMIT, Store, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport, TextAuthority
from onlyfans_runtime.worker import ChatPage, read_step


def fan(mid, **flags):
    return {**native_message(mid, owner=OWNER), **flags}


def out(mid, **flags):
    return {**native_message(mid, owner=OWNER, outgoing=True), **flags}


def tip(mid):
    return fan(mid, isTip=True)


# Newest fan row is a tip above high_water 20; kept rows are already stored.
ABOVE = [tip(30), fan(20), fan(10)]
# A fan tip AT high_water and a creator's own unsent row above it: no input.
CONTROL = [out(31, isUnsent=True), tip(20), fan(10)]


class Reader:
    """Plain (unbudgeted) reader; every page is the real connector's parse."""
    account_id = ACCOUNT

    def __init__(self, connector):
        self.connector = connector

    def messages(self, **kw):
        return self.connector.messages(**kw)


def marks(engine, account=ACCOUNT):
    """(input_revision, stored messages): a skipped-row bump is a revision change no new Message explains."""
    with Session(engine) as s:
        state = s.get(OfConversationState, (account, FAN))
        return (state.input_revision if state else 0,
                s.scalar(select(func.count()).select_from(Message).where(Message.account_id == account)))


def bump(engine, run, account=ACCOUNT):
    before = marks(engine, account)
    run()
    after = marks(engine, account)
    return (after[0] - before[0]) - (after[1] - before[1])


def recovery_row(engine, high_water, *, account=ACCOUNT, status="needed"):
    with Session(engine) as s, s.begin():
        row = s.get(OfRecovery, (account, FAN)) or OfRecovery(account_id=account, fan_id=FAN, high_water=high_water)
        row.status = status
        if high_water or status == "complete":
            row.last_complete_at = row.last_complete_at or utcnow()
        s.add(row)


def sends(authority, engine, intent, account=ACCOUNT):
    current(engine, account=account)  # history current: only the input fence is left to decide
    transport = SyntheticTextTransport(account)
    return authority.dispatch(TENANT, account, intent, transport), len(transport.calls)


def recovery(store, engine, reader, high_water):
    recovery_row(engine, high_water)
    read_step(store, TENANT, ACCOUNT, reader)


def recent_cutover(store, engine, reader, high_water):
    recovery_row(engine, high_water)
    read_step(store, TENANT, ACCOUNT, reader, recent_cutover=True)


def archive(store, engine, reader, high_water):
    recovery_row(engine, high_water, status="complete")
    history = HistoryRecovery(store)
    history.request_fan(TENANT, ACCOUNT, FAN)
    history.step(TENANT, ACCOUNT, reader, archive_only=True)


SITES = {"recovery_page": recovery, "recent_cutover_catchup": recent_cutover,
         "recent_cutover_initial": recent_cutover, "archive_page": archive}


@pytest.mark.parametrize("site,rows,bumped", [(site, rows, bumped) for site in list(SITES)[:3]
    for rows, bumped in ((ABOVE, True), (CONTROL, False)) if bumped or site != "recent_cutover_initial"])
def test_skipped_fan_row_above_high_water_supersedes_stale_draft(database, wire, site, rows, bumped):
    connector, state = wire
    store, authority = configured(database)
    engine = database[1]
    high_water = 20
    if site == "recent_cutover_initial":
        rows = [row for row in rows if row["isTip"]]  # kept rows would import as input themselves
        high_water = 0  # initial head: every fan row is above it, so no control case
    state["rows"], state["more"] = rows, False
    intent = prepare(authority, body="Draft written before the fan's newest action")
    delta = bump(engine, lambda: SITES[site](store, engine, Reader(connector), high_water))
    assert (delta, sends(authority, engine, intent)) == ((1, ("cancelled", 0)) if bumped else (0, ("accepted", 1)))
    store.close()


@pytest.mark.parametrize("site,rows,high_water,bumped", [
    ("recovery_page", ABOVE, 20, 1),
    ("recovery_page", [fan(30, isUnsent=True), fan(20)], 20, 1),
    ("recovery_page", [fan(30, isDeleted=True), fan(20)], 20, 1),
    ("recovery_page", [out(30, isTip=True), fan(20)], 20, 0),  # creator-authored: not fan input
    ("recovery_page", [out(100), tip(50), fan(20)], 20, 0),  # newer kept row: the tip is not newest
    ("recent_cutover_initial", [out(100), tip(50), fan(20)], 0, 0),
    ("recent_cutover_initial", [tip(30), fan(20), out(10)], 0, 1),
    ("archive_page", [tip(10), fan(5)], 0, 0),  # archive/lifetime pages never bump
    ("archive_page", [tip(50), fan(40)], 100, 0),
])
def test_skipped_row_bumps_once_and_only_when_newest(database, wire, site, rows, high_water, bumped):
    connector, state = wire
    store, _ = configured(database)
    engine = database[1]
    state["rows"], state["more"] = rows, False
    run = lambda: SITES[site](store, engine, Reader(connector), high_water)
    assert [bump(engine, run), bump(engine, run)] == [bumped, 0]  # the same rows re-read: no further bump
    store.close()


# Second poll: a counted tip head is present (0 reads); a creator-authored skipped head is still re-read (W6HR-2b).
@pytest.mark.parametrize("rows,bumped,reads", [([tip(30), fan(20)], 1, 0), (CONTROL, 0, 1)], ids=["above", "at_or_below"])
def test_recent_inbox_counts_a_tip_head_once(native, wire, monkeypatch, rows, bumped, reads):
    store, account, reader = native
    connector, state = wire
    state["rows"], state["more"] = rows, False
    recovery_row(store.engine, 20, account=account, status="complete")
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([FAN], None, {FAN: rows[0]["id"]}))
    calls = []
    monkeypatch.setattr(reader, "messages", lambda **kw: calls.append(kw) or connector.messages(**kw))
    poll = lambda now: lambda: poll_recent(store, TENANT, account, reader, connection_revision=1, clock=lambda: now)
    assert bump(store.engine, poll(100), account) == bumped
    with Session(store.engine) as s, s.begin():
        s.get(OfRuntime, account).enabled = True  # a verified paused account only imports
    current(store.engine, account=account)
    authority = TextAuthority(store)
    assert authority.configure(TENANT, account, expected_revision=0, enabled=True, limit_units=20,
                               throttle_seconds=1, actor="synthetic-operator", reason="Offline acceptance") == 1
    intent = authority.prepare(TENANT, account, FAN, command_key="after-tip", expected_revision=0, body="Queued after",
                               origin="manual", actor="synthetic-operator", work=None, ttl_seconds=300)
    calls.clear()
    assert (bump(store.engine, poll(100 + INTERVAL_SECONDS + 1), account), len(calls)) == (0, reads)
    assert sends(authority, store.engine, intent, account) == ("accepted", 1)


@pytest.mark.parametrize("text,rows,target", [
    (20, lambda: [tip(30), fan(20)], 20),  # text then tip: the reply is re-planned for the text, once
    (100, lambda: [tip(102), fan(101), fan(100)], 101),  # FL2-01: the same page's fan text is the target
    (20, lambda: [tip(30), out(25), fan(20)], None),  # answered on the page: input moves, nothing re-planned
], ids=["text_then_tip", "same_page_text", "answered_cancels_only"])
def test_tip_replans_only_the_newest_unanswered_text(database, wire, monkeypatch, text, rows, target):
    connector, state = wire
    monkeypatch.setenv("DATABASE_URL", database[0])  # never whatever the shell points at
    monkeypatch.setattr(Store, "_read_connection", staticmethod(lambda *a: None))  # import path; fence not under test
    store, _ = configured(database, mode="automated")
    engine = database[1]
    state["rows"], state["more"] = [fan(text)], False
    recovery(store, engine, Reader(connector), 10)
    store.drain(TENANT, ACCOUNT)
    with Session(engine) as s, s.begin():
        s.get(OfWork, (ACCOUNT, FAN)).status = "done"  # planned for the text, never answered
        s.add(AccountAiConfig(account_id=ACCOUNT, model="grok-4-1-fast-non-reasoning", persona="Synthetic Creator."))
    state["rows"], work = rows(), []  # rows built after the stored text, so createdAt orders like the ids

    def read():
        recovery_row(engine, text)
        read_step(store, TENANT, ACCOUNT, Reader(connector), connection_revision=1)
        store.drain(TENANT, ACCOUNT, connection_revision=1)
        with Session(engine) as s:
            row = s.get(OfWork, (ACCOUNT, FAN))
            work.append((row.status, row.target_message_id, row.generation))
    assert [bump(engine, read), bump(engine, read)] == [1, 0] and work[1] == work[0]  # counted and re-armed once
    assert work[0][:2] == (("waiting", target) if target else ("done", text))
    due(engine)
    current(engine, mode="automated")
    claim = store.claim(TENANT)
    assert (claim and claim.target_message_id) == target
    model = SyntheticNativeModel("Thank you, that made my day.")
    if claim is not None:  # planning passes "input moved on" and reaches the model
        assert NativeTextPlanner(store).prepare(TENANT, claim, model) and len(model.calls) == 1
    store.close()


def test_upper_bound_floors_a_later_page_of_the_same_recovery(database, wire):
    connector, state = wire
    store, _ = configured(database)
    recovery_row(database[1], 10)
    def pages():  # no drain between pages: page 1's rows are ingress only, so upper_bound alone covers them
        for state["rows"], state["more"] in (([fan(40), fan(30)], True), ([tip(25), fan(20)], False)):
            claim = store.claim_recovery(TENANT, ACCOUNT)
            page = connector.messages(fan_id=FAN, before_id=claim.before_id, limit=PAGE_LIMIT)
            store.recovery_page(TENANT, claim, page.events, has_more=page.has_more, next_before=page.next_before, skipped_inbound=page.skipped_inbound)
    assert bump(database[1], pages) == 0
    store.close()
