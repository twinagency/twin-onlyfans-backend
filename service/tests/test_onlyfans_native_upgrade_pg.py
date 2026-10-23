"""Populated frozen 0073/0074 -> 0075 rehearsal; no activation or allowance reset."""
from pathlib import Path
import pytest

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from db.models import Account, Fan, Message, OfConversationState, OfCreatorBinding, OfMessageDelivery, GrokCall, GrokDailyCost
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfNativePlan, OfTextIntent
from db.schema import EGRESS, NATIVE, HEAD, check_schema, upgrade
from onlyfans_runtime.store import utcnow


@pytest.mark.parametrize("with_native_plan", [False, True])
def test_populated_one_use_sender_upgrade_retains_unknowns_caps_and_controls(database, with_native_plan):
    _, engine = database
    snapshots = Path(__file__).resolve().parents[1] / "db/snapshots"
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        for name in ("0070_postgresql.sql", "0071_foundation.sql", "0072_delivery.sql", "0073_egress.sql"):
            c.exec_driver_sql((snapshots/name).read_text())
        if with_native_plan:
            c.exec_driver_sql((snapshots/"0074_native_text.sql").read_text())
        c.exec_driver_sql("CREATE TABLE alembic_version(version_num varchar(32) PRIMARY KEY)")
        c.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": NATIVE if with_native_plan else EGRESS})
        check_schema(c, expected=NATIVE if with_native_plan else EGRESS)
        assert ("of_native_plan" in inspect(c).get_table_names()) == with_native_plan

    now = utcnow()
    with Session(engine) as s, s.begin():
        s.add(Account(id="old", nickname="Synthetic retained account")); s.flush()
        s.add(Fan(account_id="old", fan_id=1)); s.flush()
        s.add(OfCreatorBinding(id="retained-binding", tenant_id="tenant-a", account_id="old", creator_id="old-creator",
                               creator_name="Synthetic Creator", handle="synthetic", channel="onlyfans")); s.flush()
        s.execute(OfRuntime.__table__.insert().values(account_id="old", binding_id="retained-binding", activated_at=now,
                        enabled=False, recovery_needed=True))
        s.add(GrokDailyCost(day=now.strftime("%Y-%m-%d"), account_id="old", provider="grok",
            cost_cents=10000, call_count=17, is_capped=True))
        s.add(GrokCall(purpose="ai_chatter",account_id="old",fan_id=1,model="grok-4-1-fast-non-reasoning",
            provider="grok",endpoint="synthetic://onlyfans-native-text",prompt_json="[]",
            status="pending",cost_cents=10000,was_dry_run=True,called_at=now))
        s.add(OfConversationState(account_id="old", fan_id=1, mode="no_contact", revision=9,
                                  input_revision=17, unresolved_attempt_id="retained-attempt", requires_attention=True))
        s.execute(OfDeliveryPolicy.__table__.insert().values(account_id="old", revision=4, enabled=False, used_units=2,
                               limit_units=5, queued_count=0, throttle_seconds=60))
        s.add(Message(account_id="old", fan_id=1, message_id=90, direction="out", body="Retained evidence", created_at=now))
        s.flush()
        s.add(OfMessageDelivery(account_id="old", fan_id=1, message_id=90, outcome="unknown",
                                attempt_id="retained-attempt", evidence_ref="synthetic-retained-receipt"))
        # Core insert uses only published 0073 columns, never a future ORM field.
        s.execute(OfTextIntent.__table__.insert().values(
            account_id="old", intent_id="retained-intent", fan_id=1, binding_id="retained-binding",
            command_key="old-command", actor="synthetic", origin="manual", body="Retained uncertain draft",
            command_hash="a"*64, payload_hash="b"*64, control_revision=9, input_revision=17, policy_revision=4,
            status="unknown", created_at=now, updated_at=now, expires_at=now, attempt_id="retained-attempt",
            dispatch_token="retained-token", wire_version=1, wire_started_at=now, reservation_held=True,
        ))
        if with_native_plan:
            s.execute(OfNativePlan.__table__.insert().values(
                account_id="old",plan_id="retained-native-plan",fan_id=1,binding_id="retained-binding",
                work_generation=1,input_revision=17,claim_json="{}",context_json="{}",context_hash="c"*64,
                planner_version="native-text.v1",model_id="synthetic/native-text",status="queued",
                body="Retained uncertain draft",intent_id="retained-intent",created_at=now,expires_at=now))
            s.execute(text("UPDATE of_text_intent SET native_plan_id='retained-native-plan'"))

    tables = ("accounts", "fans", "messages", "of_creator_bindings", "of_runtime", "of_conversation_state",
              "of_message_delivery", "of_delivery_policy", "of_text_intent", "grok_calls", "grok_daily_cost")
    if with_native_plan: tables += ("of_native_plan",)
    def retained_rows(c):
        return {name: [dict(row) for row in c.execute(text(f'SELECT * FROM "{name}"')).mappings()] for name in tables}
    with engine.connect() as c:
        before = retained_rows(c)
    with engine.begin() as c:
        upgrade(c)
        upgrade(c)
        check_schema(c)
        after = retained_rows(c)
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    if not with_native_plan:
        for row in after["of_text_intent"]: assert row.pop("native_plan_id") is None
    else:
        for row in after["of_native_plan"]:
            for name in ("resolved_model_id","model_call_id","budget_day","billing_provider",
                         "reserved_millicents","charged_millicents","cost_settled_at"):
                assert row.pop(name) is None
    for row in after["of_delivery_policy"]: assert row.pop("media_enabled") is False
    for row in after["of_text_intent"]:
        for key, value in {"media_json":"[]","preview_json":"[]","price_cents":0,
                "catalog_item_id":None,"catalog_snapshot_json":None,"offer_id":None}.items():
            assert row.pop(key) == value
    if with_native_plan:
        for row in after["of_native_plan"]: assert row.pop("sale_json") is None
    for row in after["of_runtime"]: assert row.pop("configuration_revision") == 0
    assert after == before, "migration may add a plan link, but cannot reset authority or rewrite evidence"
    with Session(engine) as s:
        if not with_native_plan:
            assert list(s.scalars(select(OfNativePlan))) == [], "schema upgrade never invokes or activates a planner"
        else:
            from onlyfans_runtime.native_context import cost_matches
            assert not cost_matches(s, s.get(OfNativePlan,("old","retained-native-plan")))


def test_populated_budget_upgrade_retains_cost_evidence_and_cannot_regenerate_old_input(database,monkeypatch):
    import hashlib,json
    from test_onlyfans_native_planning_pg import ready
    from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
    from onlyfans_runtime.native_planning import SyntheticNativeModel
    from onlyfans_runtime.store import Refused
    from db.schema import BUDGET
    _,engine=database
    store,_,claim,planner=ready(database,monkeypatch)
    identity=planner.prepare(TENANT,claim,SyntheticNativeModel(reported_cost_millicents=7))
    with Session(engine) as s,s.begin():
        plan=s.get(OfNativePlan,(ACCOUNT,identity));context=json.loads(plan.context_json)
        context.pop("profile")
        plan.context_json=json.dumps(context,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        prior={k:context[k] for k in ["config","fan"]}
        plan.context_hash=hashlib.sha256(json.dumps(prior,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
        plan.planner_version="native-text.v2"
    tables=["fans","fan_profiles","of_native_plan","grok_calls","grok_daily_cost","of_work","of_runtime","of_delivery_policy"]
    def values(c):return {t:[dict(x) for x in c.execute(text(f"SELECT * FROM {t}")).mappings()] for t in tables}
    with engine.begin() as c:
        from test_onlyfans_offer_import_upgrade_pg import historical_schema
        historical_schema(c,BUDGET)
        before=values(c)
        upgrade(c);upgrade(c);check_schema(c)
        after=values(c)
        for row in after["of_delivery_policy"]: assert row.pop("media_enabled") is False
        for row in after["of_native_plan"]: assert row.pop("sale_json") is None
        for row in after.get("of_runtime",[]): assert row.pop("configuration_revision")==0
        assert after==before
        assert c.scalar(text("SELECT count(*) FROM of_native_memory_change"))==0
    model=SyntheticNativeModel()
    assert planner.prepare(TENANT,claim,model)==identity and model.calls==[]
    with pytest.raises(Refused):planner.queue(TENANT,ACCOUNT,identity)
    with Session(engine) as s:
        assert s.scalar(select(GrokDailyCost)).cost_cents==7
        assert s.scalar(select(GrokDailyCost)).call_count==1
    store.close()
