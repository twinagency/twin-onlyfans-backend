"""CP4 6.D / CP5 7.E (roles): a creator, or agency staff scoped to one creator, can never read,
send through, re-budget or take over another creator's account, and a read-only member can
not activate or send at all.

Real PostgreSQL (disposable ``of_test_`` databases) and the real FastAPI routers behind the
real ``AgencyScopeMiddleware``. Two accounts in ONE tenant (the worst case: the tenant check
cannot help), the SAME fan id on both (a fan id is only unique within an account), creator A
and creator B. Synthetic data only; no provider.
"""
import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from test_fastt_controls_pg import template, database          # noqa: F401  (fixtures: bootstrap once, clone per test)
from test_onlyfans_foundation_pg import seed
from test_onlyfans_agency_pg import CREATOR, OTHER, STAFF, headers, assign
from test_onlyfans_text_delivery_pg import TENANT, current
from db.models import OfConversationState
from onlyfans_read.agency_scope import AgencyScopeMiddleware, HEADER as SCOPE_HEADER
from onlyfans_runtime.text_delivery import TextAuthority
from onlyfans_runtime.store import Refused

A, B, FAN = "acc-a", "acc-b", 101
OPS = "op-session:" + "b" * 64
CONNECTION_KEY = "ab" * 32

# family prefix -> (token env, version header, schema value)
FAMILIES = {
    "/onlyfans/api/v1": ("ONLYFANS_API_TOKEN", "X-OnlyFans-Schema-Version", "of-preview.v2"),
    "/onlyfans/control/v1": ("ONLYFANS_CONTROL_TOKEN", "X-OnlyFans-Control-Version", "of-controls.v1"),
    "/onlyfans/configuration/v1": ("ONLYFANS_CONFIGURATION_TOKEN", "X-OnlyFans-Configuration-Version", "of-configuration.v3"),
    "/onlyfans/connection/v1": ("ONLYFANS_CONNECTION_TOKEN", "X-OnlyFans-Connection-Version", "of-connections.v1"),
    "/onlyfans/campaign/v1": ("ONLYFANS_CAMPAIGN_TOKEN", "X-OnlyFans-Campaign-Version", "of-campaigns.v2"),
    "/onlyfans/content/v1": ("ONLYFANS_CONTENT_TOKEN", "X-OnlyFans-Content-Version", "of-content.v1"),
    "/onlyfans/content/v2": ("ONLYFANS_CONTENT_TOKEN", "X-OnlyFans-Content-Version", "of-content.v2"),
}


def request_headers(path, creators=(CREATOR,), manage=True):
    for prefix, (env, header, schema) in FAMILIES.items():
        if path.startswith(prefix + "/"):
            return {**headers(creators=creators, manage=manage), "Authorization": "Bearer tok-" + env, header: schema}
    return None


@pytest.fixture
def world(database, monkeypatch):
    url, engine = database
    store = seed(url, engine, account=A, tenant=TENANT, fan=FAN)
    seed(url, engine, account=B, tenant=TENANT, fan=FAN)     # same fan id, other account
    assign(engine, A, CREATOR)
    assign(engine, B, OTHER)
    for account in (A, B):
        current(engine, mode="automated", account=account, fan=FAN)
    authority = TextAuthority(store)
    for account in (A, B):
        authority.configure(TENANT, account, expected_revision=0, enabled=True, limit_units=20,
                            throttle_seconds=1, actor="synthetic-operator", reason="iso45", transport_kind="native")
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_CONNECTION_KEY", CONNECTION_KEY)
    monkeypatch.setenv("ONLYFANS_MANUAL_ENABLED", "1")
    for env, _, _ in FAMILIES.values():
        monkeypatch.setenv(env, "tok-" + env)
        monkeypatch.setenv(env.replace("_TOKEN", "_TENANT"), TENANT)
    monkeypatch.setenv("ONLYFANS_API_TENANT", "deployment")
    from onlyfans_read import api, repo
    import importlib
    engine_module = importlib.import_module("db.engine")
    aengine = create_async_engine(make_url(url).set(drivername="postgresql+asyncpg"), poolclass=NullPool)
    factory = async_sessionmaker(aengine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with factory() as session:
            yield session
    monkeypatch.setattr(repo, "get_session", sessions)
    monkeypatch.setattr(engine_module, "get_session", sessions)
    from onlyfans_commands.api import router as control
    from onlyfans_commands.configuration_api import router as configuration
    from onlyfans_commands.connection_api import router as connection
    from onlyfans_commands.campaign_api import router as campaign
    from onlyfans_commands.content_api import router as content
    from onlyfans_commands.content_api_v2 import router as content_v2
    from onlyfans_commands.vault_api import router as vault
    app = FastAPI()
    app.add_middleware(AgencyScopeMiddleware)
    for router in (api.router, control, configuration, connection, campaign, content, content_v2, vault):
        app.include_router(router)
    app.state.control_store = store
    yield TestClient(app, raise_server_exceptions=False), store, engine, authority
    store.close()
    asyncio.run(aengine.dispose())


def snapshot(engine):
    """Everything an escalation could change on the victim account B (and nothing of A's)."""
    tables = ["of_delivery_policy", "of_runtime", "of_conversation_state", "of_control_audit", "of_text_intent",
              "of_provider_connection", "of_delivery_policy_change", "of_configuration_change", "skip_list",
              "account_ai_config", "of_catalog_changes"]
    out = {}
    with engine.connect() as c:
        for table in tables:
            exists = c.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar()
            if exists is None:
                continue
            cols = [r[0] for r in c.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name=:t ORDER BY ordinal_position"), {"t": table})]
            if "account_id" not in cols:
                continue
            order = ",".join(f'"{col}"' for col in cols)
            out[table] = [tuple(map(str, r)) for r in c.execute(
                text(f'SELECT {order} FROM {table} WHERE account_id=:a ORDER BY {order}'), {"a": B})]
    return out


def route_cases():
    """(method, templated path) for every account-scoped route of every mounted router."""
    return None


def concrete(path, account):
    values = {"account_id": account, "conversation_id": f"{account}:{FAN}", "item_id": "1", "media_id": "1",
              "folder_id": "1", "campaign_id": "00000000-0000-4000-8000-000000000001"}
    def fill(match):
        return values.get(match.group(1), "00000000-0000-4000-8000-000000000001")
    return re.sub(r"\{(\w+)(?::[^}]*)?\}", fill, path)


def key(label):
    return hashlib.sha256(label.encode()).hexdigest()


# ---------------------------------------------------------------------------------------------
def test_every_account_route_refuses_creator_a_on_creator_bs_account_and_changes_nothing(world):
    client, store, engine, _ = world
    before = snapshot(engine)
    assert before.get("of_delivery_policy") and len(before) >= 6, sorted(before)
    seen, refused_for_scope, statuses = 0, 0, {}
    for route in client.app.routes:
        path = getattr(route, "path", "")
        if "{account_id}" not in path:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            url = concrete(path, B)
            hdr = request_headers(url)
            if hdr is None:
                continue
            seen += 1
            kwargs = {"json": {}} if method in {"POST", "PUT"} else {}
            r = client.request(method, url, headers=hdr, **kwargs)
            statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
            assert not 200 <= r.status_code < 300, (method, url, r.status_code, r.text[:200])
            assert r.status_code != 500, (method, url, r.text[:200])
            refused_for_scope += r.status_code in (403, 404)
    assert seen >= 25, seen
    assert snapshot(engine) == before, "a refused cross-creator call must not change B at all"
    assert refused_for_scope >= 10, statuses


def test_creator_a_can_not_read_bs_messages_media_or_handover_but_can_read_her_own(world):
    client, _, engine, _ = world
    hdr = request_headers("/onlyfans/api/v1/accounts")
    base = "/onlyfans/agency/v1"
    own = client.get(base + f"/api/v1/accounts/{A}/conversations", headers=hdr)
    assert own.status_code == 200, own.text
    listing = client.get(base + "/api/v1/accounts", headers=hdr).json()["accounts"]
    assert [x["accountId"] for x in listing] == [A], "the account list names only creator A's account"
    for suffix in ("conversations", "ledger", "operations", "workspace/content", "workspace/vault",
                   f"conversations/{B}:{FAN}", f"conversations/{B}:{FAN}/handover"):
        r = client.get(base + f"/api/v1/accounts/{B}/" + suffix, headers=hdr)
        assert r.status_code in (400, 403, 404), (suffix, r.status_code, r.text[:160])
        assert B not in r.text.replace(f"/{B}", "") or r.status_code == 404
    # B's fan id is the SAME number as A's fan; the conversation id carries the account, so A's id never reaches B.
    mixed = client.get(base + f"/api/v1/accounts/{B}/conversations/{A}:{FAN}", headers=hdr)
    assert mixed.status_code in (400, 404)
    # A's creator id is not accepted as a filter to reach B either.
    other = client.get(base + f"/api/v1/accounts?creator={OTHER}", headers=hdr)
    assert other.status_code in (200, 400, 404) and not [x for x in other.json().get("accounts", []) if x["accountId"] == B]
    # Vault media and items belong to the account: B's are refused for A, A's are not a scope failure.
    for method, path in (("GET", f"/onlyfans/content/v1/accounts/{B}/items/1"),
                         ("GET", f"/onlyfans/content/v2/accounts/{B}/items/1"),
                         ("GET", f"/onlyfans/content/v1/accounts/{B}/vault"),
                         ("GET", f"/onlyfans/content/v1/accounts/{B}/vault/assets/1"),
                         ("GET", f"/onlyfans/content/v1/accounts/{B}/vault/previews/1")):
        hdr2 = request_headers(path)
        if hdr2 is None:
            continue
        r = client.request(method, path, headers=hdr2)
        assert not 200 <= r.status_code < 300, (path, r.status_code)


def test_creator_a_can_not_use_bs_connection_or_change_bs_budget_policy_or_configuration(world):
    client, store, engine, _ = world
    before = snapshot(engine)
    conn = "/onlyfans/connection/v1/accounts/"
    h = request_headers(conn)
    cmd = lambda action, payload, rev=0: {"action": action, "payload": payload, "expectedRevision": rev,
                                          "actor": STAFF, "commandKey": key(f"{action}-{rev}")}
    activate = {"limitUnits": 9999, "throttleSeconds": 60, "mediaEnabled": True, "automateNewConversations": True}
    # Connection: the agency namespace has no connection route at all (403 for A AND for her own account);
    # the store itself still refuses a scoped actor for B's account (defence in depth below the router).
    assert client.get(conn + B, headers=h).status_code == 403
    assert client.get(conn + A, headers=h).status_code == 403
    for body in (cmd("pause", {}), cmd("activate", activate), cmd("verify", {}),
                 cmd("save", {"ownerId": 1, "handle": "x", "capture": {}})):
        assert client.post(conn + B, headers=h, json=body).status_code == 403, body["action"]
    from onlyfans_read.agency_scope import AgencyScope, current_scope
    from onlyfans_runtime.connections import change_connection, get_connection
    token = current_scope.set(AgencyScope((CREATOR,), STAFF, True))
    try:
        with pytest.raises(Refused, match="scope"):
            get_connection(store, TENANT, B)
        for action in ("pause", "activate", "verify"):
            with pytest.raises(Refused, match="scope"):
                change_connection(store, TENANT, B, action=action, payload=activate if action == "activate" else {},
                                  expected_revision=0, command_key=key("c" + action), actor=OPS)
        assert get_connection(store, TENANT, A)["accountId"] == A, "positive control: her own account is readable"
    finally:
        current_scope.reset(token)
    # Configuration (budget/persona/model): B refused, A's own reaches the account.
    cfg = "/onlyfans/configuration/v1/accounts/"
    hc = request_headers(cfg)
    patch = {"patch": {"dailyCostCapCents": 123456}, "expectedRevision": 0, "expectedFingerprint": "0" * 64,
             "commandKey": key("cfg"), "actor": STAFF}
    assert client.get(cfg + B, headers=hc).status_code == 404
    r = client.post(cfg + B, headers=hc, json=patch)
    assert r.status_code == 404 and r.json()["code"] == "not_available", r.text
    assert client.get(cfg + A, headers=hc).status_code in (200, 404, 503)    # not a 403: scope allows A
    # Delivery policy (budget units) and runtime flag on B are untouched.
    assert snapshot(engine) == before
    with engine.connect() as c:
        assert c.execute(text("SELECT limit_units FROM of_delivery_policy WHERE account_id=:a"), {"a": B}).scalar() == 20


def test_creator_a_can_not_take_over_or_control_bs_fans_even_with_the_same_fan_id(world):
    client, store, engine, _ = world
    path = lambda account, fan_account=None: (f"/onlyfans/control/v1/accounts/{account}/conversations/"
                                                f"{fan_account or account}:{FAN}")
    h = request_headers("/onlyfans/control/v1/x")
    body = lambda action, rev=0: {"action": action, "expectedRevision": rev, "commandKey": key(f"{action}{rev}"), "actor": STAFF}
    before = snapshot(engine)
    for action in ("request_takeover", "pause_automation", "mark_no_contact", "release_takeover", "resume_automation"):
        r = client.post(path(B), headers=h, json=body(action))
        assert r.status_code == 404 and r.json()["code"] == "scope_unavailable", (action, r.status_code, r.text)
    r = client.post(path(B), headers=h, json={**body("manual_message"), "body": "hello from the wrong creator"})
    assert r.status_code in (400, 404), r.text
    r = client.post(f"/onlyfans/control/v1/accounts/{B}/ledger/retry", headers=h, json={
        "commandKey": key("retry"), "expectedConnectionRevision": 1, "expectedScanFingerprint": "0" * 64})
    assert r.status_code in (400, 404), r.text
    # Addressing B's account with A's conversation id is a malformed id, not a takeover of A's fan either.
    r = client.post(path(B, A), headers=h, json=body("request_takeover"))
    assert r.status_code in (400, 404)
    assert snapshot(engine) == before, "B's conversation, skip_list, audit and intents are untouched"
    with Session(engine) as s:
        assert s.get(OfConversationState, (B, FAN)).mode == "automated"
        assert s.get(OfConversationState, (A, FAN)).mode == "automated", "and A's same-numbered fan is untouched too"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM skip_list")).scalar() == 0
    # Positive control: the same call on her own account works and moves ONLY her fan.
    r = client.post(path(A), headers=h, json=body("request_takeover"))
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert s.get(OfConversationState, (A, FAN)).mode == "human_controlled"
        assert s.get(OfConversationState, (B, FAN)).mode == "automated"
    with engine.connect() as c:
        assert [r[0] for r in c.execute(text("SELECT account_id FROM skip_list"))] == [A]
    # Her colleague-at-another-creator identity can not release it either.
    r = client.post(path(A), headers=request_headers("/onlyfans/control/v1/x", creators=(OTHER,)),
                    json=body("release_takeover", 1))
    assert r.status_code == 404


def test_read_only_members_can_not_activate_send_pause_or_edit_anything(world):
    client, store, engine, _ = world
    ro = lambda path: request_headers(path, manage=False)
    conv = f"/onlyfans/control/v1/accounts/{A}/conversations/{A}:{FAN}"
    cases = [
        (conv, {"action": "request_takeover", "expectedRevision": 0, "commandKey": key("ro1"), "actor": STAFF}),
        (conv, {"action": "manual_message", "expectedRevision": 0, "commandKey": key("ro2"), "actor": STAFF, "body": "x"}),
        (conv, {"action": "pause_automation", "expectedRevision": 0, "commandKey": key("ro3"), "actor": STAFF}),
        (f"/onlyfans/control/v1/accounts/{A}/ledger/retry", {"commandKey": key("ro4"), "expectedConnectionRevision": 1,
                                                              "expectedScanFingerprint": "0" * 64}),
        (f"/onlyfans/connection/v1/accounts/{A}", {"action": "activate", "payload": {"limitUnits": 5, "throttleSeconds": 1,
            "mediaEnabled": False}, "expectedRevision": 1, "actor": STAFF, "commandKey": key("ro5")}),
        (f"/onlyfans/connection/v1/accounts/{A}", {"action": "pause", "payload": {}, "expectedRevision": 1,
                                                    "actor": STAFF, "commandKey": key("ro6")}),
        (f"/onlyfans/configuration/v1/accounts/{A}", {"patch": {"dailyCostCapCents": 1}, "expectedRevision": 0,
                                                       "expectedFingerprint": "0" * 64, "commandKey": key("ro7"), "actor": STAFF}),
        (f"/onlyfans/campaign/v1/accounts/{A}/campaigns", {}),
        (f"/onlyfans/content/v1/accounts/{A}/items/1", {}),
        (f"/onlyfans/content/v2/accounts/{A}/items/1", {}),
        (f"/onlyfans/vault/v1/accounts/{A}/items", {}),
    ]
    def full():
        with engine.connect() as c:
            return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in
                    ("of_control_audit", "of_text_intent", "skip_list", "of_delivery_policy_change")}
    base = full()
    for path, body in cases:
        hdr = ro(path) or request_headers("/onlyfans/content/v1/x", manage=False)
        r = client.post(path, headers=hdr, json=body)
        assert r.status_code == 403 and r.json()["code"] == "agency_scope_denied", (path, r.status_code, r.text[:160])
    for method in ("PUT", "DELETE", "PATCH"):
        r = client.request(method, conv, headers=ro(conv), json={})
        assert r.status_code in (403, 405), (method, r.status_code)
    assert full() == base
    # Reads stay available to the same read-only member.
    assert client.get(f"/onlyfans/agency/v1/api/v1/accounts/{A}/conversations", headers=request_headers("/onlyfans/api/v1/accounts")
                      ).status_code == 200
    r = client.get(f"/onlyfans/agency/v1/api/v1/accounts/{A}/conversations", headers={
        **request_headers("/onlyfans/api/v1/accounts", manage=False)})
    assert r.status_code == 200


def test_a_forged_or_other_users_actor_can_not_ride_a_valid_scope(world):
    client, store, engine, _ = world
    path = f"/onlyfans/control/v1/accounts/{A}/conversations/{A}:{FAN}"
    h = request_headers(path)
    colleague = "agency-user:33333333-3333-4333-8333-333333333333:55555555-5555-4555-8555-555555555555"
    for actor in (colleague, "op-session:" + "a" * 64, "someone", ""):
        r = client.post(path, headers=h, json={"action": "request_takeover", "expectedRevision": 0,
                                               "commandKey": key("f" + actor[:8]), "actor": actor})
        assert r.status_code == 400, (actor, r.status_code, r.text)
    with Session(engine) as s:
        assert s.get(OfConversationState, (A, FAN)).mode == "automated"
