"""Real FansApiRealTransport against a loopback fake Fans server. No provider call, no model call."""
from datetime import timedelta
import json
import os
import stat

import pytest
from sqlalchemy import text, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from test_onlyfans_media_pg import shelf, media_policy
from fake_fans_server import FakeFans, ACCT, CREATOR
from conversation_engine.commerce import RealCatalogueCoverage, approve_catalogue_offer
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from db.models import Message, VaultItem
from db.onlyfans_delivery_models import OfTextIntent
from fans_api_adapter import protocol_lab
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.real_transport import (FansApiRealTransport, RealFansConfig, build_client, idempotency_key,
                                             load_real_config)
from fans_api_adapter.vault_sync import sync_vault, sync_vault_lists
from onlyfans_runtime.store import Refused, utcnow


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    # TextAuthority.dispatch is outside this change: register by exact type for the test only.
    monkeypatch.setattr(protocol_lab, 'FansApiProtocolLabTransport', FansApiRealTransport)


def build(database, fake, *, offer=False, model=None, **kw):
    store, authority = configured(database, mode='automated')
    item_id = None
    if offer:
        item_id = shelf(database[1]); media_policy(authority)
        with Session(database[1]) as s, s.begin():
            for v in s.scalars(select(VaultItem)): v.content_hash_kind = 'vault-metadata-v1'
    adapter = FansWebhookAdapter(store)
    adapter.bind_simulated(TENANT, ACCOUNT, ACCT, CREATOR)
    engine = EngineService(store, SyntheticModel({}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    cfg = RealFansConfig('fixture-token', ACCT, 'https://api.fansapi.com', 5)
    client = build_client(cfg, creator_id=CREATOR, epoch=1, transport=fake.transport())
    transport = FansApiRealTransport(store, TENANT, ACCOUNT, client, allowed_chats=frozenset({str(FAN)}),
                                     sleep=lambda s: None, enabled=True, **kw)
    return store, authority, engine, transport, item_id


def text_intent(engine, store, body='Hi there'):
    engine.model = SyntheticModel({'hello': SyntheticProposal(body)})
    store.record(TENANT, ACCOUNT, event(mid=1, fan=FAN, body='hello'))
    store.drain(TENANT, ACCOUNT)
    return engine.queue(TENANT, ACCOUNT, engine.plan(TENANT, ACCOUNT, FAN, 1))


def status(database, intent):
    with Session(database[1]) as s:
        return s.get(OfTextIntent, (ACCOUNT, intent))


def test_text_accepted_uses_attempt_key_and_strict_ban(database):
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
        assert len(fake.posts) == 1
        post = fake.posts[0]
        row = status(database, intent)
        assert post['key'] == idempotency_key(row.attempt_id)
        assert post['body'] == {'text': 'Hi there', 'blockBannedWords': 'strict_ban'}
        assert post['auth'] == 'Bearer fixture-token' and post['path'] == f'/api/{ACCT}/chats/{FAN}/messages'
        with Session(database[1]) as s:
            assert s.get(Message, (ACCOUNT, FAN, 7001)).body == 'Hi there'
    finally:
        fake.close()


def test_not_sent_retries_same_key_once_executed(database):
    fake = FakeFans(['not_sent', 'not_sent'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
        assert len(fake.posts) == 3 and len({p['key'] for p in fake.posts}) == 1
        assert sum(fake.executions.values()) == 1
    finally:
        fake.close()


def test_not_sent_exhausted_is_not_accepted(database):
    fake = FakeFans(['not_sent'] * 3)
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'not_accepted'
        assert fake.executions == {}
    finally:
        fake.close()


def test_503_unknown_blocks_fan_without_retry(database):
    fake = FakeFans(['unknown'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        assert len(fake.posts) == 1
        assert status(database, intent).status == 'unknown'
    finally:
        fake.close()


def test_conflict_in_progress_retries_same_key_then_accepts(database):
    fake = FakeFans(['conflict', 'accepted'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
        assert len({p['key'] for p in fake.posts}) == 1 and sum(fake.executions.values()) == 1
    finally:
        fake.close()


def test_conflict_never_resolving_stays_unknown(database):
    fake = FakeFans(['conflict'] * 3)
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        assert len({p['key'] for p in fake.posts}) == 1 and len(fake.posts) == 3
    finally:
        fake.close()


def test_422_banned_words_is_rejected_not_retried(database):
    fake = FakeFans(['banned'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'not_accepted'
        assert len(fake.posts) == 1
    finally:
        fake.close()


def test_key_mismatch_is_unknown_never_new_key(database):
    fake = FakeFans(['mismatch'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        assert len(fake.posts) == 1
    finally:
        fake.close()


def test_lost_response_is_unknown_then_replay_gives_exactly_one_send(database):
    fake = FakeFans(['lost'])
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'unknown'
        assert transport.reconcile_accepted(intent) == 'accepted'
        assert len({p['key'] for p in fake.posts}) == 1 and sum(fake.executions.values()) == 1
        with Session(database[1]) as s:
            assert s.get(Message, (ACCOUNT, FAN, 7001)).body == 'Hi there'
    finally:
        fake.close()


def test_crash_after_acceptance_before_projection_sends_exactly_once(database, monkeypatch):
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        intent = text_intent(engine, store)
        real = type(authority).record_accepted
        def crash(self, *a, **k): raise KeyboardInterrupt('process died after provider acceptance')
        monkeypatch.setattr(type(authority), 'record_accepted', crash)
        with pytest.raises(KeyboardInterrupt):
            authority.dispatch(TENANT, ACCOUNT, intent, transport)
        monkeypatch.setattr(type(authority), 'record_accepted', real)
        assert sum(fake.executions.values()) == 1
        with database[1].begin() as c:
            c.execute(text("UPDATE of_text_intent SET deadline = CURRENT_TIMESTAMP - INTERVAL '1 second'"))
        assert authority.expire_dispatches(TENANT, ACCOUNT) == 1
        assert transport.reconcile_accepted(intent) == 'accepted'
        assert sum(fake.executions.values()) == 1 and len({p['key'] for p in fake.posts}) == 1
        with Session(database[1]) as s:
            assert s.get(Message, (ACCOUNT, FAN, 7001)).body == 'Hi there'
    finally:
        fake.close()


def test_other_chat_is_refused_before_the_wire(database):
    fake = FakeFans()
    try:
        store, authority, engine, transport, _ = build(database, fake)
        object.__setattr__(transport, 'allowed_chats', frozenset({'999'}))
        intent = text_intent(engine, store)
        authority.dispatch(TENANT, ACCOUNT, intent, transport)
        assert fake.posts == []
    finally:
        fake.close()


def test_paid_offer_carries_vault_ids_previews_and_dollar_price(database):
    fake = FakeFans()
    try:
        store, authority, engine, transport, item_id = build(database, fake, offer=True)
        cov = RealCatalogueCoverage(ACCOUNT, FAN, item_id, 'vault:synced', utcnow() + timedelta(minutes=5))
        grant = approve_catalogue_offer(store, TENANT, ACCOUNT, FAN, item_id, cov)
        engine.model = SyntheticModel({'show me the set': SyntheticProposal('x', action='offer', offer_grant_id=grant)})
        store.record(TENANT, ACCOUNT, event(mid=1, fan=FAN, body='show me the set')); store.drain(TENANT, ACCOUNT)
        intent = engine.queue(TENANT, ACCOUNT, engine.plan(TENANT, ACCOUNT, FAN, 1))
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
        body = fake.posts[0]['body']
        assert body['mediaFiles'] == ['11', '12', '13'] and body['previews'] == ['11']
        assert body['price'] == 10 and body['blockBannedWords'] == 'strict_ban'
    finally:
        fake.close()


def test_offer_for_unsynced_media_gets_no_grant(database):
    fake = FakeFans()
    try:
        store, authority, engine, transport, item_id = build(database, fake, offer=True)
        with Session(database[1]) as s, s.begin():
            for v in s.scalars(select(VaultItem)): v.content_hash_kind = None
        with pytest.raises(Refused):
            approve_catalogue_offer(store, TENANT, ACCOUNT, FAN, item_id,
                RealCatalogueCoverage(ACCOUNT, FAN, item_id, 'vault:synced', utcnow() + timedelta(minutes=5)))
    finally:
        fake.close()


def _item(i, **kw):
    base = {'id': i, 'type': 'photo', 'isReady': True, 'canView': True, 'hasError': False,
            'createdAt': '2026-11-01T00:00:00+00:00', 'files': {'thumb': {'url': 'https://cdn.example/t.jpg'},
            'full': {'url': 'https://cdn.example/f.jpg'}}, 'listStates': [{'type': 'custom', 'id': 5, 'hasMedia': True}]}
    base.update(kw); return base


def test_vault_sync_skips_unready_drm_error_and_pages(database):
    items = [_item(100 + i) for i in range(10)] + [
        _item(200, isReady=False), _item(201, hasError=True), _item(202, canView=False),
        _item(203, type='video', files={'thumb': {'url': 'https://cdn.example/t.jpg'}, 'full': {'url': None}}),
        _item(204, type='video'), _item(205, type='audio')]
    fake = FakeFans(vault=items)
    try:
        store, authority, engine, transport, _ = build(database, fake)
        result = sync_vault(store, TENANT, ACCOUNT, transport.client, page_size=10)
        assert result['complete'] and result['pages'] == 2
        assert result['skipped'] == {'not_ready': 1, 'error': 1, 'cannot_view': 1, 'drm': 1, 'unsupported_kind': 1}
        with Session(database[1]) as s:
            ids = {v.media_id for v in s.scalars(select(VaultItem)) if v.removed_at is None}
        assert ids == set(range(100, 110)) | {204}
        assert sync_vault_lists(transport.client)[0]['name'] == 'L'
        fake.vault = fake.vault[:5]
        assert sync_vault(store, TENANT, ACCOUNT, transport.client, page_size=10)['complete']
        with Session(database[1]) as s:
            assert {v.media_id for v in s.scalars(select(VaultItem)) if v.removed_at is None} == set(range(100, 105))
    finally:
        fake.close()


def test_config_flag_and_credentials_file_rules(tmp_path):
    with pytest.raises(Refused):
        load_real_config({})
    f = tmp_path / 'fansapi.env'
    f.write_text('FANSAPI_KEY=k\nFANSAPI_ACCT=acct_x\n'); os.chmod(f, 0o644)
    env = {'TWIN_ENGINE_FANSAPI_REAL': '1', 'TWIN_ENGINE_FANSAPI_ENV_FILE': str(f)}
    with pytest.raises(Refused):
        load_real_config(env)
    os.chmod(f, 0o600)
    cfg = load_real_config(env)
    assert cfg.provider_account == 'acct_x' and 'k' not in repr(cfg).replace('acct', '').replace('base', '')
