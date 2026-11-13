"""G06b DSF2: FansAPI media verified outside the latest completed commercial selection is never sold.

Isolated PostgreSQL, explicit mock provider (test_commercial_vault_pg.setup); no paid calls or sends.
Verified outside = a full (0 < selected < 1000) completed selection at the current binding/epoch lacks the
media and nothing re-observed it since. No selection at the current epoch (reconnect mid-import), an empty
or a capped one is unknown and keeps selling. Content sync's sale_hold uses the same predicate.
"""
import json
import pytest
from datetime import datetime
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from db.models import CatalogItem
from db.onlyfans_engine_models import OfFansConnection
from db.onlyfans_models import OfRuntime
from db.onlyfans_offer_import_models import OfContentSyncAccount
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from test_real_transport_pg import _item
from test_commercial_vault_pg import setup
from fans_api_adapter.vault_sync import sync_vault
from onlyfans_runtime.content_sync import _media_states
from onlyfans_runtime.media_delivery import catalog_terms
from onlyfans_runtime.store import Refused
REFUSED="catalogue media unavailable|media_unusable"   # row rule | content sync's sale_hold


def vanished(database, synced=False):
    """Sync media 11+12, save an enabled item over both with no category audit (as put_singles, offer
    import and manual items do), then a second full sync where 12 vanished (no isDeleted marker)."""
    items=[_item(11),_item(12)]
    store,client,calls,state=setup(database,items)
    assert sync_vault(store,TENANT,ACCOUNT,client,command_key="a"*64)["complete"]
    with Session(store.engine) as s,s.begin():
        row=CatalogItem(account_id=ACCOUNT,kind="photo",label="Scenic set",description_for_ai="Two photographs.",
            media_ids="[11,12]",preview_media_ids="[11]",price_cents=1200,enabled=True)
        s.add(row);s.flush();item=row.id
        if synced:   # content sync configured: sale_hold's media_unusable runs
            now=datetime.utcnow()
            s.add(OfContentSyncAccount(account_id=ACCOUNT,binding_id=s.get(OfRuntime,ACCOUNT).binding_id,enabled=False,
                status="off",revision=1,actor="t",command_key="c"*64,backlog=0,failures=0,created_at=now,updated_at=now))
    with Session(store.engine) as s:
        assert catalog_terms(s,ACCOUNT,item)["media_ids"]==[11,12]
    items[:]=[_item(11)]
    assert sync_vault(store,TENANT,ACCOUNT,client,command_key="d"*64)["complete"]
    return store,item


def states(store):
    with Session(store.engine) as s:
        return _media_states(s,ACCOUNT,s.get(OfRuntime,ACCOUNT),None,[11,12])


def sells(store, item):
    with Session(store.engine) as s:
        assert catalog_terms(s,ACCOUNT,item)["media_ids"]==[11,12]
    assert "unusable" not in states(store).values()
    store.close()


@pytest.mark.parametrize("synced",[False,True])
def test_vanished_media_outside_completed_selection_is_not_sold(database, synced):
    store,item=vanished(database,synced)
    for lock in (False,True):
        with Session(store.engine) as s,s.begin():
            with pytest.raises(Refused,match=REFUSED):
                catalog_terms(s,ACCOUNT,item,lock=lock)
    assert states(store)=={11:"ok",12:"unusable"}
    store.close()


# Fail on fe1f1add (cap/empty/stale absence counted as evidence; sale_hold refused too when synced).
@pytest.mark.parametrize("selected",[["11"]+[str(10**6+i) for i in range(999)],[]],ids=["capped","empty"])
@pytest.mark.parametrize("synced",[False,True])
def test_capped_or_empty_selection_is_unknown_and_sells(database, selected, synced):
    store,item=vanished(database,synced)
    with store.engine.begin() as c:
        c.execute(text("UPDATE of_vault_sync_job SET window_json=:w WHERE account_id=:a AND status='complete'"),
                  {"w":json.dumps({"selected":selected}),"a":ACCOUNT})
    sells(store,item)


def test_reobserved_after_the_selection_sells(database):
    store,item=vanished(database)
    with store.engine.begin() as c:   # a newer (still incomplete) job saw 12 after the selection completed
        c.execute(text("UPDATE of_vault_observation SET observed_at=now()+interval '1 hour' "
                       "WHERE account_id=:a AND media_id=12"),{"a":ACCOUNT})
    sells(store,item)


def test_native_policy_ignores_fansapi_selection(database):
    # Guard (passes on fe1f1add): native policy is unchanged on both the row rule and sale_hold.
    store,item=vanished(database,synced=True)
    with store.engine.begin() as c:
        c.execute(text("UPDATE of_delivery_policy SET transport_kind='native' WHERE account_id=:a"),{"a":ACCOUNT})
    sells(store,item)


# Guards (pass on fe1f1add): no completed selection at the current epoch is a transient unknown.
@pytest.mark.parametrize("change",["retired","new_epoch"])
def test_unknown_selection_is_transient_and_still_sells(database, change):
    store,item=vanished(database)
    with Session(store.engine) as s,s.begin():
        old=s.scalar(select(OfFansConnection).where(OfFansConnection.account_id==ACCOUNT))
        old.status="retired";s.flush()   # session gone; new_epoch: reconnected, new import not complete yet
        if change=="new_epoch":
            s.add(OfFansConnection(account_id=ACCOUNT,epoch=old.epoch+1,binding_id=old.binding_id,status="active",
                provider_account_id=old.provider_account_id,provider_creator_id=old.provider_creator_id,created_at=datetime.utcnow()))
    sells(store,item)


# DSF3: scripts_api.put_singles refuses, at save time, the media the sale boundary would refuse.
DRM_VIDEO='{"id": 22, "type": "video", "isReady": true}'   # no progressive full URL: sellable (OQ-3)


def put_singles(database, monkeypatch, media, enabled=True, extra=None):
    import asyncio
    from contextlib import asynccontextmanager
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker,create_async_engine
    import scripts_api as api
    engine=create_async_engine(make_url(database[0]).set(drivername="postgresql+asyncpg"))
    factory=async_sessionmaker(engine,expire_on_commit=False)

    @asynccontextmanager
    async def get_session():
        async with factory() as s,s.begin():
            yield s
    monkeypatch.setattr(api,"get_session",get_session)
    monkeypatch.setattr(api,"assert_account_owned",lambda a:None)
    item={"kind":"video","label":"Blue","description_for_ai":"A blue set.","media_ids":media,
          "preview_media_ids":[21],"price_cents":1200,"enabled":enabled}
    async def run():
        more=[dict(item,label="Other",media_ids=extra,enabled=True)] if extra else []
        try:return await api.put_singles(api._ItemsBody(account_id=ACCOUNT,items=[item,*more]))
        finally:await engine.dispose()
    return asyncio.run(run())


def seed_raw(database, raw):
    from test_engine_catalogue_pg import setup as engine_setup
    store,authority,*_=engine_setup(database)
    with database[1].begin() as c:
        c.execute(text("UPDATE vault_items SET raw_json=:r WHERE account_id=:a AND media_id=22"),{"r":raw,"a":ACCOUNT})
        c.execute(text("UPDATE catalog_items SET enabled=false WHERE account_id=:a"),{"a":ACCOUNT})  # each save is a new single
    return store,authority


@pytest.mark.parametrize("raw,why",[('{"isHidden": true}',"removed"),('{"isReady": false}',"not_ready"),
                                    ('{"id":',"unreadable")])
def test_put_singles_refuses_media_the_sale_refuses(database, monkeypatch, raw, why):
    from fastapi import HTTPException
    store,_=seed_raw(database,raw)
    with pytest.raises(HTTPException) as e:
        put_singles(database,monkeypatch,[21,22])
    assert e.value.status_code==422 and f"media 22 cannot be sold ({why})" in e.value.detail
    assert put_singles(database,monkeypatch,[21,22],enabled=False)["status"]=="ok"   # disabled draft saves
    store.close()


def test_put_singles_drm_saves_fansapi_not_ready_refused(database, monkeypatch):
    """OQ-3 (owner 2026-11-13): DRM sells on both transports; FansAPI still refuses not isReady true."""
    from fastapi import HTTPException
    from test_real_transport_pg import real_policy
    store,authority=seed_raw(database,DRM_VIDEO)
    assert put_singles(database,monkeypatch,[21,22])=={"status":"ok","items":1}   # native: DRM offerable
    real_policy(database,authority)
    assert put_singles(database,monkeypatch,[21,22],enabled=False)["status"]=="ok"   # unchanged enabled ones skip
    assert put_singles(database,monkeypatch,[21,22])=={"status":"ok","items":1}   # FansAPI: DRM offerable
    assert put_singles(database,monkeypatch,[21,22],enabled=False)["status"]=="ok"
    with database[1].begin() as c:
        c.execute(text("UPDATE vault_items SET raw_json=:r WHERE account_id=:a AND media_id=22"),
                  {"r":'{"id": 22, "type": "video"}',"a":ACCOUNT})
    with pytest.raises(HTTPException,match=r"media 22 cannot be sold \(not_ready\)"):
        put_singles(database,monkeypatch,[21,22])
    assert put_singles(database,monkeypatch,[21])["status"]=="ok"   # route-level: the save is accepted
    store.close()


def test_put_singles_unchanged_stale_single_does_not_block_an_unrelated_edit(database, monkeypatch):
    # Fails on fe1f1add: the wholesale replace re-checked every enabled single.
    from fastapi import HTTPException
    from test_engine_catalogue_pg import setup as engine_setup
    store,*_=engine_setup(database)
    assert put_singles(database,monkeypatch,[21,22])["status"]=="ok"
    with database[1].begin() as c:
        c.execute(text("UPDATE vault_items SET raw_json='{\"isHidden\": true}' WHERE account_id=:a AND media_id=22"),{"a":ACCOUNT})
        c.execute(text("UPDATE catalog_items SET media_ids='[21,22]' WHERE account_id=:a"),{"a":ACCOUNT})  # create_catalog_single's form
    assert put_singles(database,monkeypatch,[21,22],extra=[21])=={"status":"ok","items":2}
    with pytest.raises(HTTPException,match=r"media 22 cannot be sold \(removed\)"):
        put_singles(database,monkeypatch,[21,22],extra=[22])   # a new single over it still refuses
    store.close()


def test_put_singles_refuses_media_outside_the_selection(database, monkeypatch):
    from fastapi import HTTPException
    store,_=vanished(database)
    with database[1].begin() as c:
        c.execute(text("UPDATE catalog_items SET enabled=false WHERE account_id=:a"),{"a":ACCOUNT})  # a new single, not the seeded one
    with pytest.raises(HTTPException,match=r"media 12 cannot be sold \(outside_selection\)"):
        put_singles(database,monkeypatch,[11,12])
    store.close()
