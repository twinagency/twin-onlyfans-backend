"""LIVE proof, Maile -> test fan <test-fan-chat> only. Skipped unless LIVE_FANSAPI_B3=1.
Limits enforced in code: chat id asserted before every POST, <=15 sends, <=2 paid cards at $3, <=150 credits."""
import os
import json, os, time
from datetime import timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

CHAT = os.environ.get('LIVE_TEST_FAN_CHAT', 'test-fan-chat')
MAX_SENDS, MAX_PAID, MAX_CREDITS = 15, 2, 150
pytestmark = pytest.mark.skipif(os.environ.get('LIVE_FANSAPI_B3') != '1', reason='live proof is opt-in')

from test_onlyfans_foundation_pg import database, seed, event
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT, current
from conversation_engine.commerce import RealCatalogueCoverage, approve_catalogue_offer
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from db.models import Message, VaultItem
from db.onlyfans_delivery_models import OfTextIntent
import fans_api_adapter.https_transport as ht
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.real_transport import FansApiRealTransport, build_client, load_real_config, idempotency_key
from fans_api_adapter.vault_sync import sync_vault, sync_vault_lists, create_catalogue_item
from onlyfans_runtime.text_delivery import TextAuthority
from onlyfans_runtime.store import utcnow


def test_live_proof(database, monkeypatch, capsys):
    url, engine = database
    counters = {'sends': 0, 'paid': 0, 'first': None, 'last': None}
    log = []
    orig = ht.FansHttpsTransport.__call__
    def guarded(self, *, method, url, headers, body, timeout, follow_redirects):
        path = url.split('/api/', 1)[1]
        if method == 'POST':
            assert path.endswith(f'/chats/{CHAT}/messages') and path.count('/') == 3, 'POST outside test chat'
            payload = json.loads(body)
            counters['sends'] += 1
            assert counters['sends'] <= MAX_SENDS
            if payload.get('price'):
                counters['paid'] += 1
                assert counters['paid'] <= MAX_PAID and payload['price'] == 3
        else:
            assert path == 'accounts' or '/media/vault' in path, 'only identity and vault reads allowed'
        if counters['first'] is not None:
            assert counters['first'] - counters['last'] < MAX_CREDITS - 10, 'credit budget'
        r = orig(self, method=method, url=url, headers=headers, body=body, timeout=timeout, follow_redirects=follow_redirects)
        low = {k.lower(): v for k, v in r.headers.items()}
        bal = low.get('x-ofapi-credits-balance')
        if bal and bal.isdigit():
            counters['first'] = counters['first'] if counters['first'] is not None else int(bal) + 1
            counters['last'] = int(bal)
        log.append({'method': method, 'what': path.split('/', 1)[-1].split('?')[0] if method == 'GET' else 'POST chat message',
                    'status': r.status, 'replayed': low.get('idempotent-replayed'), 'credits_balance': bal})
        return r
    monkeypatch.setattr(ht.FansHttpsTransport, '__call__', guarded)

    config = load_real_config()
    fan = int(CHAT)
    store = seed(url, engine, fan=fan)
    current(engine, mode='automated', fan=fan)
    authority = TextAuthority(store)
    authority.configure(TENANT, ACCOUNT, expected_revision=0, enabled=True, limit_units=20, throttle_seconds=1,
                        actor='live-operator', reason='B3 live proof')
    authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True, media_enabled=True, limit_units=20,
                        throttle_seconds=1, actor='live-operator', reason='B3 live proof media')
    authority.configure(TENANT, ACCOUNT, expected_revision=2, enabled=True, limit_units=20,
                        throttle_seconds=1, actor='live-operator', reason='B3 live proof real kind',
                        transport_kind='fansapi')
    creator = os.environ.get('LIVE_TEST_CREATOR_OF_ID', '900000000001')
    adapter = FansWebhookAdapter(store)
    adapter.bind_simulated(TENANT, ACCOUNT, config.provider_account, creator)
    client = build_client(config, creator_id=creator, epoch=1)
    transport = FansApiRealTransport(store, TENANT, ACCOUNT, client, allowed_chats=frozenset({CHAT}), enabled=True)
    eng = EngineService(store, SyntheticModel({}))
    eng.select_simulated(TENANT, ACCOUNT, fan)
    out = {}

    # 1. vault sync (reads only)
    result = sync_vault(store, TENANT, ACCOUNT, client)
    lists = sync_vault_lists(client)
    out['vault_sync'] = {k: result[k] for k in ('complete', 'pages', 'upserted', 'skipped')}
    assert result['complete'], result
    lingerie = [l for l in lists if 'ingerie/bikini' in (l['name'] or '')]
    assert len(lingerie) == 1
    lid = lingerie[0]['id']
    with Session(engine) as s:
        rows = [v for v in s.scalars(select(VaultItem).where(VaultItem.removed_at.is_(None)).order_by(VaultItem.media_id.desc()))
                if v.kind == 'photo' and json.loads(v.of_folder_ids) == [lid]]
    photo = rows[0].media_id
    out['vault_items_sellable'] = None
    out['chosen_photo_vault_id'] = photo

    # 2. one text through the engine
    eng.model = SyntheticModel({'live test one': SyntheticProposal('hey, this is a test message from the engine')})
    store.record(TENANT, ACCOUNT, event(mid=1, fan=fan, body='live test one')); store.drain(TENANT, ACCOUNT)
    text_intent = eng.queue(TENANT, ACCOUNT, eng.plan(TENANT, ACCOUNT, fan, 1))
    out['text_result'] = authority.dispatch(TENANT, ACCOUNT, text_intent, transport)
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, text_intent))
        out['text_provider_message_id'] = row.provider_message_id
        text_attempt, text_payload = row.attempt_id, (row.body, tuple(json.loads(row.media_json)))
    assert out['text_result'] == 'accepted'

    # 3. one $3 paid card with a real vault photo
    time.sleep(3)
    item = create_catalogue_item(store, TENANT, ACCOUNT, [photo], price_cents=300, label='Lingerie photo',
                                 description_for_ai='A lingerie or bikini photo.')
    eng.select_simulated(TENANT, ACCOUNT, fan)
    cov = RealCatalogueCoverage(ACCOUNT, fan, item, 'vault:live-sync', utcnow() + timedelta(minutes=10))
    grant = approve_catalogue_offer(store, TENANT, ACCOUNT, fan, item, cov)
    eng.model = SyntheticModel({'live test two': SyntheticProposal('here is a little something for you', action='offer', offer_grant_id=grant)})
    store.record(TENANT, ACCOUNT, event(mid=2, fan=fan, body='live test two')); store.drain(TENANT, ACCOUNT)
    paid_intent = eng.queue(TENANT, ACCOUNT, eng.plan(TENANT, ACCOUNT, fan, 2))
    out['paid_result'] = authority.dispatch(TENANT, ACCOUNT, paid_intent, transport)
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, paid_intent))
        out['paid_provider_message_id'] = row.provider_message_id
        out['paid_price_cents'], out['paid_media'] = row.price_cents, row.media_json
    assert out['paid_result'] == 'accepted'

    # 4. deliberate idempotent retry of the text: same key, same payload
    replay = client.send(CHAT, key=idempotency_key(ACCOUNT, text_intent), text=text_payload[0], block_banned_words='strict_ban')
    out['replay'] = {'state': replay.state, 'message_id': replay.data and replay.data['message_id'],
                     'replayed': replay.replayed, 'http': replay.http_status}
    assert replay.state == 'accepted' and replay.replayed and str(replay.data['message_id']) == str(out['text_provider_message_id'])

    out['sends_used'], out['paid_used'] = counters['sends'], counters['paid']
    out['credits_first_balance_plus1'], out['credits_last_balance'] = counters['first'], counters['last']
    out['credits_used_this_run'] = counters['first'] - counters['last']
    out['calls'] = log
    with capsys.disabled():
        print('LIVE_RESULT ' + json.dumps(out, default=str))
