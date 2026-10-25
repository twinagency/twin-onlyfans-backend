"""Real PostgreSQL pool reservations, synthetic credentials, no provider network."""
import json
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select,func
from sqlalchemy.orm import Session
import pytest
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import settings,provision,save,data
from onlyfans_runtime.store import Store,Refused
from onlyfans_runtime.proxy_pool import reserve_proxy,inventory
from db.onlyfans_connection_models import OfProxyAssignment

def pool(monkeypatch,n=3):
    monkeypatch.setenv("ONLYFANS_FIXED_PROXY_POOL_JSON",json.dumps([
        {"id":"fixed-"+str(i),"url":"http://synthetic:secret@proxy-"+str(i)+".invalid:8080"} for i in range(n)]))

def test_same_creator_retries_return_same_encrypted_reservation(database,monkeypatch):
    settings(monkeypatch);pool(monkeypatch);store=Store(database[0])
    with ThreadPoolExecutor(max_workers=8) as ex:
        values=list(ex.map(lambda _:reserve_proxy(store,"agency-a","creator-a"),range(8)))
    assert len(set(values))==1
    with Session(database[1]) as s:
        rows=s.scalars(select(OfProxyAssignment)).all()
        assert len(rows)==1 and values[0] not in rows[0].sealed_proxy
    # Removing inventory must not rotate a creator's reserved login network.
    monkeypatch.setenv("ONLYFANS_FIXED_PROXY_POOL_JSON","[]")
    assert reserve_proxy(store,"agency-a","creator-a")==values[0]
    store.close()

def test_tenants_and_creators_never_share_and_pool_exhaustion_is_explicit(database,monkeypatch):
    settings(monkeypatch);pool(monkeypatch,2);store=Store(database[0])
    with ThreadPoolExecutor(max_workers=2) as ex:
        values=list(ex.map(lambda scope:reserve_proxy(store,*scope),[("agency-a","same-creator"),("agency-b","same-creator")]))
    assert len(set(values))==2
    with pytest.raises(Refused,match="proxy pool exhausted"):reserve_proxy(store,"agency-c","creator-c")
    store.close()

def test_manually_connected_proxy_is_not_reallocated(database,monkeypatch):
    settings(monkeypatch);store=Store(database[0]);account=provision(store);save(store,account,data())
    monkeypatch.setenv("ONLYFANS_FIXED_PROXY_POOL_JSON",json.dumps([{"id":"already-used","url":data()["proxyUrl"]}]))
    with pytest.raises(Refused,match="proxy pool exhausted"):reserve_proxy(store,"other-agency","other-creator")
    store.close()

@pytest.mark.parametrize("raw",[
    "{}","[null]",
    '[{"id":"a","url":"http://private:password@proxy.invalid:99999"}]',
    '[{"id":"a","url":"http://proxy.invalid:80"},{"id":"b","url":"HTTP://PROXY.INVALID:80/"}]',
    '[{"id":"a","url":"http://proxy.invalid:80"},{"id":"a","url":"http://other.invalid:80"}]'
])
def test_invalid_inventory_does_not_leak_credentials(raw):
    with pytest.raises(Refused,match="^proxy pool configuration is unavailable$"):inventory(raw)
