"""Campaign dispatch through the actual runner and canonical FASTT transport."""
from types import SimpleNamespace
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_campaigns_pg import configured,create,CampaignService,ACCOUNT,TENANT,FAN
from test_onlyfans_fastt_operator import Client
from db.onlyfans_delivery_models import OfTextIntent
from db.models import Message
from onlyfans_runtime import fastt_scope
from native_pilot.service import Service

@pytest.mark.parametrize("failure",[None,"timeout","recipient","price"])
@pytest.mark.parametrize("kind",["text","photo","video","bundle"])
@pytest.mark.parametrize("scheduled",[False,True])
def test_campaign_runner_dispatches_once_and_restart_retains_unknown(database,monkeypatch,failure,kind,scheduled):
    paid=kind!="text"
    store,authority=configured(database,mode="automated")
    try:
        item=None
        revision=1
        if paid:
            from test_onlyfans_media_pg import shelf,media_policy
            item=shelf(database[1]);media_policy(authority);revision=2
            if kind!="bundle":
                from db.models import CatalogItem
                with Session(store.engine) as session,session.begin():
                    catalog=session.get(CatalogItem,item)
                    catalog.media_ids="[11]" if kind=="photo" else "[12]"
                    catalog.preview_media_ids="[]"
                    catalog.kind="image" if kind=="photo" else "video"
            from datetime import timedelta
            from db.onlyfans_ledger_models import OfLedgerScan
            from db.onlyfans_models import OfRuntime
            from onlyfans_runtime.ledger import _coverage_receipt
            from onlyfans_runtime.store import utcnow
            with Session(store.engine) as session, session.begin():
                now=utcnow()
                scan=OfLedgerScan(account_id=ACCOUNT,binding_id=session.get(OfRuntime,ACCOUNT).binding_id,
                    owner_id=7001,connection_revision=1,window_start=now-timedelta(days=90),window_end=now,
                    page_offset=0,status="idle",last_complete_at=now,last_full_at=now,next_poll_at=now,updated_at=now)
                scan.coverage_json=_coverage_receipt(scan)
                session.add(scan)
        authority.configure(TENANT,ACCOUNT,expected_revision=revision,enabled=True,media_enabled=paid,limit_units=20,
            throttle_seconds=1,actor="synthetic-operator",reason="Isolated campaign runner",
            transport_kind="native")
        from datetime import timedelta, timezone
        from onlyfans_runtime.store import utcnow
        target=(utcnow()+timedelta(seconds=3)).replace(tzinfo=timezone.utc).isoformat() if scheduled else None
        create(CampaignService(store),scheduled_at=target,**({"catalog_item_id":item} if paid else {}))
        monkeypatch.setenv("DATABASE_URL",database[0])
        monkeypatch.setenv("ONLYFANS_WORKER_TENANT",TENANT)
        monkeypatch.setattr(fastt_scope,"owned_scope",lambda:({(ACCOUNT,FAN)},set()))
        monkeypatch.setattr(fastt_scope,"serve_scope",lambda:({(ACCOUNT,FAN)},set()))
        client=Client(timeout=failure=="timeout",
            change=(lambda row:row.update(toUser={"id":FAN+1})) if failure=="recipient" else
            (lambda row:row.update(price=0 if paid else 10)) if failure=="price" else None)
        def runner():
            obj=object.__new__(Service)
            obj.db=ACCOUNT;obj.all_fans=False;obj.args=SimpleNamespace(fan=FAN)
            obj.scope=SimpleNamespace(provider_id=7001)
            obj.client=client;obj._twin_store=store
            return obj
        if scheduled:
            assert runner()._twin_controls() is None
            assert client.calls==[]
            import time
            time.sleep(3.2)
        assert runner()._twin_controls()==("unknown" if failure else "accepted")
        with Session(store.engine) as s:
            row=s.query(OfTextIntent).filter_by(account_id=ACCOUNT).one()
            assert row.origin=="campaign"
            assert row.status==("unknown" if failure else "accepted")
            assert (s.get(Message,(ACCOUNT,FAN,9001)) is None)==bool(failure)
        runner()._twin_controls()
        assert len(client.calls)==1
    finally:
        store.close()
