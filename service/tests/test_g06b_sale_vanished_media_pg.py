"""G06b DSF2: FansAPI media verified outside the latest completed commercial selection is never sold.

Isolated PostgreSQL, explicit mock provider (test_commercial_vault_pg.setup); no paid calls or sends.
Verified outside = a completed selection exists at the current binding/epoch and lacks the media
(vanished from the provider listing, or fallen out of the newest window). No completed selection at
the current epoch (reconnect mid-import) is a transient unknown and keeps selling, as before.
"""
import pytest
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from db.models import CatalogItem
from db.onlyfans_engine_models import OfFansConnection
from db.onlyfans_models import OfRuntime
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from test_real_transport_pg import _item
from test_commercial_vault_pg import setup
from fans_api_adapter.vault_sync import sync_vault
from onlyfans_runtime.content_sync import _media_states
from onlyfans_runtime.media_delivery import catalog_terms
from onlyfans_runtime.store import Refused


def sellable(database):
    """Sync media 11+12 and save an enabled item over both with no category audit, as put_singles,
    offer import and manual items do; returns (store, client, provider items, item id)."""
    items=[_item(11),_item(12)]
    store,client,calls,state=setup(database,items)
    assert sync_vault(store,TENANT,ACCOUNT,client,command_key="a"*64)["complete"]
    with Session(store.engine) as s,s.begin():
        row=CatalogItem(account_id=ACCOUNT,kind="photo",label="Scenic set",description_for_ai="Two photographs.",
            media_ids="[11,12]",preview_media_ids="[11]",price_cents=1200,enabled=True)
        s.add(row);s.flush();item=row.id
    with Session(store.engine) as s:
        assert catalog_terms(s,ACCOUNT,item)["media_ids"]==[11,12]
    return store,client,items,item


def states(store):
    with Session(store.engine) as s:
        return _media_states(s,ACCOUNT,s.get(OfRuntime,ACCOUNT),None,[11,12])


def test_vanished_media_outside_completed_selection_is_not_sold(database):
    store,client,items,item=sellable(database)
    items[:]=[_item(11)]   # 12 vanished from the provider listing, no isDeleted marker
    assert sync_vault(store,TENANT,ACCOUNT,client,command_key="d"*64)["complete"]
    for lock in (False,True):
        with Session(store.engine) as s,s.begin():
            with pytest.raises(Refused,match="catalogue media unavailable"):
                catalog_terms(s,ACCOUNT,item,lock=lock)
    assert states(store)=={11:"ok",12:"unusable"}
    store.close()


def test_unknown_selection_is_transient_and_still_sells(database):
    store,client,items,item=sellable(database)
    with Session(store.engine) as s,s.begin():
        # Session gone (connection retired, no reconnect yet): the selection is unknown, not verified.
        s.scalar(select(OfFansConnection).where(OfFansConnection.account_id==ACCOUNT)).status="retired"
    with Session(store.engine) as s:
        assert catalog_terms(s,ACCOUNT,item)["media_ids"]==[11,12]
    assert "unusable" not in states(store).values()
    store.close()


def test_missing_vault_row_is_refused_at_the_sale_boundary(database):
    store,client,items,item=sellable(database)
    with store.engine.begin() as c:
        for table in ("vault_folder_items","of_vault_observation","vault_items"):
            c.execute(text(f"DELETE FROM {table} WHERE account_id=:a AND media_id=12"),{"a":ACCOUNT})
    with Session(store.engine) as s:
        with pytest.raises(Refused,match="catalogue media unavailable"):
            catalog_terms(s,ACCOUNT,item)
    store.close()


# DSF3: scripts_api.put_singles refuses, at save time, the media the sale boundary would refuse.
DRM_VIDEO='{"id": 22, "type": "video", "isReady": true}'   # no progressive full URL


def put_singles(database, monkeypatch, media, enabled=True):
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
        try:return await api.put_singles(api._ItemsBody(account_id=ACCOUNT,items=[item]))
        finally:await engine.dispose()
    return asyncio.run(run())


def seed_raw(database, raw):
    from test_engine_catalogue_pg import setup as engine_setup
    store,authority,*_=engine_setup(database)
    with database[1].begin() as c:
        c.execute(text("UPDATE vault_items SET raw_json=:r WHERE account_id=:a AND media_id=22"),{"r":raw,"a":ACCOUNT})
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


def test_put_singles_fansapi_drm_refused_native_drm_saves(database, monkeypatch):
    from fastapi import HTTPException
    from test_real_transport_pg import real_policy
    store,authority=seed_raw(database,DRM_VIDEO)
    assert put_singles(database,monkeypatch,[21,22])=={"status":"ok","items":1}   # native: DRM offerable
    real_policy(database,authority)
    with pytest.raises(HTTPException,match=r"media 22 cannot be sold \(drm\)"):
        put_singles(database,monkeypatch,[21,22])
    assert put_singles(database,monkeypatch,[21])["status"]=="ok"
    store.close()
