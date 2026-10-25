"""Real disposable PostgreSQL races; no provider or production connection."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import base64
import json
import os
from pathlib import Path
import sys
import uuid

import psycopg2
from psycopg2 import sql
import pytest

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/"service" if (root/"service").exists() else root))
from managed_proxy.provider import Candidate, ProxyError, address_hash
from managed_proxy.store import Store, canonical
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ACTOR="op-session:"+"a"*64


@pytest.fixture
def store():
    url=os.environ["ONLYFANS_PROXY_TEST_PG_URL"]
    admin=psycopg2.connect(url);admin.autocommit=True
    name="proxy_test_"+uuid.uuid4().hex
    with admin.cursor() as c:c.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    parts=psycopg2.extensions.parse_dsn(url);parts["dbname"]=name
    connection=psycopg2.connect(**parts)
    with connection,connection.cursor() as c:
        c.execute("CREATE TABLE of_creator_bindings(id text PRIMARY KEY,tenant_id text,creator_id text,handle text,revoked_at timestamp)")
        c.execute("CREATE TABLE of_provider_connection(account_id text PRIMARY KEY,binding_id text,revision integer,sealed_config text)")
    connection.close()
    value=Store(psycopg2.extensions.make_dsn(**parts),"42"*32);value.initialize()
    try:yield value
    finally:
        value.close()
        with admin.cursor() as c:c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def candidate(ip="8.8.4.4",**patch):
    args=dict(address_hash=address_hash(ip),country="US",order_id=123,expires_at=datetime.now(timezone.utc)+timedelta(days=10),url="http://fixture:secret@"+ip+":12323")
    return Candidate(**{**args,**patch})


def assign(store,handle="free_account",tenant="tenant-a",creator="creator-a",country="US"):
    return store.assign(tenant,creator,handle,country,ACTOR)


def legacy(store,ip="8.8.4.4",tenant="tenant-a",creator="old-creator",handle="old_handle"):
    nonce=b"1"*12
    clear=canonical({"proxyUrl":"http://fixture:secret@"+ip+":12323"}).encode()
    aad=canonical([tenant,"of-123","binding-a",1]).encode()
    sealed=base64.b64encode(nonce+AESGCM(store.key).encrypt(nonce,clear,aad)).decode()
    with store.transaction() as c:
        c.execute("INSERT INTO of_creator_bindings VALUES ('binding-a',%s,%s,%s,NULL)",(tenant,creator,handle))
        c.execute("INSERT INTO of_provider_connection VALUES ('of-123','binding-a',1,%s)",(sealed,))


def test_one_ip_cannot_be_claimed_twice_under_concurrent_creators(store):
    store.sync([candidate()])
    def claim(i):
        try:return store.assign("tenant-a","creator-"+str(i),"handle_"+str(i),"US",ACTOR)
        except ProxyError as error:return str(error)
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(claim,range(4)))
    assert sum(result.startswith("http:") for result in results)==1
    assert results.count("proxy_stock_empty")==3
    with store.transaction() as c:
        c.execute("SELECT count(*) AS n FROM onlyfans_proxy.audit");assert c.fetchone()["n"]==1


def test_same_creator_retries_converge_without_duplicate_assignments(store):
    store.sync([candidate(),candidate("8.8.8.8")])
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:assign(store),range(4)))
    assert len(set(results))==1
    assert store.stock()[0]["available"]==1
    assert "secret" not in json.dumps(store.stock())


def test_free_paid_and_tenant_accounts_are_isolated(store):
    store.sync([candidate(),candidate("8.8.8.8"),candidate("1.1.1.1")])
    assert len({assign(store),assign(store,"paid_account"),assign(store,tenant="tenant-b")})==3


def test_preexisting_proxy_never_enters_unassigned_stock(store):
    legacy(store);store.sync([candidate()])
    assert store.stock()==[]
    with pytest.raises(ProxyError,match="proxy_stock_empty"):assign(store)


def test_provider_credential_change_quarantines_assignment_and_never_recycles_it(store):
    store.sync([candidate()]);first=assign(store)
    store.sync([candidate(url="http://new:credential@8.8.4.4:12323")])
    with pytest.raises(ProxyError,match="assigned_proxy_needs_attention"):assign(store)
    with pytest.raises(ProxyError,match="proxy_stock_empty"):assign(store,"another_account")
    assert "fixture:secret" in first


def test_wrong_country_and_expired_or_stale_inventory_are_refused(store):
    store.sync([candidate()]);assign(store)
    with pytest.raises(ProxyError,match="assignment_country_conflict"):assign(store,country="GB")
    with store.transaction() as c:c.execute("UPDATE onlyfans_proxy.inventory SET observed_at=CURRENT_TIMESTAMP-INTERVAL '6 minutes'")
    with pytest.raises(ProxyError,match="assigned_proxy_needs_attention"):assign(store)


def test_missing_order_retires_but_never_reallocates_assignment(store):
    store.sync([candidate()]);assign(store)
    store.sync([]);store.sync([candidate()])
    with pytest.raises(ProxyError,match="assigned_proxy_needs_attention"):assign(store)
    assert store.stock()==[]


def test_existing_binding_can_attach_only_to_its_matching_assignment(store):
    store.sync([candidate()]);first=assign(store)
    legacy(store,creator="creator-a",handle="free_account");store.sync([candidate()])
    assert assign(store)==first
    with store.transaction() as c:
        c.execute("SELECT account_id FROM onlyfans_proxy.inventory");assert c.fetchone()["account_id"]=="of-123"
