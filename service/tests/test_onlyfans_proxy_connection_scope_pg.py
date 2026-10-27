"""Existing credential owner must retain the reservation's exact handle."""
from datetime import datetime,timedelta,timezone
import pytest
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import settings,provision,data,save
from onlyfans_runtime.store import Store,Refused
from managed_proxy.store import Store as ProxyStore
from managed_proxy.provider import Candidate,address_hash

def test_connection_save_refuses_other_handle_reservation(database,monkeypatch):
    settings(monkeypatch)
    url,engine=database
    broker=ProxyStore(url.replace("postgresql+psycopg2:","postgresql:"),"42"*32)
    store=Store(url)
    try:
        broker.initialize()
        fixed="http://synthetic:secret@8.8.4.4:12323"
        broker.sync([Candidate(address_hash=address_hash("8.8.4.4"),country="US",order_id=123,
            expires_at=datetime.now(timezone.utc)+timedelta(days=10),url=fixed)])
        account=provision(store)
        broker.assign("tenant-a","creator-new","wrong_handle","US","op-session:"+"a"*64)
        with pytest.raises(Refused,match="managed network reservation changed"):
            save(store,account,{**data(),"proxyUrl":fixed})
    finally:
        broker.close();store.close()
