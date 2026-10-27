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


def test_country_preference_does_not_replace_existing_assignment_and_stale_inventory_is_refused(store):
    store.sync([candidate()]);assign(store)
    assert assign(store,country="GB")==assign(store)
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

def test_new_account_uses_other_country_when_preferred_country_has_no_stock(store):
    proxy=candidate(country="NL")
    store.sync([proxy])
    assert assign(store,country="US")==proxy.url
    assert store.stock()==[]


def test_available_preferred_country_is_used_before_other_countries(store):
    preferred=candidate(country="US")
    alternative=candidate("1.1.1.1",country="NL",expires_at=datetime.now(timezone.utc)+timedelta(days=20))
    store.sync([preferred,alternative])
    assert assign(store)==preferred.url
    assert assign(store,handle="second_account")==alternative.url


def test_cross_country_fallback_does_not_share_proxy_under_concurrent_claims(store):
    store.sync([candidate(country="NL"),candidate("1.1.1.1",country="PT")])
    def claim(i):
        try:return store.assign("tenant-a","creator-"+str(i),"handle_"+str(i),"US",ACTOR)
        except ProxyError as error:return str(error)
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(claim,range(4)))
    assigned=[r for r in results if r.startswith("http:")]
    assert len(assigned)==2 and len(set(assigned))==2
    assert results.count("proxy_stock_empty")==2

@pytest.fixture
def recovery(store):
    with store.transaction() as c:
        c.execute("CREATE TABLE of_connection_enrollment(tenant_id text,creator_id text)")
    store.sync([candidate(),candidate("8.8.8.8")])
    assign(store)
    return store

def reclaim(store,**patch):
    row=store.reservations("tenant-a")[0]
    args=dict(creator="creator-a",address=row["addressHash"],reservation=row["reservationId"],
        command="a"*64,actor=ACTOR,reason="Verified never-used setup reservation")
    return store.reclaim("tenant-a",**{**args,**patch})

def test_reclaim_unused_exact_retry_retains_audit_and_unrelated_stock(recovery):
    row=recovery.reservations("tenant-a")[0]
    assert reclaim(recovery)
    assert recovery.stock()[0]["available"]==2
    assigned=assign(recovery,creator="creator-b")
    assert recovery.reclaim("tenant-a",creator="creator-a",address=row["addressHash"],
        reservation=row["reservationId"],command="a"*64,actor=ACTOR,
        reason="Verified never-used setup reservation")
    assert len(recovery.reservations("tenant-a"))==1
    with recovery.transaction() as c:
        c.execute("SELECT count(*) AS n FROM onlyfans_proxy.audit WHERE action='reclaim'")
        assert c.fetchone()["n"]==1

@pytest.mark.parametrize("patch",[{"creator":"creator-b"},{"reservation":"b"*64},
    {"actor":"creator-session:"+"a"*64},{"reason":""}])
def test_reclaim_refuses_wrong_owner_identity_and_non_management_actor(recovery,patch):
    with pytest.raises(ProxyError):reclaim(recovery,**patch)
    assert len(recovery.reservations("tenant-a"))==1

@pytest.mark.parametrize("reference",["browser","binding","enrollment","revoked_saved_proxy"])
def test_reclaim_refuses_every_used_or_saved_reference(recovery,reference):
    if reference=="browser":
        url=assign(recovery)
        assert recovery.begin_use("tenant-a","creator-a",url,ACTOR)
        assert recovery.begin_use("tenant-a","creator-a",url,ACTOR)
    elif reference=="binding":
        with recovery.transaction() as c:
            c.execute("INSERT INTO of_creator_bindings VALUES ('active','tenant-a','creator-a','free_account',NULL)")
    elif reference=="enrollment":
        with recovery.transaction() as c:c.execute("INSERT INTO of_connection_enrollment VALUES ('tenant-a','creator-a')")
    else:
        from urllib.parse import urlsplit
        legacy(recovery,ip=urlsplit(assign(recovery)).hostname)
        with recovery.transaction() as c:c.execute("UPDATE of_creator_bindings SET revoked_at=CURRENT_TIMESTAMP")
    with pytest.raises(ProxyError):reclaim(recovery)
    assert len(recovery.reservations("tenant-a"))==1

def test_concurrent_browser_admission_and_reclaim_have_one_winner(recovery):
    from threading import Barrier
    url=assign(recovery);barrier=Barrier(2)
    def use():
        barrier.wait()
        try:return recovery.begin_use("tenant-a","creator-a",url,ACTOR)
        except ProxyError:return False
    def release():
        barrier.wait()
        try:return reclaim(recovery)
        except ProxyError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(use);b=pool.submit(release)
        assert sum([a.result(),b.result()])==1
    rows=recovery.reservations("tenant-a")
    if rows:assert rows[0]["used"] is True

def test_standalone_pending_owner_resolves_from_capture_and_retry_keeps_network(recovery):
    import hashlib
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from managed_proxy.reservation import reserve_network
    creator="standalone-creator"
    pending="pending_"+hashlib.sha256(creator.encode()).hexdigest()[:32]
    actor="creator-session:"+"b"*64
    url=recovery.assign("tenant-a",creator,pending,"US",actor)
    recovery.begin_use("tenant-a",creator,url,actor)
    parts=psycopg2.extensions.parse_dsn(os.environ["ONLYFANS_PROXY_TEST_PG_URL"])
    with recovery.transaction() as c:parts["dbname"]=c.connection.info.dbname
    engine=create_engine("postgresql+psycopg2://",creator=lambda:psycopg2.connect(**parts))
    try:
        with Session(engine) as s,s.begin():
            assert reserve_network(s,"tenant-a",creator,url,recovery.key,actor,required=True,captured_handle="verified_handle")
        assert recovery.assign("tenant-a",creator,pending,"US",actor)==url
        with Session(engine) as s,s.begin():
            with pytest.raises(ProxyError):
                reserve_network(s,"tenant-a",creator,url,recovery.key,actor,required=True,captured_handle="other_handle")
        row=next(r for r in recovery.reservations("tenant-a") if r["creatorId"]==creator)
        assert row["handle"]=="verified_handle" and row["used"]
        with pytest.raises(ProxyError):
            recovery.reclaim("tenant-a",creator=creator,address=row["addressHash"],reservation=row["reservationId"],
                command="b"*64,actor=ACTOR,reason="Must refuse captured credentials")
    finally:engine.dispose()
