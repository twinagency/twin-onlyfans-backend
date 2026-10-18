"""Canonical purchase/reversal evidence; no provider or model is constructed."""
from dataclasses import replace
from datetime import timedelta, timezone
import multiprocessing

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_media_pg import manual, intent
from test_onlyfans_text_delivery_pg import ACCOUNT,TENANT,FAN
from db.models import ContentOffer,Fan,LadderState,Message,OfConversationState,Transaction,VaultSend
from db.onlyfans_delivery_models import OfCommerceEvidence
from onlyfans_runtime.commerce import CommerceIntake,SyntheticCommerceEvent,held
from onlyfans_runtime.store import Refused,utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from ownership import owned_media_in_session


def setup(database,monkeypatch):
    _,engine=database
    store,authority,item=manual(database)
    identity=intent(authority,item)
    assert authority.dispatch(TENANT,ACCOUNT,identity,SyntheticTextTransport(ACCOUNT))=="accepted"
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    monkeypatch.setenv("DATABASE_URL",database[0])
    return store,authority,item,CommerceIntake(store)


def evidence(*,revision=1,kind="purchase",payout="cleared",base=None,**kw):
    stamp=(base or utcnow())+timedelta(seconds=revision)
    return SyntheticCommerceEvent(account_id=ACCOUNT,fan_id=FAN,message_id=9001,
        event_key=f"synthetic-transaction:{revision}:{kind}",provider_transaction_id="synthetic-tx-1",
        provider_revision=revision,kind=kind,amount_cents=1000,currency="USD",
        occurred_at=stamp.replace(tzinfo=timezone.utc).isoformat(),evidence_ref=f"synthetic-payment:{revision}:{kind}",
        payout_status=payout,**kw)


def test_paid_receipt_is_idempotent_and_uses_existing_canonical_owners(database,monkeypatch):
    _,engine=database;store,_,_,intake=setup(database,monkeypatch)
    paid=evidence();assert intake.record(TENANT,ACCOUNT,paid)=="applied"
    assert intake.record(TENANT,ACCOUNT,paid)=="applied"
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Transaction))==1
        assert s.scalar(select(func.count()).select_from(OfCommerceEvidence))==1
        assert s.get(Message,(ACCOUNT,FAN,9001)).is_paid is True
        assert s.get(Fan,(ACCOUNT,FAN)).lifetime_spend_cents==1000
        assert owned_media_in_session(s,ACCOUNT,FAN)=={11,12,13}
        offer=s.scalar(select(ContentOffer));assert (offer.status,offer.resolved_by)==("delivered","ppv_ledger")
        assert s.get(LadderState,(ACCOUNT,FAN)).status=="hot"
        assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=="human_controlled"
    store.close()


def test_pending_payout_is_paid_once_and_clearing_does_not_reopen_selling(database,monkeypatch):
    _,engine=database;store,_,_,intake=setup(database,monkeypatch)
    base=utcnow();assert intake.record(TENANT,ACCOUNT,evidence(base=base,payout="pending"))=="applied"
    with Session(engine) as s,s.begin():
        bought_at=s.get(Message,(ACCOUNT,FAN,9001)).purchased_at
        s.get(LadderState,(ACCOUNT,FAN)).status="stopped"
    assert intake.record(TENANT,ACCOUNT,evidence(base=base,revision=2))=="applied"
    with Session(engine) as s:
        assert s.scalar(select(Transaction)).status=="cleared"
        assert s.get(Fan,(ACCOUNT,FAN)).lifetime_spend_cents==1000
        assert s.get(Message,(ACCOUNT,FAN,9001)).purchased_at==bought_at
        assert s.get(LadderState,(ACCOUNT,FAN)).status=="stopped"
        assert owned_media_in_session(s,ACCOUNT,FAN)=={11,12,13}
    store.close()


@pytest.mark.parametrize("kind,status",[("refund","refunded"),("chargeback","chargedback")])
def test_reversal_revokes_only_this_purchase_and_stops_sales_without_silencing_chat(database,monkeypatch,kind,status):
    _,engine=database;store,_,_,intake=setup(database,monkeypatch)
    base=utcnow();assert intake.record(TENANT,ACCOUNT,evidence(base=base))=="applied"
    reverse=evidence(base=base,revision=2,kind=kind)
    assert intake.record(TENANT,ACCOUNT,reverse)=="applied" and intake.record(TENANT,ACCOUNT,reverse)=="applied"
    with Session(engine) as s:
        assert s.scalar(select(Transaction)).status==status
        assert s.get(Fan,(ACCOUNT,FAN)).lifetime_spend_cents==0
        assert s.get(Message,(ACCOUNT,FAN,9001)).is_paid is False
        assert owned_media_in_session(s,ACCOUNT,FAN)==set()
        assert not any(x.was_purchased for x in s.scalars(select(VaultSend)))
        assert s.scalar(select(ContentOffer)).status=="cancelled"
        assert s.get(LadderState,(ACCOUNT,FAN)).status=="stopped"
        state=s.get(OfConversationState,(ACCOUNT,FAN));assert state.mode=="human_controlled" and state.requires_attention
    store.close()


def test_out_of_order_purchase_cannot_resurrect_a_refund(database,monkeypatch):
    _,engine=database;store,_,_,intake=setup(database,monkeypatch)
    base=utcnow();assert intake.record(TENANT,ACCOUNT,evidence(base=base,revision=2,kind="refund"))=="applied"
    older=evidence(base=base);assert intake.record(TENANT,ACCOUNT,older)=="stale"
    assert intake.record(TENANT,ACCOUNT,older)=="stale"
    with Session(engine) as s:
        assert s.scalar(select(Transaction)).status=="refunded" and s.get(Fan,(ACCOUNT,FAN)).lifetime_spend_cents==0
        assert owned_media_in_session(s,ACCOUNT,FAN)==set() and not held(s,ACCOUNT)
        assert s.scalar(select(func.count()).select_from(OfCommerceEvidence))==2
    store.close()


@pytest.mark.parametrize("change",["event_key","revision","terminal","clock","message_payload"])
def test_conflicting_payment_evidence_is_retained_and_holds_media_sales(database,monkeypatch,change):
    _,engine=database;store,authority,item,intake=setup(database,monkeypatch)
    base=utcnow();paid=evidence(base=base);assert intake.record(TENANT,ACCOUNT,paid)=="applied"
    incoming=evidence(base=base,revision=2,kind="refund")
    if change=="event_key":incoming=replace(incoming,event_key=paid.event_key)
    elif change=="revision":incoming=replace(incoming,provider_revision=1)
    elif change=="clock":incoming=replace(incoming,occurred_at=base.replace(tzinfo=timezone.utc).isoformat())
    elif change=="terminal":
        assert intake.record(TENANT,ACCOUNT,incoming)=="applied"
        incoming=evidence(base=base,revision=3)
    elif change=="message_payload":
        with Session(engine) as s,s.begin():s.get(Message,(ACCOUNT,FAN,9001)).media_ids="[11]"
    assert intake.record(TENANT,ACCOUNT,incoming)=="conflict"
    assert intake.record(TENANT,ACCOUNT,incoming)=="conflict"
    with Session(engine) as s:
        assert held(s,ACCOUNT) and not held(s,"unrelated-account")
        assert s.get(OfConversationState,(ACCOUNT,FAN)).requires_attention
        assert s.scalar(select(func.count()).select_from(Transaction))==1
    with pytest.raises(Refused,match="commerce evidence needs review"):intent(authority,item,key="held-media")
    # A conflict does not change the text allowance or acquire send rights.
    text_intent=authority.prepare(TENANT,ACCOUNT,FAN,command_key="still-human",expected_revision=0,
        body="I am reviewing that payment.",origin="manual",actor="synthetic-operator")
    assert text_intent
    store.close()


def test_refund_preserves_ownership_proved_by_another_message(database,monkeypatch):
    _,engine=database;store,_,_,intake=setup(database,monkeypatch)
    base=utcnow();assert intake.record(TENANT,ACCOUNT,evidence(base=base))=="applied"
    with Session(engine) as s,s.begin():
        s.add(Message(account_id=ACCOUNT,fan_id=FAN,message_id=8000,direction="out",body="Another paid copy",
            media_ids="[12]",media_count=1,price_cents=1000,is_paid=True,created_at=base-timedelta(hours=1)))
        s.add(VaultSend(account_id=ACCOUNT,fan_id=FAN,media_id=12,message_id=8000,price_cents=1000,was_purchased=True))
    assert intake.record(TENANT,ACCOUNT,evidence(base=base,revision=2,kind="refund"))=="applied"
    with Session(engine) as s:assert owned_media_in_session(s,ACCOUNT,FAN)=={12}
    store.close()


@pytest.mark.parametrize("bad",[{"amount_cents":999},{"fan_id":999},{"account_id":"another"},
    {"currency":"EUR"},{"provider_revision":True},{"kind":"tip"},{"message_id":999}])
def test_payment_scope_and_exact_terms_refuse_without_side_effects(database,monkeypatch,bad):
    _,engine=database;store,_,_,intake=setup(database,monkeypatch)
    with pytest.raises(Refused):intake.record(TENANT,ACCOUNT,replace(evidence(),**bad))
    with Session(engine) as s:
        assert list(s.scalars(select(Transaction)))==[] and list(s.scalars(select(OfCommerceEvidence)))==[]
        assert s.get(Message,(ACCOUNT,FAN,9001)).is_paid is False
        assert s.get(Fan,(ACCOUNT,FAN)).lifetime_spend_cents==0
    store.close()


def test_mid_projection_database_failure_rolls_back_all_money_ownership_and_evidence(database,monkeypatch):
    _,engine=database;store,_,_,intake=setup(database,monkeypatch)
    with engine.begin() as c:
        c.exec_driver_sql("CREATE FUNCTION fail_purchase_projection() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic projection crash'; END $$")
        c.exec_driver_sql("CREATE TRIGGER fail_purchase_projection BEFORE UPDATE ON vault_sends FOR EACH ROW EXECUTE FUNCTION fail_purchase_projection()")
    with pytest.raises(DBAPIError):intake.record(TENANT,ACCOUNT,evidence())
    with Session(engine) as s:
        assert list(s.scalars(select(Transaction)))==[] and list(s.scalars(select(OfCommerceEvidence)))==[]
        assert s.get(Message,(ACCOUNT,FAN,9001)).is_paid is False
        assert s.get(Fan,(ACCOUNT,FAN)).lifetime_spend_cents==0 and s.scalar(select(ContentOffer)).status=="open"
    store.close()


@pytest.mark.parametrize("verb",["UPDATE of_commerce_evidence SET status='stale'","DELETE FROM of_commerce_evidence"])
def test_payment_evidence_is_append_only(database,monkeypatch,verb):
    _,engine=database;store,_,_,intake=setup(database,monkeypatch)
    assert intake.record(TENANT,ACCOUNT,evidence())=="applied"
    with pytest.raises(DBAPIError,match="append-only"):
        with engine.begin() as c:c.execute(text(verb))
    store.close()


def _payment_process(url,event,queue):
    import os
    os.environ["ONLYFANS_PROCESS_ROLE"]="worker"
    os.environ["DATABASE_URL"]=url
    from onlyfans_runtime.store import Store
    store=Store(url)
    try:queue.put(CommerceIntake(store).record(TENANT,ACCOUNT,event))
    finally:store.close()


@pytest.mark.parametrize("reverse",[False,True])
def test_competing_payment_workers_never_double_count_or_resurrect_ownership(database,monkeypatch,reverse):
    url,engine=database;store,_,_,_=setup(database,monkeypatch)
    base=utcnow();paid=evidence(base=base)
    events=[paid,evidence(base=base,revision=2,kind="refund") if reverse else paid]
    ctx=multiprocessing.get_context("spawn");queue=ctx.Queue()
    workers=[ctx.Process(target=_payment_process,args=(url,e,queue)) for e in events]
    for worker in workers:worker.start()
    for worker in workers:worker.join(30);assert worker.exitcode==0
    results=[queue.get(timeout=2) for _ in workers]
    assert set(results)<={"applied","stale"}
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Transaction))==1
        assert s.scalar(select(func.count()).select_from(OfCommerceEvidence))==(2 if reverse else 1)
        assert s.get(Fan,(ACCOUNT,FAN)).lifetime_spend_cents==(0 if reverse else 1000)
        assert owned_media_in_session(s,ACCOUNT,FAN)==(set() if reverse else {11,12,13})
    store.close()


def test_payment_cannot_invent_the_accepted_message_of_an_unknown_send(database,monkeypatch):
    _,engine=database;store,authority,item=manual(database)
    identity=intent(authority,item)
    assert authority.dispatch(TENANT,ACCOUNT,identity,SyntheticTextTransport(ACCOUNT,outcome="timeout"))=="unknown"
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    with pytest.raises(Refused,match="accepted media history required"):
        CommerceIntake(store).record(TENANT,ACCOUNT,evidence())
    with Session(engine) as s:
        assert list(s.scalars(select(Transaction)))==[] and list(s.scalars(select(OfCommerceEvidence)))==[]
        assert s.get(Message,(ACCOUNT,FAN,9001)) is None
        assert s.get(OfConversationState,(ACCOUNT,FAN)).unresolved_attempt_id is not None
    store.close()


def test_encrypted_restore_preserves_paid_owners_conflict_and_append_only_evidence(database,monkeypatch,tmp_path):
    import os
    from test_onlyfans_recovery_pg import KEY,empty_database,rows
    from onlyfans_runtime import operations as ops
    from db.schema import check_schema,SchemaMismatch
    url,engine=database;store,_,_,intake=setup(database,monkeypatch)
    paid=evidence();assert intake.record(TENANT,ACCOUNT,paid)=="applied"
    assert intake.record(TENANT,ACCOUNT,replace(paid,kind="refund"))=="conflict"
    before=rows(engine);before.pop("alembic_version")
    archive=tmp_path/"synthetic-commerce.encrypted"
    ops.backup(url,archive,key=KEY,pg_bin=os.environ["ONLYFANS_TEST_PG_BIN"])
    with empty_database() as (target,restored):
        ops.restore(target,archive,key=KEY,pg_bin=os.environ["ONLYFANS_TEST_PG_BIN"])
        after=rows(restored);after.pop("alembic_version");assert after==before
        with restored.connect() as c:
            with pytest.raises(SchemaMismatch,match="quarantined"):check_schema(c)
        with pytest.raises(DBAPIError,match="append-only"):
            with restored.begin() as c:c.execute(text("DELETE FROM of_commerce_evidence"))
    store.close()
