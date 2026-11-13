"""W1 L3 (fans-api-adapter) red tests — review-only lane, engine code untouched.

Each test pins a written invariant (test id = finding id in docs/review/W1-fans-api-adapter/FINDINGS.md;
L3-01/02/03 exercise vault_sync's own create_catalogue_item — the catalog_terms side is L4 F01's rules test) (REVIEW.md @234155de) that e8e90954 breaks.
Real PostgreSQL (ONLYFANS_TEST_PG_URL, throwaway), httpx.MockTransport at the wire only.
Expected at e8e90954: every test here FAILS (red). Green = the finding is fixed.
G24 L3-05..08 tests live off-PR (premerge counts expected failures as skips); bodies kept by the fix orchestrator.
"""
from datetime import timedelta
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import VaultItem
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import configured, TENANT, ACCOUNT
from test_real_transport_pg import real_policy, _item
from test_engine_real_worker_pg import answer
from fake_fans_server import ACCT, CREATOR
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.real_transport import build_client, RealFansConfig
from fans_api_adapter.vault_sync import sync_vault, create_catalogue_item
from onlyfans_runtime.store import Refused, utcnow

DRM_VIDEO = dict(type='video', files={'thumb': {'url': 'https://cdn.example/t.jpg'}, 'full': {'url': None}})


def setup(database, script):
    """script(offset, limit) -> httpx.Response. Records every vault GET offset."""
    store, authority = configured(database, mode="automated")
    real_policy(database, authority)
    FansWebhookAdapter(store).bind_simulated(TENANT, ACCOUNT, ACCT, CREATOR)
    calls = []

    def handler(request):
        if request.url.path == "/api/accounts":
            return answer(200, [{"id": ACCT, "onlyfans_id": CREATOR, "is_authenticated": True}])
        q = request.url.params
        calls.append(int(q["offset"]))
        return script(int(q["offset"]), int(q["limit"]))
    client = build_client(RealFansConfig("synthetic", ACCT, "https://api.fansapi.com", 5),
                          creator_id=CREATOR, epoch=1, transport=httpx.MockTransport(handler))
    return store, client, calls


def listing(items):
    return lambda offset, limit: answer(200, {"data": {"list": items[offset:offset + limit],
                                                       "hasMore": offset + limit < len(items)}})


# L3-01/L3-02: not-ready / error / cannot_view / DRM items retained with removed_at NULL are accepted
# by vault_sync's own catalogue creator. Invariant: never offered (OQ-1 all items, OQ-3 DRM not offered).
@pytest.mark.parametrize("flags", [dict(isReady=False), dict(hasError=True), dict(canView=False), DRM_VIDEO],
                         ids=["not_ready", "error", "cannot_view", "drm"])
def test_w1_l3_01_02_unready_or_drm_item_cannot_become_catalogue_item(database, flags):
    store, client, _ = setup(database, listing([_item(11), _item(12, **flags)]))
    assert sync_vault(store, TENANT, ACCOUNT, client)["complete"]
    with pytest.raises(Refused):
        create_catalogue_item(store, TENANT, ACCOUNT, [12], price_cents=500, label="x", description_for_ai="x")
    store.close()


# L3-01b: an item previously retired (removed_at set) is resurrected when re-listed while still unusable.
def test_w1_l3_01b_retired_unready_item_stays_unsellable_after_resync(database):
    store, client, _ = setup(database, listing([_item(11), _item(12, isReady=False)]))
    assert sync_vault(store, TENANT, ACCOUNT, client)["complete"]
    with Session(store.engine) as s, s.begin():
        s.get(VaultItem, (ACCOUNT, 12)).removed_at = utcnow() - timedelta(days=1)   # retired by the 659ea29 importer
    store2, client2, _ = setup_again(database, store, listing([_item(11), _item(12, isReady=False)]))
    assert sync_vault(store2, TENANT, ACCOUNT, client2, command_key="b" * 64)["complete"]
    with pytest.raises(Refused):   # invariant: still unsellable (any fix shape: removed_at kept or readiness gate)
        create_catalogue_item(store, TENANT, ACCOUNT, [12], price_cents=500, label="x", description_for_ai="x")
    store.close()


def setup_again(database, store, script):
    calls = []

    def handler(request):
        if request.url.path == "/api/accounts":
            return answer(200, [{"id": ACCT, "onlyfans_id": CREATOR, "is_authenticated": True}])
        q = request.url.params
        calls.append(int(q["offset"]))
        return script(int(q["offset"]), int(q["limit"]))
    client = build_client(RealFansConfig("synthetic", ACCT, "https://api.fansapi.com", 5),
                          creator_id=CREATOR, epoch=1, transport=httpx.MockTransport(handler))
    return store, client, calls


# L3-03: vanished item (absent from a complete pass, not isDeleted) stays sellable.
def test_w1_l3_03_vanished_item_cannot_become_catalogue_item(database):
    items = [_item(11), _item(12)]
    store, client, _ = setup(database, lambda o, l: listing(items)(o, l))
    assert sync_vault(store, TENANT, ACCOUNT, client)["complete"]
    del items[1]
    _, client2, _ = setup_again(database, store, lambda o, l: listing(items)(o, l))
    assert sync_vault(store, TENANT, ACCOUNT, client2, command_key="c" * 64)["complete"]
    with pytest.raises(Refused):
        create_catalogue_item(store, TENANT, ACCOUNT, [12], price_cents=500, label="x", description_for_ai="x")
    store.close()


# L3-P1 (GREEN proof, not a defect): reconnect to another provider account is refused while a send
# (text intent + independent admission) is unknown, and the active connection/epoch is untouched.
def test_w1_l3_p1_reconnect_refused_while_send_unknown(database):
    from test_engine_independence_pg import setup as independence_setup, arithmetic
    from test_onlyfans_text_delivery_pg import unthrottle
    from db.onlyfans_engine_models import OfFansConnection, OfEngineIndependentAdmission
    store, authority, engine, provider, wire, first = independence_setup(database, ('accepted_lost', 'unknown'))
    decision, intent = arithmetic(engine, store)
    unthrottle(database[1])
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'unknown'
    with Session(database[1]) as s:
        assert s.scalar(select(OfEngineIndependentAdmission.status).where(
            OfEngineIndependentAdmission.intent_id == intent)) == 'unknown'
        before = [(c.epoch, c.status, c.provider_account_id) for c in s.scalars(select(OfFansConnection))]
    with pytest.raises(Refused, match='reconcile unresolved sends'):
        FansWebhookAdapter(store).bind_simulated(TENANT, ACCOUNT, 'acct_other', 'creator-other')
    with Session(database[1]) as s:
        assert [(c.epoch, c.status, c.provider_account_id) for c in s.scalars(select(OfFansConnection))] == before
    store.close()
