"""Release-candidate join: one migration chain, one real transport registration, kind-bound gates.

No provider call and no model call. The real transport is only exercised against the loopback fake.
"""
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from test_onlyfans_media_pg import shelf, media_policy
from fake_fans_server import FakeFans, ACCT, CREATOR
from conversation_engine.catalogue import (SyntheticCatalogueCoverage, VaultCatalogueCoverage, search_scoped,
                                           synthetic_coverage, vault_coverage)
from conversation_engine.commerce import (ProviderAccessObservation, RealCatalogueCoverage, SyntheticAccessObservation,
                                          SyntheticCoverage, approve_catalogue_offer, approve_simulated_offer,
                                          record_provider_access, record_simulated_access, verified_terms)
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from db.models import VaultItem
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineOfferGrant, OfEngineOwner
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.real_transport import (FansApiRealTransport, RealFansConfig, build_client,
                                             build_real_transport, real_transport_enabled)
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport


# ---------------------------------------------------------------- migration chain

def _revisions():
    from alembic.script import ScriptDirectory
    from db.schema import HERE, _config
    from alembic.config import Config
    cfg = Config(str(HERE / 'migrations/alembic.ini'))
    cfg.set_main_option('script_location', str(HERE / 'migrations'))
    return ScriptDirectory.from_config(cfg)


def test_single_linear_head_and_declared_chain():
    from db.schema import ENGINE_APPROVAL, ENGINE_GENERATIONS, ENGINE_MODEL_CALLS, FANS_INTAKE, FANSAPI_TRANSPORT, HEAD, PLAN_C_SCOPE, PLAN_C_INGRESS, OFFER_GRANT_LINEAGE, MODEL_BILLING_EVIDENCE, RECONNECT_RECEIPT, MODEL_RETRY_DUE, COMMERCIAL_SCOPE, CATALOGUE_TERMS, HISTORY_COVERAGE, REVISION_RANK
    script = _revisions()
    assert script.get_heads() == [HEAD] == [HISTORY_COVERAGE]  # commercial line: 0129 -> 0129b (R0) -> 0130..0133 -> 0134 (step 2) -> 0135 (step 3) -> 0136 (step 1)
    chain = [r.revision for r in script.walk_revisions('base', HEAD)]
    assert chain[:19] == ['0136_history_coverage', '0135_commercial_catalogue_terms', '0134_commercial_scope_contract', '0133_preparation_retry_state', '0132_account_preparation', '0131_manual_category_prices', '0130_commercial_vault_provenance', '0129b_model_retry_due', '0129_reconnect_receipt', '0128_model_billing_evidence', '0127_offer_grant_lineage', '0126_plan_c_ingress', '0125_plan_c_scope', '0124_engine_approval', '0123_engine_model_calls', '0122_fansapi_transport', '0121_engine_generations', '0120_fans_intake', '0119_identity_independence']
    assert [REVISION_RANK[v] for v in (FANS_INTAKE, ENGINE_GENERATIONS, FANSAPI_TRANSPORT, ENGINE_MODEL_CALLS, ENGINE_APPROVAL, PLAN_C_SCOPE, PLAN_C_INGRESS, OFFER_GRANT_LINEAGE, MODEL_BILLING_EVIDENCE, RECONNECT_RECEIPT, MODEL_RETRY_DUE)] == [120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 129]
    # no fork anywhere above the live Moxy base (older history legitimately contains merge points)
    assert all(len(r.nextrev) <= 1 for r in script.walk_revisions('0111_moxy_owner', HEAD))


def test_upgrade_from_0111_to_head_downgrade_and_upgrade_again(database):
    from alembic import command
    from db.schema import HEAD, _config, check_schema
    with database[1].begin() as c:
        command.downgrade(_config(c), '0111_moxy_owner')
    with database[1].connect() as c:
        check_schema(c, expected='0111_moxy_owner')
        assert c.scalar(text("SELECT to_regclass('of_fans_link')")) is None
        assert c.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_name='of_engine_decision' AND column_name='generation'")) == 0
    with database[1].begin() as c:
        command.upgrade(_config(c), HEAD)
    with database[1].connect() as c:
        check_schema(c)
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    with database[1].begin() as c:
        command.downgrade(_config(c), '0111_moxy_owner')
        command.upgrade(_config(c), HEAD)
    with database[1].connect() as c:
        check_schema(c)


def test_fansapi_downgrade_refuses_while_a_real_policy_exists(database):
    from alembic import command
    from db.schema import _config
    store, authority = configured(database, mode='automated')
    _real_policy(database, authority)
    with pytest.raises(RuntimeError, match='fansapi'):
        with database[1].begin() as c:
            command.downgrade(_config(c), '0121_engine_generations')
    with database[1].connect() as c:
        assert c.scalar(text("SELECT transport_kind FROM of_delivery_policy")) == 'fansapi'
    store.close()


# ---------------------------------------------------------------- helpers

def _real_policy(database, authority):
    with Session(database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        args = dict(expected_revision=policy.revision, enabled=policy.enabled, limit_units=policy.limit_units,
                    throttle_seconds=policy.throttle_seconds)
    authority.configure(TENANT, ACCOUNT, actor='join-test', reason='real provider kind', transport_kind='fansapi', **args)


def _kind(database):
    with Session(database[1]) as s:
        return s.get(OfDeliveryPolicy, ACCOUNT).transport_kind


def _build(database, kind, *, offer=False):
    store, authority = configured(database, mode='automated')
    item = None
    if offer:
        item = shelf(database[1]); media_policy(authority)
        with Session(database[1]) as s, s.begin():
            for v in s.scalars(select(VaultItem)): v.content_hash_kind = 'vault-metadata-v1'
        from approval_helpers import finished_backfill
        finished_backfill(database[1], ACCOUNT, FAN)     # real offers wait for a finished backfill of the fan
    if kind == 'fansapi':
        _real_policy(database, authority)
    adapter = FansWebhookAdapter(store)
    adapter.bind_simulated(TENANT, ACCOUNT, ACCT, CREATOR)
    engine = EngineService(store, SyntheticModel({}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    return store, authority, engine, item


def _real_transport(store, fake, **kw):
    cfg = RealFansConfig('fixture-token', ACCT, 'https://api.fansapi.com', 5)
    client = build_client(cfg, creator_id=CREATOR, epoch=1, transport=fake.transport())
    return FansApiRealTransport(store, TENANT, ACCOUNT, client, allowed_chats=frozenset({str(FAN)}),
                                sleep=lambda s: None, enabled=True, **kw)


def _intent(engine, store, body='Hi there'):
    engine.model = SyntheticModel({'hello': SyntheticProposal(body)})
    store.record(TENANT, ACCOUNT, event(mid=1, fan=FAN, body='hello'))
    store.drain(TENANT, ACCOUNT)
    from approval_helpers import queue_decision
    return queue_decision(engine, TENANT, ACCOUNT, engine.plan(TENANT, ACCOUNT, FAN, 1))


# ---------------------------------------------------------------- first-class registration

def test_real_transport_dispatches_with_no_monkeypatch_when_flag_and_kind_match(database, monkeypatch):
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL', '1')
    fake = FakeFans()
    try:
        store, authority, engine, _ = _build(database, 'fansapi')
        intent = _intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, _real_transport(store, fake)) == 'accepted'
        assert len(fake.posts) == 1
    finally:
        fake.close()


def test_flag_off_by_default_refuses_dispatch_before_any_attempt(database, monkeypatch):
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL', '1')
    fake = FakeFans()
    try:
        store, authority, engine, _ = _build(database, 'fansapi')
        intent = _intent(engine, store)          # an approved intent exists; then the switch goes off
        monkeypatch.delenv('TWIN_ENGINE_FANSAPI_REAL', raising=False)
        assert real_transport_enabled() is False
        with pytest.raises(Refused, match='not enabled'):
            authority.dispatch(TENANT, ACCOUNT, intent, _real_transport(store, fake))
        assert fake.posts == []
        with Session(database[1]) as s:
            row = s.get(OfTextIntent, (ACCOUNT, intent))
            assert row.status == 'queued' and not row.attempt_id
        with pytest.raises(Refused):
            build_real_transport(store, TENANT, ACCOUNT, creator_id=CREATOR, epoch=1, env={})
    finally:
        fake.close()


def test_real_transport_refused_under_synthetic_policy_and_synthetic_transport_under_fansapi(database, monkeypatch):
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL', '1')
    fake = FakeFans()
    try:
        store, authority, engine, _ = _build(database, 'synthetic')
        intent = _intent(engine, store)
        with pytest.raises(Refused, match='does not match'):
            authority.dispatch(TENANT, ACCOUNT, intent, _real_transport(store, fake))
        assert fake.posts == []
    finally:
        fake.close()


def test_synthetic_transport_refused_under_fansapi_policy(database, monkeypatch):
    monkeypatch.setenv('TWIN_ENGINE_FANSAPI_REAL', '1')
    store, authority, engine, _ = _build(database, 'fansapi')
    intent = _intent(engine, store)
    with pytest.raises(Refused, match='does not match'):
        authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT))
    store.close()


def test_native_and_fansapi_never_switch_directly(database):
    store, authority = configured(database, mode='automated')
    with Session(database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT); rev = policy.revision
    authority.configure(TENANT, ACCOUNT, expected_revision=rev, enabled=True, limit_units=20, throttle_seconds=1,
                        actor='join-test', reason='native', transport_kind='native')
    with pytest.raises(Refused, match='native and fansapi'):
        authority.configure(TENANT, ACCOUNT, expected_revision=rev + 1, enabled=True, limit_units=20, throttle_seconds=1,
                            actor='join-test', reason='real', transport_kind='fansapi')
    assert _kind(database) == 'native'
    store.close()


# ---------------------------------------------------------------- offers and coverage are bound to the policy kind

def test_real_catalogue_offer_requires_fansapi_policy_and_synthetic_offer_requires_synthetic(database):
    store, authority, engine, item = _build(database, 'synthetic', offer=True)
    with pytest.raises(Refused):
        approve_catalogue_offer(store, TENANT, ACCOUNT, FAN, item,
                                RealCatalogueCoverage(ACCOUNT, FAN, item, 'vault:synced', utcnow() + timedelta(minutes=5)))
    _real_policy(database, authority)
    grant_id = approve_catalogue_offer(store, TENANT, ACCOUNT, FAN, item,
                                       RealCatalogueCoverage(ACCOUNT, FAN, item, 'vault:synced', utcnow() + timedelta(minutes=5)))
    with pytest.raises(Refused):
        approve_simulated_offer(store, TENANT, ACCOUNT, FAN, item,
                                SyntheticCoverage(ACCOUNT, FAN, item, (), 'synthetic:x', utcnow() + timedelta(minutes=5)))
    with Session(database[1], expire_on_commit=False) as s:
        grant = s.get(OfEngineOfferGrant, (ACCOUNT, grant_id))
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        from onlyfans_runtime.store import Refused as R
        runtime = store._runtime(s, TENANT, ACCOUNT)
        assert verified_terms(s, runtime, owner, grant)['item']['id'] == item
        from types import SimpleNamespace
        def clone(ref):
            return SimpleNamespace(**{k: getattr(grant, k) for k in ('grant_id', 'binding_id', 'fan_id', 'expires_at', 'used_intent_id',
                                                                      'item_id', 'terms_hash')}, coverage_ref=ref)
        with pytest.raises(R):
            verified_terms(s, runtime, owner, clone('synthetic:forged'))   # lab evidence cannot authorize a real send
        s.get(OfDeliveryPolicy, ACCOUNT).transport_kind = 'synthetic'
        with pytest.raises(R):
            verified_terms(s, runtime, owner, clone('vault:synced'))       # real evidence cannot authorize a lab send
        s.rollback()
    store.close()


def test_catalogue_search_and_coverage_are_bound_to_the_kind(database):
    store, authority, engine, item = _build(database, 'fansapi', offer=True)
    until = utcnow() + timedelta(minutes=5)
    with pytest.raises(Refused):
        synthetic_coverage(store, TENANT, ACCOUNT, complete=True, evidence_ref='synthetic:x', valid_until=until)
    with pytest.raises(Refused):
        vault_coverage(store, TENANT, ACCOUNT, complete=True, evidence_ref='synthetic:x', valid_until=until)
    cov = vault_coverage(store, TENANT, ACCOUNT, complete=True, evidence_ref='vault:synced', valid_until=until)
    assert type(cov) is VaultCatalogueCoverage
    with Session(database[1]) as s:
        runtime = store._runtime(s, TENANT, ACCOUNT)
        assert search_scoped(s, runtime, FAN, 'show me something', cov)['coverage'] in {'complete', 'incomplete'}
        wrong = SyntheticCatalogueCoverage(ACCOUNT, runtime.binding_id, True, 'synthetic:x', until, cov.snapshot_hash)
        with pytest.raises(Refused):
            search_scoped(s, runtime, FAN, 'show me something', wrong)
        s.get(OfDeliveryPolicy, ACCOUNT).transport_kind = 'native'
        s.flush()
        with pytest.raises(Refused):
            search_scoped(s, runtime, FAN, 'show me something', cov)   # native is never engine-owned
        s.rollback()
    store.close()


def test_access_evidence_entry_points_are_bound_to_the_kind(database):
    store, authority, engine, item = _build(database, 'fansapi')
    stamp = utcnow().replace(tzinfo=__import__('datetime').timezone.utc)
    synth = SyntheticAccessObservation(ACCOUNT, FAN, 'i', 1, (), 'k1', 'synthetic:x', 'confirmed', stamp)
    real = ProviderAccessObservation(ACCOUNT, FAN, 'i', 1, (), 'k2', 'fansapi:x', 'confirmed', stamp)
    with pytest.raises(Refused, match='does not match'):
        record_simulated_access(store, TENANT, ACCOUNT, synth)
    with pytest.raises(Refused):
        record_provider_access(store, TENANT, ACCOUNT, synth)           # wrong type
    with pytest.raises(Refused):
        record_provider_access(store, TENANT, ACCOUNT, ProviderAccessObservation(
            ACCOUNT, FAN, 'i', 1, (), 'k3', 'synthetic:x', 'confirmed', stamp))   # wrong evidence prefix
    with pytest.raises(Refused, match='does not match the accepted purchase'):
        record_provider_access(store, TENANT, ACCOUNT, real)           # passes the kind gate, no such offer
    store.close()
