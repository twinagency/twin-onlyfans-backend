"""Real PostgreSQL proof using only disposable databases and synthetic history.

No live provider or model is contacted. Shared schema registration remains the
integration owner's work; this fixture installs proposed owned DDL in its own DB.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event, seed
from db.models import Fan, FanProfile, GrokCall, Message, OfCreatorBinding
from db.onlyfans_models import OfWork
from db.onlyfans_delivery_models import OfNativePlan, OfTextIntent
from onlyfans_read.agency_scope import AgencyScope, current_scope
from onlyfans_runtime.store import Store, Refused, utcnow
from onlyfans_runtime.launch_history_memory import memory as memory_module
from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory, augment_prompt, enrich_snapshot, locked_fields
from onlyfans_runtime.launch_history_memory.models import MemoryCorrection, MemoryEvidence, MemoryReceipt, MemoryState

TENANT, ACCOUNT, FAN = "tenant-a", "acc-a", 101
ACTOR = "op-session:" + "a" * 64
START = datetime(2025, 1, 1)


@pytest.fixture
def memory_db(database, monkeypatch):
    url, engine = database
    store = seed(url, engine)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    try:
        yield url, engine, store, HistoricalMemory(store)
    finally:
        store.close()


def insert_messages(engine, entries, account=ACCOUNT, fan=FAN):
    """entries=(id, body, timestamp offset minutes[, direction[, unsent]])."""
    with Session(engine) as session, session.begin():
        for entry in entries:
            mid, body, minute, *options = entry
            session.add(Message(account_id=account, fan_id=fan, message_id=mid,
                body=body, created_at=START + timedelta(minutes=minute),
                direction=options[0] if options else "in",
                is_unsent=options[1] if len(options) > 1 else False))


def drain_memory(memory, *, account=ACCOUNT, tenant=TENANT, fan=FAN, limit=100):
    processed = 0
    for _ in range(100):
        batch = memory.process(tenant, account, fan, limit=limit)["processed"]
        processed += batch
        if batch == 0:
            return processed
    pytest.fail("memory processing failed to converge within synthetic bound")


def correction(memory, **changes):
    values = dict(field="home_city", operation="set", value="Perth",
        expected_version=memory.read(TENANT, ACCOUNT, FAN)["version"],
        command_key="synthetic-correction", actor=ACTOR, reason="Verified operator correction")
    values.update(changes)
    return values


def assert_no_reply_or_model_rows(engine):
    with Session(engine) as session:
        for model in (OfWork, OfNativePlan, OfTextIntent, GrokCall):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_middle_of_large_history_reaches_memory_and_recent_hundred_stays_intact(memory_db):
    _, engine, store, memory = memory_db
    entries = [(mid, f"Synthetic ordinary interaction {mid}", mid) for mid in range(1, 1206)]
    entries[503] = (504, "I work as an astronomer.", 504)
    entries[612] = (613, "We discussed the rare blue comet at the observatory.", 613, "out")
    entries[-1] = (1205, "Can you recall our blue comet discussion?", 1205)
    insert_messages(engine, entries)
    initial = memory.progress(TENANT, ACCOUNT, FAN)
    assert (initial["storedMessages"], initial["pendingMessages"]) == (1205, 1205)
    assert initial["providerHistoryComplete"] is None and not initial["storedMemoryComplete"]
    initial_projection = memory.read(TENANT, ACCOUNT, FAN, query="blue comet")
    assert memory.process(TENANT, ACCOUNT, FAN, limit=100)["processed"] == 100
    after_empty_fact_batch = memory.read(TENANT, ACCOUNT, FAN, query="blue comet")
    assert after_empty_fact_batch["version"] != initial_projection["version"]
    assert after_empty_fact_batch["fingerprint"] == initial_projection["fingerprint"]
    assert memory.progress(TENANT, ACCOUNT, FAN)["pendingMessages"] == 1105
    assert drain_memory(memory) == 1105
    result = memory.read(TENANT, ACCOUNT, FAN, query="blue comet")
    assert result["facts"]["occupation"]["value"] == "astronomer"
    assert result["facts"]["occupation"]["sourceMessageId"] == "504"
    assert [row["sourceMessageId"] for row in result["interactions"]] == ["613"]
    with Session(engine) as session:
        store._runtime(session, TENANT, ACCOUNT)
        base = {"config": {"preserve": "configuration"}, "fan": {"preserve": "durable facts"}}
        enriched = enrich_snapshot(session, ACCOUNT, FAN, base)
        assert enriched["config"] == base["config"] and enriched["fan"] == base["fan"]
        assert "historicalMemory" not in base
        assert "revision" not in enriched["historicalMemory"] and "version" not in enriched["historicalMemory"]
        assert [row["sourceMessageId"] for row in enriched["historicalMemory"]["interactions"]] == ["613"]
        recent = session.scalars(select(Message).where(Message.account_id == ACCOUNT,
            Message.fan_id == FAN).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(100)).all()
        assert len(recent) == 100 and min(row.message_id for row in recent) == 1106
        prompt = [{"role": "user", "content": row.body} for row in reversed(recent)]
        enhanced = augment_prompt(prompt, result)
        assert enhanced[:-3] + [enhanced[-1]] == prompt
        assert session.get(Fan, (ACCOUNT, FAN)).occupation is None
    final = memory.progress(TENANT, ACCOUNT, FAN)
    assert final["storedMemoryComplete"] and final["processedCurrentMessages"] == 1205
    assert final["providerHistoryComplete"] is None
    assert_no_reply_or_model_rows(engine)


def test_restart_and_late_older_sources_are_processed_without_fact_regression(memory_db):
    url, engine, store, memory = memory_db
    insert_messages(engine, [(20, "I live in Sydney.", 200), (30, "I live in Perth.", 300)])
    assert memory.process(TENANT, ACCOUNT, FAN, limit=1)["processed"] == 1
    store.close()
    restarted_store = Store(url)
    try:
        restarted = HistoricalMemory(restarted_store)
        assert drain_memory(restarted) == 1
        before = restarted.read(TENANT, ACCOUNT, FAN)
        assert before["facts"]["home_city"]["value"] == "Perth"
        insert_messages(engine, [(1, "I live in Brisbane. My name is Sam.", 1)])
        assert restarted.progress(TENANT, ACCOUNT, FAN)["pendingMessages"] == 1
        assert drain_memory(restarted) == 1
        after = restarted.read(TENANT, ACCOUNT, FAN)
        assert after["facts"]["home_city"]["value"] == "Perth"
        assert after["facts"]["real_name"]["value"] == "Sam"
        assert after["fingerprint"] != before["fingerprint"]
        revision = after["revision"]
        assert restarted.process(TENANT, ACCOUNT, FAN)["revision"] == revision
    finally:
        restarted_store.close()
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MemoryReceipt)) == 3


def test_edit_and_retraction_hide_stale_evidence_immediately_and_reprocess(memory_db):
    _, engine, _, memory = memory_db
    insert_messages(engine, [(1, "I live in Brisbane.", 1), (2, "I live in Sydney.", 2)])
    drain_memory(memory)
    first = memory.read(TENANT, ACCOUNT, FAN)
    assert first["facts"]["home_city"]["value"] == "Sydney"
    with Session(engine) as session, session.begin():
        session.get(Message, (ACCOUNT, FAN, 2)).body = "I live in Perth."
    edited = memory.read(TENANT, ACCOUNT, FAN)
    assert edited["facts"]["home_city"]["value"] == "Brisbane"
    assert edited["fingerprint"] != first["fingerprint"]
    assert memory.progress(TENANT, ACCOUNT, FAN)["pendingMessages"] == 1
    assert drain_memory(memory) == 1
    refreshed = memory.read(TENANT, ACCOUNT, FAN)
    assert refreshed["facts"]["home_city"]["value"] == "Perth"
    with Session(engine) as session, session.begin():
        session.get(Message, (ACCOUNT, FAN, 2)).is_unsent = True
    retracted = memory.read(TENANT, ACCOUNT, FAN)
    assert retracted["facts"]["home_city"]["value"] == "Brisbane"
    assert retracted["fingerprint"] != refreshed["fingerprint"]
    assert drain_memory(memory) == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MemoryEvidence)) == 3, "audit survives invalidation"
    with Session(engine) as session, session.begin():
        session.get(Message, (ACCOUNT, FAN, 1)).direction = "out"
    assert "home_city" not in memory.read(TENANT, ACCOUNT, FAN)["facts"]
    assert drain_memory(memory) == 1


def test_existing_canonical_facts_and_saved_summary_have_priority_and_are_preserved(memory_db):
    _, engine, _, memory = memory_db
    with Session(engine) as session, session.begin():
        session.get(Fan, (ACCOUNT, FAN)).home_city = "Canonical city"
        session.add(FanProfile(account_id=ACCOUNT, fan_id=FAN, short_bio="Saved durable biography",
            bullet_points="Existing reliable notes", q1="Keep question", tease1="Keep opener"))
    insert_messages(engine, [(1, "I live in Brisbane. I work as an engineer.", 1)])
    drain_memory(memory)
    result = memory.read(TENANT, ACCOUNT, FAN)
    assert result["facts"]["home_city"] == {"value": "Canonical city", "source": "canonical"}
    assert result["facts"]["occupation"]["source"] == "history"
    assert result["summaries"] == {"short_bio": "Saved durable biography", "bullet_points": "Existing reliable notes"}
    with Session(engine) as session:
        assert session.get(Fan, (ACCOUNT, FAN)).home_city == "Canonical city"
        profile = session.get(FanProfile, (ACCOUNT, FAN))
        assert (profile.q1, profile.tease1) == ("Keep question", "Keep opener")


def test_explicit_correction_set_clear_tombstone_replay_and_conflict(memory_db):
    _, engine, _, memory = memory_db
    insert_messages(engine, [(1, "I live in Brisbane.", 1)])
    drain_memory(memory)
    old = memory.read(TENANT, ACCOUNT, FAN)
    args = correction(memory)
    applied = memory.correct(TENANT, ACCOUNT, FAN, **args)
    assert not applied["replayed"]
    assert memory.correct(TENANT, ACCOUNT, FAN, **args) == {"revision": applied["revision"], "replayed": True}
    changed = memory.read(TENANT, ACCOUNT, FAN)
    assert changed["fingerprint"] != old["fingerprint"]
    assert changed["facts"]["home_city"]["source"] == "operator"
    with pytest.raises(Refused, match="idempotency conflict"):
        memory.correct(TENANT, ACCOUNT, FAN, **{**args, "value": "Darwin"})
    with pytest.raises(Refused, match="version conflict"):
        memory.correct(TENANT, ACCOUNT, FAN, **{**args, "command_key": "stale-version"})
    clear = correction(memory, operation="clear", value=None, command_key="clear-city")
    memory.correct(TENANT, ACCOUNT, FAN, **clear)
    insert_messages(engine, [(2, "I live in Sydney.", 2)])
    drain_memory(memory)
    cleared = memory.read(TENANT, ACCOUNT, FAN)
    assert "home_city" not in cleared["facts"]
    assert cleared["overrides"]["home_city"]["operation"] == "clear"
    assert cleared["fingerprint"] != changed["fingerprint"]
    with Session(engine) as session:
        assert session.get(Fan, (ACCOUNT, FAN)).home_city is None
        assert locked_fields(session, ACCOUNT, FAN) == {"home_city"}
        assert session.scalar(select(func.count()).select_from(MemoryCorrection)) == 2
    assert_no_reply_or_model_rows(engine)


def test_native_fact_change_invalidates_operator_version_and_profile_correction_is_narrow(memory_db):
    _, engine, _, memory = memory_db
    args = correction(memory)
    with Session(engine) as session, session.begin():
        session.get(Fan, (ACCOUNT, FAN)).occupation = "New canonical fact"
        session.add(FanProfile(account_id=ACCOUNT, fan_id=FAN, short_bio="Old bio",
            bullet_points="Retained notes", q1="Retained question"))
    with pytest.raises(Refused, match="version conflict"):
        memory.correct(TENANT, ACCOUNT, FAN, **args)
    memory.correct(TENANT, ACCOUNT, FAN, **correction(memory, field="short_bio", value="Corrected biography"))
    memory.correct(TENANT, ACCOUNT, FAN, **correction(memory, field="bullet_points", operation="clear",
                                                    value=None, command_key="clear-notes"))
    assert memory.read(TENANT, ACCOUNT, FAN)["summaries"] == {"short_bio": "Corrected biography", "bullet_points": None}
    with Session(engine) as session:
        profile = session.get(FanProfile, (ACCOUNT, FAN))
        assert (profile.short_bio, profile.bullet_points, profile.q1) == ("Corrected biography", None, "Retained question")


def test_account_fan_and_agency_isolation_and_revoked_binding(memory_db):
    url, engine, _, memory = memory_db
    other_store = seed(url, engine, account="acc-b", tenant="tenant-b", fan=FAN)
    try:
        with Session(engine) as session, session.begin():
            session.add(Fan(account_id=ACCOUNT, fan_id=202))
        insert_messages(engine, [(1, "My name is Alice.", 1)])
        insert_messages(engine, [(1, "My name is Bob.", 1)], account="acc-b")
        insert_messages(engine, [(1, "My name is Carol.", 1)], fan=202)
        drain_memory(memory)
        drain_memory(memory, account="acc-b", tenant="tenant-b")
        drain_memory(memory, fan=202)
        assert memory.read(TENANT, ACCOUNT, FAN)["facts"]["real_name"]["value"] == "Alice"
        assert memory.read("tenant-b", "acc-b", FAN)["facts"]["real_name"]["value"] == "Bob"
        assert memory.read(TENANT, ACCOUNT, 202)["facts"]["real_name"]["value"] == "Carol"
        args = correction(memory)
        for method in (memory.read, memory.process, memory.progress):
            with pytest.raises(Refused, match="scope"):
                method("tenant-b", ACCOUNT, FAN)
            with pytest.raises(Refused, match="conversation unavailable"):
                method(TENANT, ACCOUNT, 999)
        with pytest.raises(Refused, match="scope"):
            memory.correct("tenant-b", ACCOUNT, FAN, **args)
        token = current_scope.set(AgencyScope(("creator-acc-b",), ACTOR, True))
        try:
            for method in (memory.read, memory.process, memory.progress):
                with pytest.raises(Refused, match="scope"):
                    method(TENANT, ACCOUNT, FAN)
            with pytest.raises(Refused, match="scope"):
                memory.correct(TENANT, ACCOUNT, FAN, **args)
        finally:
            current_scope.reset(token)
        token = current_scope.set(AgencyScope(("creator-acc-a",), ACTOR, False))
        try:
            assert memory.read(TENANT, ACCOUNT, FAN)["facts"]["real_name"]["value"] == "Alice"
            with pytest.raises(Refused, match="invalid memory correction"):
                memory.correct(TENANT, ACCOUNT, FAN, **args)
        finally:
            current_scope.reset(token)
        with Session(engine) as session, session.begin():
            session.get(OfCreatorBinding, "binding-acc-a").revoked_at = utcnow()
        for method in (memory.read, memory.process, memory.progress):
            with pytest.raises(Refused, match="scope"):
                method(TENANT, ACCOUNT, FAN)
        with pytest.raises(Refused, match="scope"):
            memory.correct(TENANT, ACCOUNT, FAN, **args)
    finally:
        other_store.close()


def test_process_rolls_back_all_receipts_and_evidence_then_retries(memory_db, monkeypatch):
    _, engine, _, memory = memory_db
    insert_messages(engine, [(1, "My name is Sam.", 1), (2, "I live in Brisbane.", 2)])
    original = memory_module.extract
    calls = []
    def fail_second(source):
        calls.append(source.message_id)
        if len(calls) == 2:
            raise RuntimeError("synthetic extraction interruption")
        return original(source)
    monkeypatch.setattr(memory_module, "extract", fail_second)
    with pytest.raises(RuntimeError, match="synthetic extraction interruption"):
        memory.process(TENANT, ACCOUNT, FAN)
    with Session(engine) as session:
        for model in (MemoryReceipt, MemoryEvidence, MemoryState):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    monkeypatch.setattr(memory_module, "extract", original)
    assert drain_memory(memory) == 2
    assert memory.read(TENANT, ACCOUNT, FAN)["facts"]["real_name"]["value"] == "Sam"


def test_correction_failure_rolls_back_canonical_value_and_override(memory_db):
    _, engine, _, memory = memory_db
    args = correction(memory)
    with engine.begin() as connection:
        connection.exec_driver_sql("""CREATE FUNCTION synthetic_reject_correction() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic correction interruption'; END; $$;
        CREATE TRIGGER synthetic_reject_correction BEFORE INSERT ON of_history_memory_correction
        FOR EACH ROW EXECUTE FUNCTION synthetic_reject_correction();""")
    with pytest.raises(DBAPIError, match="synthetic correction interruption"):
        memory.correct(TENANT, ACCOUNT, FAN, **args)
    with Session(engine) as session:
        assert session.get(Fan, (ACCOUNT, FAN)).home_city is None
        assert session.scalar(select(func.count()).select_from(MemoryCorrection)) == 0
        assert session.get(MemoryState, (ACCOUNT, FAN)) is None
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER synthetic_reject_correction ON of_history_memory_correction")
    assert not memory.correct(TENANT, ACCOUNT, FAN, **args)["replayed"]


def test_import_processing_never_enqueues_replies_and_new_inbound_can_progress(memory_db):
    _, engine, store, memory = memory_db
    for mid in range(1, 106):
        store.record(TENANT, ACCOUNT, event(mid=mid, body=f"Historical message {mid}"), source="import")
    while store.drain(TENANT, ACCOUNT):
        pass
    assert memory.process(TENANT, ACCOUNT, FAN, limit=10)["processed"] == 10
    assert_no_reply_or_model_rows(engine)
    store.record(TENANT, ACCOUNT, event(mid=1000, body="I live in Perth."))
    store.drain(TENANT, ACCOUNT)
    with Session(engine) as session:
        work = session.get(OfWork, (ACCOUNT, FAN))
        assert work is not None
        generation = work.generation
    assert memory.progress(TENANT, ACCOUNT, FAN)["pendingMessages"] == 96
    assert drain_memory(memory) == 96
    with Session(engine) as session:
        assert session.get(OfWork, (ACCOUNT, FAN)).generation == generation
        assert session.scalar(select(func.count()).select_from(OfTextIntent)) == 0
        assert session.scalar(select(func.count()).select_from(GrokCall)) == 0
    assert memory.read(TENANT, ACCOUNT, FAN)["facts"]["home_city"]["value"] == "Perth"


def test_audit_is_append_only_and_receipts_can_advance(memory_db):
    _, engine, _, memory = memory_db
    insert_messages(engine, [(1, "I live in Brisbane.", 1)])
    drain_memory(memory)
    memory.correct(TENANT, ACCOUNT, FAN, **correction(memory))
    for table in ("of_history_memory_evidence", "of_history_memory_correction"):
        for statement in (f"UPDATE {table} SET value='altered'", f"DELETE FROM {table}"):
            with pytest.raises(DBAPIError, match="append-only"):
                with engine.begin() as connection:
                    connection.exec_driver_sql(statement)
    with Session(engine) as session, session.begin():
        session.get(Message, (ACCOUNT, FAN, 1)).body = "I live in Sydney."
    assert drain_memory(memory) == 1


def test_invalid_bounds_role_actor_and_clear_are_refused(memory_db, monkeypatch):
    _, _, _, memory = memory_db
    for limit in (0, 101, True, 1.5):
        with pytest.raises(Refused):
            memory.process(TENANT, ACCOUNT, FAN, limit=limit)
    for kwargs in ({"max_interactions": 9}, {"max_interactions": True}, {"query": "x" * 2001}):
        with pytest.raises(Refused):
            memory.read(TENANT, ACCOUNT, FAN, **kwargs)
    for kwargs in ({"actor": "untrusted-user"}, {"operation": []}, {"operation": {}},
                   {"operation": "clear", "value": "not-null"},
                   {"command_key": "x" * 65}, {"expected_version": "stale"}):
        with pytest.raises(Refused):
            memory.correct(TENANT, ACCOUNT, FAN, **correction(memory, **kwargs))
    monkeypatch.delenv("ONLYFANS_PROCESS_ROLE")
    with pytest.raises(Refused, match="worker role"):
        memory.process(TENANT, ACCOUNT, FAN)


def test_legacy_large_values_are_projected_bounded_without_losing_correction_access(memory_db):
    _, engine, _, memory = memory_db
    with Session(engine) as session, session.begin():
        session.get(Fan, (ACCOUNT, FAN)).hobbies = "h" * 14000
        session.add(FanProfile(account_id=ACCOUNT, fan_id=FAN, short_bio="b" * 13000,
                               bullet_points="p" * 10000, q1="Unrelated question"))
    view = memory.read(TENANT, ACCOUNT, FAN)
    assert len(view["facts"]["hobbies"]["value"]) == 80
    assert len(view["summaries"]["short_bio"]) == 400
    assert len(view["summaries"]["bullet_points"]) == 600
    with Session(engine) as session, session.begin():
        profile = session.get(FanProfile, (ACCOUNT, FAN))
        assert len(profile.short_bio) == 13000
        profile.short_bio += "change beyond prompt clipping"
    after = memory.read(TENANT, ACCOUNT, FAN)
    assert after["version"] != view["version"] and after["fingerprint"] != view["fingerprint"]
    memory.correct(TENANT, ACCOUNT, FAN, **correction(memory, field="short_bio", value="Corrected bio"))
    with Session(engine) as session:
        assert session.get(FanProfile, (ACCOUNT, FAN)).q1 == "Unrelated question"


def test_account_sweep_is_fair_and_yields_to_an_active_live_claim(memory_db):
    _, engine, store, memory = memory_db
    with Session(engine) as session, session.begin():
        session.add(Fan(account_id=ACCOUNT, fan_id=202))
    insert_messages(engine, [(1, "My hobbies are chess.", 1), (2, "I live in Paris.", 2)])
    insert_messages(engine, [(1, "My hobbies are hiking.", 1)], fan=202)
    assert memory.step(TENANT, ACCOUNT, limit=1)["fanId"] == "101"
    assert memory.step(TENANT, ACCOUNT, limit=1)["fanId"] == "202"
    store.record(TENANT, ACCOUNT, event(mid=100, body="A new inbound"))
    store.drain(TENANT, ACCOUNT)
    from test_onlyfans_foundation_pg import due
    due(engine)
    claim = store.claim(TENANT)
    assert claim is not None
    assert memory.step(TENANT, ACCOUNT)["fanId"] is None
    assert memory.process(TENANT, ACCOUNT, FAN)["deferred"] == "live_claim"
    with Session(engine) as session, session.begin():
        session.get(OfWork, (ACCOUNT, FAN)).lease_until = utcnow() - timedelta(seconds=1)
    assert memory.step(TENANT, ACCOUNT)["processed"] == 2
