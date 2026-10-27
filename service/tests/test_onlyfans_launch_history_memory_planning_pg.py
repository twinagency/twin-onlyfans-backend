"""Real native composer/planner with its registered synthetic model and the integrated memory hooks."""
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event, due
from test_onlyfans_native_planning_pg import ready
from db.models import Message
from db.onlyfans_delivery_models import OfNativePlan, OfTextIntent
from onlyfans_runtime import native_context, native_memory, native_planning
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory, enrich_snapshot, augment_prompt
from onlyfans_runtime.store import utcnow

TENANT, ACCOUNT, FAN = "tenant-a", "acc-a", 101
ACTOR = "op-session:" + "a" * 64


def wire_test_seams(monkeypatch):
    """The shared hooks are integrated (versions, prompt addon, override filter); nothing is patched here."""


def history_ready(database, monkeypatch):
    store, authority, _, planner = ready(database, monkeypatch)
    _, engine = database
    old = utcnow() - timedelta(days=40)
    with Session(engine) as s, s.begin():
        s.add_all(Message(account_id=ACCOUNT, fan_id=FAN, message_id=mid,
            direction="in", created_at=old + timedelta(seconds=mid),
            body=("My hobbies are origami. I finished the Copperleaf marathon."
                  if mid == 700 else f"Can you remember this synthetic older interaction {mid}?"))
            for mid in range(100, 1305))
    memory = HistoricalMemory(store)
    store.record(TENANT, ACCOUNT, event(mid=2000, body="Can you remember my Copperleaf marathon?"))
    store.drain(TENANT, ACCOUNT)
    while memory.process(TENANT, ACCOUNT, FAN, limit=100)["processed"]:
        pass
    due(engine)
    wire_test_seams(monkeypatch)
    return store, authority, planner, memory, store.claim(TENANT)


def test_historical_fact_and_interaction_reach_actual_native_model_with_exact_100_window(database, monkeypatch):
    store, _, planner, memory, claim = history_ready(database, monkeypatch)
    try:
        model = SyntheticNativeModel("That sounds like a memorable achievement.")
        identity = planner.prepare(TENANT, claim, model)
        with Session(store.engine) as s:
            plan = s.get(OfNativePlan, (ACCOUNT, identity))
            assert plan.status == "ready"
            context = json.loads(plan.context_json)
            assert len(context["messages"]) == 100
            assert 700 not in [m["message_id"] for m in context["messages"]]
            assert context["messages"][-1]["message_id"] == 2000
            assert context["historicalMemory"]["facts"]["hobbies"]["value"] == "origami"
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
        prompt = str(model.calls[0])
        assert "origami" in prompt and "I finished the Copperleaf marathon" in prompt
        assert "untrusted source data" in prompt
        assert model.calls[0][-1]["role"] == "user"
    finally:
        store.close()


@pytest.mark.parametrize("change", ["correction", "retraction"])
def test_changed_historical_evidence_invalidates_real_inflight_plan(database, monkeypatch, change):
    store, _, planner, memory, claim = history_ready(database, monkeypatch)
    try:
        def during_generation():
            if change == "correction":
                view = memory.read(TENANT, ACCOUNT, FAN)
                memory.correct(TENANT, ACCOUNT, FAN, field="hobbies", operation="clear", value=None,
                    expected_version=view["version"], command_key="clear-history", actor=ACTOR,
                    reason="Synthetic correction during generation")
            else:
                with Session(store.engine) as s, s.begin():
                    s.get(Message, (ACCOUNT, FAN, 700)).is_unsent = True
        identity = planner.prepare(TENANT, claim, SyntheticNativeModel(on_generate=during_generation))
        with Session(store.engine) as s:
            assert s.get(OfNativePlan, (ACCOUNT, identity)).status == "stale"
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
    finally:
        store.close()
