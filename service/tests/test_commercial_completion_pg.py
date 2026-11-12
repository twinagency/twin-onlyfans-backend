"""Focused independent-review counterexamples. Synthetic providers; normal owners only."""
import json
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_coexistence_staff_pg import make
from coexistence_fixture import FAN_A, FAN_B
from test_onlyfans_foundation_pg import database
from commercial_worker_fixture import build_world
from test_commercial_sale_rules_pg import catalogue
from history_support import card, fan_message
from conversation_engine import pilot_scope
from conversation_engine.writer_context import set_active_profiles
from db.onlyfans_engine_models import OfEnginePilotWebhookDigest, OfEnginePilotReconnectReceipt, OfEngineAccessEvidence, OfEngineOfferGrant
from db.onlyfans_fans_intake_models import OfFansEvidence
from onlyfans_runtime.launch_operations_models import OfLaunchCommand


def test_queue_boundary_restart_and_late_conflict(make):
    w = make()
    w.launch(coexist=True)
    for i in range(501):
        w.deliver(w.queue(queue_id=900000+i), event_key='completion-queue-'+str(i))
        if i in (498, 499, 500):
            assert w.block(FAN_A) is None and w.block(FAN_B) is None
    from model_transport.pilot_supervised import build_supervised_pilot
    w.pilot = build_supervised_pilot(w.store, w.env, model_transport=w.model_transport, fans_transport=w.fans_mock.transport)
    assert w.block(FAN_A) is None and w.block(FAN_B) is None
    with Session(w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfEnginePilotWebhookDigest).where(OfEnginePilotWebhookDigest.account_id==w.account)) == 501
        assert s.scalar(select(func.count()).select_from(OfEnginePilotReconnectReceipt).where(OfEnginePilotReconnectReceipt.account_id==w.account)) == 501
        assert s.scalar(select(func.count()).select_from(OfLaunchCommand).where(OfLaunchCommand.account_id==w.account, OfLaunchCommand.kind=='plan_c_queue_observation')) == 501
    # The 501st key is already classified. A different signed payload must revoke
    # its benign disposition even though every earlier page is harmless.
    w.deliver(w.queue(queue_id=999999), event_key='completion-queue-500')
    assert w.block(FAN_A) == 'pilot_evidence_pending'
    assert w.block(FAN_B) == 'pilot_evidence_pending'


def test_legacy_queue_history_is_classified_in_pages_without_losing_a_late_material_event(make, monkeypatch):
    w = make()
    w.launch(coexist=True)
    real = pilot_scope.classify_queue_observation
    monkeypatch.setattr(pilot_scope, 'classify_queue_observation', lambda *a: False)
    for i in range(501):
        w.deliver(w.queue(queue_id=800000+i), event_key='legacy-queue-'+str(i))
    # An unrecognized event follows the first full page; it is never discarded.
    w.deliver({'event':'unknown.safety','account_id':w.provider,'payload':{}}, event_key='legacy-late-unknown')
    monkeypatch.setattr(pilot_scope, 'classify_queue_observation', real)
    w.pilot.validate()  # restart/worker maintenance commits the legacy classification
    assert w.block(FAN_A) == 'pilot_evidence_pending'
    assert w.block(FAN_B) == 'pilot_evidence_pending'
    with Session(w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfLaunchCommand).where(OfLaunchCommand.account_id==w.account, OfLaunchCommand.kind=='plan_c_queue_observation')) == 501


def test_historical_access_explains_observation_without_inventing_engine_purchase(database, tmp_path, monkeypatch):
    def history(cfg):
        return {FAN_A:[fan_message(7101,FAN_A,'hi'),card(7102,cfg,[31],1200,previews=[],opened=True)]}
    w=build_world(database,tmp_path,monkeypatch,i=4,history=history)
    try:
        ids = catalogue(w)
        w.launch()
        w.converse('show me the beach photo',FAN_A)
        assert not w.cards()
        w.converse('send me the pool video',FAN_A)
        assert [p['body']['mediaFiles'] for p in w.cards()] == [['41']]
        with Session(database[1]) as s:
            row=s.scalar(select(OfFansEvidence).where(OfFansEvidence.account_id==w.account, OfFansEvidence.message_id==7102))
            assert (row.status,row.reason)==('paired','external_access_proven')
            assert row.intent_id is None and row.grant_id is None and row.peer_id is None
            assert s.scalar(select(func.count()).select_from(OfEngineAccessEvidence).where(OfEngineAccessEvidence.account_id==w.account))==0
            assert not any(g.item_id == ids['photo'] for g in s.scalars(select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id==w.account)))
        from model_transport.pilot_supervised import build_supervised_pilot
        restarted=build_supervised_pilot(w.store,w.env,model_transport=w.model_transport,fans_transport=w.fans_mock.transport)
        restarted.tick()
        assert len(w.cards())==1
    finally:
        set_active_profiles(None)
        w.store.close()


def test_conflicting_historical_preview_evidence_is_not_paired_or_granted(database, tmp_path, monkeypatch):
    def history(cfg):
        return {FAN_A:[fan_message(7101,FAN_A,'hi'),card(7102,cfg,[31],1200,previews=[],opened=True)]}
    w=build_world(database,tmp_path,monkeypatch,i=4,history=history)
    try:
        catalogue(w);w.launch()
        from fans_api_adapter.events import FansEventIntake
        from test_commercial_preparation_pg import TENANT
        intake=FansEventIntake(w.store)
        intake.ingest_history_item(TENANT,w.account,FAN_A,card(7102,w.cfg,[31],1200,previews=[31],opened=True))
        intake.drive(w.account,FAN_A)
        with Session(database[1]) as session:
            row=session.scalar(select(OfFansEvidence).where(OfFansEvidence.account_id==w.account, OfFansEvidence.message_id==7102))
            assert row.status in ('pending','held') and row.intent_id is None and row.grant_id is None
            from db.onlyfans_history_models import OfFansMessageFact
            assert session.get(OfFansMessageFact,(w.account,FAN_A,7102)).conflicted
        w.converse('How is your day?',FAN_A)
        assert len(w.fans_mock.posts)==1 and not w.cards()
    finally:
        set_active_profiles(None);w.store.close()
