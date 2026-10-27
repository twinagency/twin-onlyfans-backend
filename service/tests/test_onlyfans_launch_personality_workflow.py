"""Synthetic disposable DB proofs, including the real native prompt composer."""
import hashlib
import json
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

# No engine is connected by importing the runtime. Its legacy boot remains off.
os.environ.setdefault("ONLYFANS_PROCESS_ROLE", "worker")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:1/synthetic")

from db.models import Account, AccountAiConfig, Base, Fan, FanProfile, Message, OfCreatorBinding
from db.onlyfans_models import OfRuntime, OfConfigurationChange, OfIngress
from db.onlyfans_creator_facts_models import OfCreatorFactsSource
from onlyfans_read.agency_scope import AgencyScope, current_scope
from onlyfans_runtime.configuration import InvalidConfiguration
from onlyfans_runtime.native_context import versions
from onlyfans_runtime.native_planning import _native_messages
from onlyfans_runtime.store import Refused, Store
from onlyfans_runtime.launch_history_memory.models import MemoryCorrection, MemoryEvidence, MemoryReceipt, MemoryState
from onlyfans_runtime.launch_personality.models import PersonalityHead, PersonalityRevision
from onlyfans_runtime.launch_personality.workflow import PersonalityWorkflow

ACCOUNT, TENANT, BINDING, CREATOR = "synthetic-account", "synthetic-agency", "synthetic-binding", "synthetic-creator"
ACTOR = "op-session:" + "a" * 64
MODEL = "grok-4-1-fast-non-reasoning"
ACK = {"mixedAuthorship": True, "runtimeRules": True, "factsVerified": True}


def key(n): return f"{n:064x}"


PG = "ONLYFANS_TEST_PG_URL"


@pytest.fixture(scope="module")
def pg_template():
    # One bootstrapped (real migration chain, 0090 tables and triggers) disposable template per module.
    if not os.environ.get(PG):
        yield None
        return
    from test_onlyfans_foundation_pg import database
    lifecycle = database.__wrapped__()
    template = next(lifecycle)
    template[1].dispose()
    yield template[1].url.database
    try:
        next(lifecycle)
    except StopIteration:
        pass


@pytest.fixture
def env(tmp_path, pg_template):
    admin = database = None
    if pg_template is not None:
        import uuid
        admin = create_engine(os.environ[PG], isolation_level="AUTOCOMMIT")
        database = "of_test_personality_" + uuid.uuid4().hex
        with admin.connect() as c: c.exec_driver_sql(f'CREATE DATABASE "{database}" TEMPLATE "{pg_template}"')
        url = admin.url.set(database=database).render_as_string(hide_password=False)
        engine = create_engine(url, connect_args={"options": "-c timezone=UTC"})
    else:
        engine = create_engine("sqlite:///" + str(tmp_path / "synthetic.sqlite"))
    if engine.dialect.name == "sqlite":
        # pysqlite's legacy mode otherwise commits a released SAVEPOINT before
        # the SELECT-only outer transaction has physically started.
        @event.listens_for(engine, "connect")
        def sqlite_connect(connection, _):
            connection.isolation_level = None
            connection.create_function("md5", 1,
                lambda value: hashlib.md5(value.encode()).hexdigest() if value is not None else None,
                deterministic=True)
        @event.listens_for(engine, "begin")
        def sqlite_begin(connection): connection.exec_driver_sql("BEGIN")
    wanted = {m.__table__ for m in (Account, AccountAiConfig, Fan, FanProfile, Message,
        OfCreatorBinding, OfRuntime, OfConfigurationChange, OfCreatorFactsSource,
        OfIngress, PersonalityHead, PersonalityRevision,
        MemoryCorrection, MemoryEvidence, MemoryReceipt, MemoryState)}
    def dependencies(table):
        for fk in table.foreign_keys:
            other = fk.column.table
            if other not in wanted:
                wanted.add(other)
                dependencies(other)
    for table in list(wanted): dependencies(table)
    if database is None:
        Base.metadata.create_all(engine, tables=list(wanted))
    with Session(engine) as s, s.begin():
        s.add(Account(id=ACCOUNT)); s.flush()
        s.add(OfCreatorBinding(id=BINDING, tenant_id=TENANT, creator_id=CREATOR, account_id=ACCOUNT)); s.flush()
        s.add(OfRuntime(account_id=ACCOUNT, binding_id=BINDING, activated_at=datetime(2026, 1, 1), enabled=False))
        s.add(AccountAiConfig(account_id=ACCOUNT, persona="Original synthetic personality.", model=MODEL,
            persona_facts_json='{"age":"28","home_city":"Original Harbor"}',
            model_by_purpose='{"other_purpose":"keep"}', daily_cost_cap_cents=500,
            welcome_rules="Keep original rules.", ai_chatter_config_json='{"pack_send_enabled":false}'))
        s.add(Fan(account_id=ACCOUNT, fan_id=101))
    store = SimpleNamespace(engine=engine, _runtime=Store._runtime)
    yield store, PersonalityWorkflow(store)
    if database is None:
        Base.metadata.drop_all(engine, tables=list(wanted))
    engine.dispose()
    if admin is not None:
        with admin.connect() as c: c.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        admin.dispose()


def sample():
    return ({"accountId": ACCOUNT, "bindingId": BINDING, "creatorId": CREATOR,
        "sourceId": "synthetic-profile", "fields": {"age": "28", "home_city": "New Harbor"}},
        [{"accountId": ACCOUNT, "bindingId": BINDING, "sourceId": "synthetic-message",
          "direction": "out", "body": "hey lovely, how was your afternoon?", "author": "unknown",
          "fanKey": "synthetic-fan", "occurredAt": "2026-10-01T00:00:00Z"}])


def imported(w):
    profile, messages = sample()
    return w.import_draft(TENANT, ACCOUNT, profile=profile, messages=messages,
        expected_revision=0, command_key=key(1), actor=ACTOR)


def reviewed(w, **overrides):
    imported(w)
    review = {"persona": "Warm, playful and concise. Use gentle questions and natural lowercase texting.",
              "creatorFacts": {"home_city": "New Harbor"}, "replyModelOverride": MODEL, **overrides}
    return w.edit(TENANT, ACCOUNT, review=review, expected_revision=1, command_key=key(2), actor=ACTOR)


def approve(w, **overrides):
    return w.approve(TENANT, ACCOUNT, acknowledgements=ACK, expected_revision=2,
        command_key=key(3), actor=ACTOR, **overrides)


def test_import_is_review_only_and_revision_history_is_durable(env):
    store, w = env
    assert w.get(TENANT, ACCOUNT)["state"] == "empty"
    data = imported(w)
    assert data["state"] == "draft" and data["document"]["review"] == {}
    assert data["document"]["draft"]["creatorFacts"][0]["selected"] is False
    with Session(store.engine) as s:
        assert s.get(AccountAiConfig, ACCOUNT).persona == "Original synthetic personality."
        assert not s.get(OfRuntime, ACCOUNT).enabled
        assert s.scalar(select(func.count()).select_from(OfConfigurationChange)) == 0
    assert w.history(TENANT, ACCOUNT)["revisions"][0]["revision"] == 1


def test_approval_reaches_actual_native_prompt_without_changing_automation(env):
    store, w = env
    reviewed(w)
    result = approve(w)
    assert result["state"] == "approved" and result["configurationRevision"] == 1
    with Session(store.engine) as s:
        config = s.get(AccountAiConfig, ACCOUNT)
        assert json.loads(config.persona_facts_json) == {"age": "28", "home_city": "New Harbor"}
        assert json.loads(config.model_by_purpose) == {"other_purpose": "keep", "ai_chatter": MODEL}
        assert config.welcome_rules == "Keep original rules."
        assert json.loads(config.ai_chatter_config_json) == {"pack_send_enabled": False}
        assert not s.get(OfRuntime, ACCOUNT).enabled
        assert s.scalar(select(func.count()).select_from(OfConfigurationChange)) == 1
        _, context = versions(s, ACCOUNT, 101)
    context["messages"] = [{"direction": "in", "body": "How was your day?", "message_id": 99,
        "created_at": "2026-10-22T10:00:00"}]
    # Real composer and real native message builder, with no model/transport call.
    messages = _native_messages(context)
    prompt = json.dumps(messages, ensure_ascii=False)
    assert "Warm, playful and concise" in prompt and "New Harbor" in prompt and "28" in prompt
    assert "synthetic-message" not in prompt and "how was your afternoon" not in prompt
    assert not result["readiness"]["ready"]  # missing credential metadata never means ready


def test_approval_and_configuration_audit_roll_back_together(env):
    store, w = env
    reviewed(w)
    def fail(mapper, connection, target):
        if target.state == "approved": raise RuntimeError("synthetic audit failure")
    event.listen(PersonalityRevision, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit failure"): approve(w)
    finally:
        event.remove(PersonalityRevision, "before_insert", fail)
    with Session(store.engine) as s:
        assert s.get(AccountAiConfig, ACCOUNT).persona == "Original synthetic personality."
        assert s.get(OfRuntime, ACCOUNT).configuration_revision == 0
        assert s.get(PersonalityHead, ACCOUNT).revision == 2
        assert s.scalar(select(func.count()).select_from(OfConfigurationChange)) == 0
    assert approve(w)["state"] == "approved"


def test_retry_returns_recorded_revision_and_reused_command_refuses(env):
    store, w = env
    reviewed(w)
    first = approve(w)
    assert approve(w)["revision"] == first["revision"]
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(OfConfigurationChange)) == 1
    with pytest.raises(Refused, match="idempotency"):
        w.edit(TENANT, ACCOUNT, review={"persona": "Another tone."}, expected_revision=3, command_key=key(3), actor=ACTOR)


@pytest.mark.parametrize("legacy", [False, True])
def test_stale_configuration_refuses_approval_without_partial_writes(env, legacy):
    store, w = env
    reviewed(w)
    with Session(store.engine) as s, s.begin():
        if legacy: s.get(AccountAiConfig, ACCOUNT).welcome_rules = "Other writer"
        else: s.get(OfRuntime, ACCOUNT).configuration_revision += 1
    with pytest.raises(Refused, match="configuration revision"): approve(w)
    assert w.get(TENANT, ACCOUNT)["revision"] == 2


def test_approved_edits_create_new_draft_and_keep_previous_approval(env):
    store, w = env
    reviewed(w); approve(w)
    data = w.edit(TENANT, ACCOUNT, review={"persona": "Dry wit and short, friendly replies."}, expected_revision=3, command_key=key(4), actor=ACTOR)
    assert data["state"] == "draft"
    assert w.get(TENANT, ACCOUNT, revision=3)["state"] == "approved"
    with Session(store.engine) as s: assert "Warm, playful" in s.get(AccountAiConfig, ACCOUNT).persona


def test_rebind_hides_prior_evidence_and_allows_new_owner_import(env):
    store, w = env
    imported(w)
    with Session(store.engine) as s, s.begin():
        s.get(OfCreatorBinding, BINDING).revoked_at = datetime(2026, 10, 2); s.flush()
        s.add(OfCreatorBinding(id="new-binding", account_id=ACCOUNT, tenant_id="new-agency", creator_id="new-creator")); s.flush()
        s.get(OfRuntime, ACCOUNT).binding_id = "new-binding"
    with pytest.raises(Refused): w.get(TENANT, ACCOUNT)
    empty = w.get("new-agency", ACCOUNT)
    assert empty["state"] == "empty" and empty["document"] is None and empty["revision"] == 1
    assert w.history("new-agency", ACCOUNT)["revisions"] == []
    with pytest.raises(Refused): w.get("new-agency", ACCOUNT, revision=1)
    profile, messages = sample()
    profile.update(bindingId="new-binding", creatorId="new-creator")
    messages[0]["bindingId"] = "new-binding"
    assert w.import_draft("new-agency", ACCOUNT, profile=profile, messages=messages,
        expected_revision=1, command_key=key(8), actor=ACTOR)["revision"] == 2


@pytest.mark.parametrize("operation", ["import_existing", "edit", "approve"])
def test_displayed_binding_is_fenced_atomically_before_personality_mutation(env, operation):
    store, workflow = env
    revision = 0
    extra = {}
    if operation == "edit":
        imported(workflow)
        revision, extra = 1, {"review": {"persona": "Stale browser edit."}}
    elif operation == "approve":
        reviewed(workflow)
        revision, extra = 2, {"acknowledgements": ACK}
    with Session(store.engine) as session, session.begin():
        original = session.get(AccountAiConfig, ACCOUNT).persona
        count = session.scalar(select(func.count()).select_from(PersonalityRevision))
        session.get(OfCreatorBinding, BINDING).revoked_at = datetime(2026, 10, 23)
        session.flush()
        session.add(OfCreatorBinding(id="replacement-binding", account_id=ACCOUNT,
            tenant_id=TENANT, creator_id="replacement-creator"))
        session.flush()
        session.get(OfRuntime, ACCOUNT).binding_id = "replacement-binding"
    with pytest.raises(Refused, match="binding conflict"):
        getattr(workflow, operation)(TENANT, ACCOUNT, expected_revision=revision,
            command_key=key(99), actor=ACTOR, expected_binding_id=BINDING, **extra)
    with Session(store.engine) as session:
        assert session.scalar(select(func.count()).select_from(PersonalityRevision)) == count
        assert session.get(AccountAiConfig, ACCOUNT).persona == original
        assert session.scalar(select(func.count()).select_from(OfConfigurationChange)) == 0


def test_displayed_binding_participates_in_exact_command_replay(env):
    _, workflow = env
    kwargs = {"expected_revision": 0, "command_key": key(98), "actor": ACTOR,
              "expected_binding_id": BINDING}
    first = workflow.import_existing(TENANT, ACCOUNT, **kwargs)
    assert workflow.import_existing(TENANT, ACCOUNT, **kwargs) == first
    with pytest.raises(Refused, match="binding conflict"):
        workflow.import_existing(TENANT, ACCOUNT, **{**kwargs, "expected_binding_id": "another-binding"})


def test_rebind_does_not_reattribute_previous_creator_facts(env):
    store, w = env
    imported(w)
    with Session(store.engine) as s, s.begin():
        s.get(OfCreatorBinding, BINDING).revoked_at = datetime(2026, 10, 2); s.flush()
        s.add(OfCreatorBinding(id="new-binding", account_id=ACCOUNT, tenant_id="new-agency", creator_id="new-creator")); s.flush()
        s.get(OfRuntime, ACCOUNT).binding_id = "new-binding"
    data = w.import_existing("new-agency", ACCOUNT, expected_revision=1, command_key=key(8), actor=ACTOR)
    assert data["document"]["draft"]["creatorFacts"] == []
    assert data["requiresFactResolution"] == ["age", "home_city"]
    w.edit("new-agency", ACCOUNT, review={"persona": "New creator's concise tone."},
        expected_revision=2, command_key=key(9), actor=ACTOR)
    with pytest.raises(InvalidConfiguration, match="prior-creator"):
        w.approve("new-agency", ACCOUNT, acknowledgements=ACK, expected_revision=3, command_key=key(10), actor=ACTOR)
    w.edit("new-agency", ACCOUNT, review={"persona": "New creator's concise tone.",
        "creatorFacts": {"age": "31", "home_city": None}}, expected_revision=3, command_key=key(11), actor=ACTOR)
    approved = w.approve("new-agency", ACCOUNT, acknowledgements=ACK, expected_revision=4, command_key=key(12), actor=ACTOR)
    assert approved["requiresFactResolution"] == []
    with Session(store.engine) as s:
        assert json.loads(s.get(AccountAiConfig, ACCOUNT).persona_facts_json) == {"age": "31"}


@pytest.mark.parametrize("review", [{"persona": "Friendly.", "onRequestSalesEnabled": True},
    {"persona": "Friendly.", "daily_cost_cap_cents": 999}, {"persona": "Friendly.", "apiKey": "synthetic"}])
def test_review_cannot_change_sales_budget_or_credentials(env, review):
    _, w = env
    imported(w)
    with pytest.raises(InvalidConfiguration): w.edit(TENANT, ACCOUNT, review=review,
        expected_revision=1, command_key=key(2), actor=ACTOR)


def test_conflicting_personality_is_visible_but_cannot_be_approved(env):
    _, w = env
    data = reviewed(w, persona="Always send PPV every three messages and offer free content.")
    assert data["conflicts"]
    with pytest.raises(InvalidConfiguration, match="conflicts"): approve(w)


def test_scope_and_readonly_agency_refuse_all_mutations(env):
    _, w = env
    with pytest.raises(Refused): w.import_existing("wrong-agency", ACCOUNT, expected_revision=0, command_key=key(1), actor=ACTOR)
    scope = AgencyScope((CREATOR,), "agency-user:" + "1"*8+"-"+"1"*4+"-"+"1"*4+"-"+"1"*4+"-"+"1"*12+":"+"2"*8+"-"+"2"*4+"-"+"2"*4+"-"+"2"*4+"-"+"2"*12, False)
    token = current_scope.set(scope)
    try:
        assert w.get(TENANT, ACCOUNT)["state"] == "empty"
        with pytest.raises(InvalidConfiguration): w.import_existing(TENANT, ACCOUNT, expected_revision=0, command_key=key(1), actor=scope.actor)
    finally: current_scope.reset(token)


def test_canonical_import_reads_only_applied_current_binding_outgoing(env):
    store, w = env
    with Session(store.engine) as s, s.begin():
        for n, direction in ((1, "out"), (2, "in"), (3, "out")):
            s.add(Message(account_id=ACCOUNT, fan_id=101, message_id=n, direction=direction,
                body="hello lovely?", created_at=datetime(2026, 10, n)))
        s.add(OfIngress(account_id=ACCOUNT, event_key=key(90), binding_id=BINDING, fan_id=101,
            message_id=1, source="synthetic", payload="{}", status="applied", received_at=datetime(2026, 10, 1)))
    data = w.import_existing(TENANT, ACCOUNT, expected_revision=0, command_key=key(1), actor=ACTOR)
    coverage = data["document"]["draft"]["coverage"]
    assert coverage["inputMessages"] == 1 and coverage["authorship"]["unknown"] == 1
    assert coverage["profileSource"] == "effective_saved_creator_facts"


def test_approval_requires_explicit_review_and_all_acknowledgements(env):
    _, w = env
    imported(w)
    with pytest.raises(InvalidConfiguration): w.approve(TENANT, ACCOUNT, acknowledgements=ACK,
        expected_revision=1, command_key=key(2), actor=ACTOR)
    with pytest.raises(InvalidConfiguration): w.approve(TENANT, ACCOUNT, acknowledgements={**ACK, "factsVerified": False},
        expected_revision=1, command_key=key(2), actor=ACTOR)


def test_credential_metadata_is_server_scoped_and_never_persisted(env):
    store, w = env
    reviewed(w); approve(w)
    reader = lambda s, account, binding: {"accountId": account, "modelId": MODEL,
        "configured": True, "expiresAt": "2099-01-01T00:00:00Z"}
    data = PersonalityWorkflow(store, credential_status_reader=reader).get(TENANT, ACCOUNT)
    assert data["readiness"]["ready"] and data["readiness"]["credential"]["metadataAvailable"]
    assert "2099" not in json.dumps(data)


def test_noop_approval_records_review_without_inventing_configuration_change(env):
    store, w = env
    reviewed(w, persona="Original synthetic personality.", creatorFacts={"home_city": "Original Harbor"}, replyModelOverride=None)
    data = approve(w)
    assert data["state"] == "approved" and data["configurationRevision"] == 0
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(OfConfigurationChange)) == 0


def test_other_configuration_edit_invalidates_readiness_of_approved_personality(env):
    store, w = env
    reviewed(w); approve(w)
    with Session(store.engine) as s, s.begin(): s.get(AccountAiConfig, ACCOUNT).persona = "Changed elsewhere."
    reader = lambda s, account, binding: {"accountId": account, "modelId": MODEL,
        "configured": True, "expiresAt": "2099-01-01T00:00:00Z"}
    data = PersonalityWorkflow(store, credential_status_reader=reader).get(TENANT, ACCOUNT)
    assert not data["readiness"]["ready"] and "approved_configuration_changed" in data["readiness"]["blockers"]


@pytest.mark.skipif(not os.environ.get(PG), reason="ONLYFANS_TEST_PG_URL required for the row-lock concurrency proof")
def test_concurrent_edits_only_one_expected_revision_wins(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    _, w = env
    imported(w)
    barrier = Barrier(2)
    def save(n):
        barrier.wait(timeout=5)
        try:
            return w.edit(TENANT, ACCOUNT, review={"persona": f"Friendly concise tone version {n}."},
                expected_revision=1, command_key=key(n), actor=ACTOR)["revision"]
        except Refused: return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, (2, 3)))
    assert sorted(results, key=str) == [2, "conflict"]
    assert w.get(TENANT, ACCOUNT)["revision"] == 2


@pytest.mark.skipif(not os.environ.get(PG), reason="ONLYFANS_TEST_PG_URL required for the row-lock concurrency proof")
def test_concurrent_approval_retry_applies_configuration_once(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store, w = env
    reviewed(w)
    barrier = Barrier(2)
    def save(_):
        barrier.wait(timeout=5)
        return approve(w)["revision"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(save, (1, 2))) == [3, 3]
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(OfConfigurationChange)) == 1


def test_real_api_import_review_approve_and_readback(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from onlyfans_runtime.launch_personality.api import router, PREFIX
    from onlyfans_commands.configuration_api import HEADER, SCHEMA
    store, w = env
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TOKEN", "synthetic-personality-configuration-purpose-token")
    monkeypatch.setenv("ONLYFANS_CONFIGURATION_TENANT", TENANT)
    app = FastAPI(); app.state.control_store = store; app.include_router(router)
    path = PREFIX.replace("{account_id}", ACCOUNT)
    headers = {"Authorization": "Bearer synthetic-personality-configuration-purpose-token", HEADER: SCHEMA}
    with TestClient(app) as client:
        imported_response = client.post(path + "/import", headers=headers,
            json={"expectedRevision": 0, "commandKey": key(1), "actor": ACTOR})
        assert imported_response.status_code == 200, imported_response.text
        reviewed_response = client.post(path + "/review", headers=headers,
            json={"expectedRevision": 1, "commandKey": key(2), "actor": ACTOR,
                  "review": {"persona": "Warm, casual tone with short natural questions."}})
        assert reviewed_response.status_code == 200, reviewed_response.text
        approved_response = client.post(path + "/approve", headers=headers,
            json={"expectedRevision": 2, "commandKey": key(3), "actor": ACTOR, "acknowledgements": ACK})
        assert approved_response.status_code == 200, approved_response.text
        saved = client.get(path, headers=headers)
        assert saved.status_code == 200 and saved.json()["personality"]["state"] == "approved"
        assert saved.headers["cache-control"] == "private, no-store"
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(Message)) == 0
        assert not s.get(OfRuntime, ACCOUNT).enabled


@pytest.mark.skipif(not os.environ.get(PG), reason="ONLYFANS_TEST_PG_URL required for the migration trigger proof")
def test_exact_migration_evidence_trigger_rejects_update_and_delete(env):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    store, w = env
    imported(w)
    # The trigger is installed by migration 0090_launch, not by this fixture.
    for statement in ("UPDATE of_personality_revision SET state='approved'", "DELETE FROM of_personality_revision"):
        with pytest.raises(DBAPIError, match="append-only"):
            with store.engine.begin() as connection: connection.execute(text(statement))
