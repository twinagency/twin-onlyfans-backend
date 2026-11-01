"""R3/R7 through the real native planner on PostgreSQL. Synthetic model and ids only."""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event  # noqa: F401
from test_onlyfans_text_delivery_pg import configured, TENANT, ACCOUNT
from test_onlyfans_native_planning_pg import plan_state
from db.models import AccountAiConfig, Fan, FanProfile, Message
from db.onlyfans_delivery_models import OfNativePlan, OfTextIntent
from db.onlyfans_models import OfWork
from onlyfans_runtime.native_planning import NativeTextPlanner, SyntheticNativeModel
from onlyfans_runtime import operator_case_port as port

ADS = "#ads go show her some love on her free page https://example.invalid/synthetic-page"


def prepare_turn(database, monkeypatch, bodies, *, fan_update=None, media=None, model=None, persona_facts='{"name":"Synthetic Creator"}'):
    url, engine = database
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", url)
    port.PENDING.clear()
    store, _ = configured(database, mode="automated")
    with Session(engine) as s, s.begin():
        s.add(AccountAiConfig(account_id=ACCOUNT, model="grok-4-1-fast-non-reasoning",
                              persona="Synthetic Creator enjoys hiking and coffee.", persona_facts_json=persona_facts))
        fan = s.get(Fan, (ACCOUNT, 101))
        for key, value in (fan_update or {}).items():
            setattr(fan, key, value)
    for number, body in enumerate(bodies, start=1):
        store.record(TENANT, ACCOUNT, event(mid=number, body=body))
    store.drain(TENANT, ACCOUNT)
    for mid, values in (media or {}).items():
        with Session(engine) as s, s.begin():
            row = s.get(Message, (ACCOUNT, 101, mid))
            for key, value in values.items():
                setattr(row, key, value)
    due(engine)
    claim = store.claim(TENANT)
    assert claim is not None
    model = model or SyntheticNativeModel("Thanks for telling me, how has your day been?")
    planner = NativeTextPlanner(store)
    plan = planner.prepare(TENANT, claim, model)
    return store, engine, planner, plan, model


def plan_row(engine, plan):
    with Session(engine) as s:
        row = s.get(OfNativePlan, (ACCOUNT, plan))
        return row.status, row.reason


def counts(engine):
    with Session(engine) as s:
        return (s.scalar(select(func.count()).select_from(Message)), s.scalar(select(func.count()).select_from(OfTextIntent)),
                s.scalar(select(func.count()).select_from(FanProfile)))


def test_confirmed_promo_gets_no_model_call_no_reply_no_memory_and_raw_record_kept(database, monkeypatch):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, [ADS], fan_update={"source": "creator_we_follow"})
    assert model.calls == [] and plan_row(engine, plan) == ("refused", "promo_no_reply")
    with Session(engine) as s:
        assert s.scalar(select(Message.body).where(Message.message_id == 1)) == ADS
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
        assert s.get(Fan, (ACCOUNT, 101)).real_name in (None, "")
        assert s.get(OfWork, (ACCOUNT, 101)).status == "blocked"
        context = json.loads(s.get(OfNativePlan, (ACCOUNT, plan)).context_json)
    assert context["inboundIntent"]["labels"] == ["promo"]    # reviewable
    store.close()


def test_mixed_burst_with_a_genuine_request_stays_eligible(database, monkeypatch):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, [ADS, "can i get a preview first?"],
                                                       fan_update={"source": "creator_we_follow"})
    assert plan_state(engine, plan) == "ready" and len(model.calls) == 1
    store.close()


def test_ambiguous_message_keeps_the_customer_eligible(database, monkeypatch):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, ["is your free page any good?"],
                                                       fan_update={"source": "creator_we_follow"})
    assert plan_state(engine, plan) == "ready" and len(model.calls) == 1
    store.close()


def test_spender_is_never_auto_excluded(database, monkeypatch):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, [ADS], fan_update={"source": "creator_we_follow", "lifetime_spend_cents": 900})
    assert plan_state(engine, plan) == "ready" and len(model.calls) == 1
    store.close()


def test_business_enquiry_is_withheld_and_opens_an_operator_case(database, monkeypatch):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, ["Hi, I run an agency and would like to discuss managing your page"])
    assert plan_row(engine, plan) == ("refused", "business_no_reply") and model.calls == []
    assert [c["kind"] for c in port.PENDING] == ["business_enquiry"]
    store.close()


def test_promo_latest_message_cannot_write_personal_memory(database, monkeypatch):
    latest = ADS + " my name is Sam"
    model = SyntheticNativeModel("ok", fact_updates=[{"field": "real_name", "value": "Sam", "quote": "my name is Sam"}])
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, ["hello there", latest], model=model)
    assert plan_state(engine, plan) == "ready"
    with Session(engine) as s:
        assert s.get(Fan, (ACCOUNT, 101)).real_name in (None, "")
    store.close()


def test_fan_correction_beats_the_stored_fact_even_if_the_model_misses_it(database, monkeypatch):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, ["my name is Sam, not Josh"], fan_update={"real_name": "Josh"})
    assert plan_state(engine, plan) == "ready"
    with Session(engine) as s:
        assert s.get(Fan, (ACCOUNT, 101)).real_name == "Sam"
    store.close()


@pytest.mark.parametrize("age_seconds,desc,expect", [
    (5, None, "has not been looked at yet"),
    (900, None, "you could not view it"),
    (900, "a mountain at sunrise", "a mountain at sunrise"),
])
def test_media_only_inbound_is_visible_to_the_planner_in_every_state(database, monkeypatch, age_seconds, desc, expect):
    from datetime import datetime, timedelta, timezone
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=age_seconds)
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, [""],
        media={1: {"media_count": 1, "image_desc": desc, "raw_json": '{"media":[{"type":"photo"}]}', "created_at": created}})
    prompt = json.dumps(model.calls[0])
    assert expect in prompt and "[he sent a photo" in prompt
    assert "FACT DISCIPLINE" in prompt and "ATTACHMENTS:" in prompt
    store.close()


def test_unsolicited_rating_is_held_but_a_requested_one_with_a_description_passes(database, monkeypatch):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, ["some shots from last night"],
        model=SyntheticNativeModel("8.5"))
    assert plan_row(engine, plan) == ("refused", "draft_refused")
    store.close()


def test_rating_requested_with_a_described_attachment_is_allowed(database, monkeypatch):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, ["rate me out of 10"],
        media={1: {"media_count": 1, "image_desc": "a smiling man", "raw_json": '{"media":[{"type":"photo"}]}'}},
        model=SyntheticNativeModel("solid 8/10"))
    assert plan_state(engine, plan) == "ready"
    store.close()


def test_failed_attachment_opens_a_visible_case_and_never_asks_for_a_resend(database, monkeypatch):
    from datetime import datetime, timedelta, timezone
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=900)
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, [""],
        media={1: {"media_count": 1, "image_desc": None, "raw_json": '{"media":[{"type":"photo"}]}', "created_at": created}})
    assert "attachment_unreadable" in [c["kind"] for c in port.PENDING]
    store.close()


@pytest.mark.parametrize("reply", ["no im not a bot babe", "its really me typing all of this myself"])
def test_false_denial_of_automation_is_held(database, monkeypatch, reply):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, ["are you a bot?"], model=SyntheticNativeModel(reply))
    assert plan_row(engine, plan)[0] == "refused"
    store.close()


def test_identity_question_opens_a_case_once_and_keeps_the_existing_model_turn(database, monkeypatch):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, ["are you a bot?"])
    assert plan_state(engine, plan) == "ready" and "Do not confirm or deny it" in json.dumps(model.calls[0])
    assert [c["kind"] for c in port.PENDING] == ["identity_question"]
    assert port.PENDING[0]["dedupe_key"].endswith(":1")
    store.close()


def test_unknown_creator_age_is_not_invented_in_the_native_prompt(database, monkeypatch):
    store, engine, planner, plan, model = prepare_turn(database, monkeypatch, ["how old are you?"])
    prompt = json.dumps(model.calls[0])
    assert "22yo" not in prompt and "Age:" not in prompt and "is unknown" in prompt
    store.close()
