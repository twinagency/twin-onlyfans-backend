"""Scoped catalogue discovery and ambiguity-aware offer references."""
from datetime import timedelta
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, unthrottle
from test_onlyfans_media_pg import shelf, media_policy
from test_engine_simulated_journey_pg import incoming
from conversation_engine.catalogue import approve_alias, search, synthetic_coverage
from conversation_engine.commerce import SyntheticCoverage, approve_simulated_offer
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from db.models import CatalogItem, VaultItem
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.store import Refused, utcnow

ACTOR = 'op-session:' + 'a'*64


def setup(database):
    store, authority = configured(database, mode='automated', limit=20)
    red = shelf(database[1]); media_policy(authority)
    with Session(database[1]) as s, s.begin():
        red_item = s.get(CatalogItem, red)
        red_item.label, red_item.description_for_ai, red_item.tags = 'Red set', 'A red studio collection.', '["studio", "red"]'
        for mid in (11,12,13): s.get(VaultItem, (ACCOUNT, mid)).content_hash_kind = 'synthetic'
        s.add_all([VaultItem(account_id=ACCOUNT, media_id=21, kind='image', created_at=utcnow(),
                            content_hash='blue-21', content_hash_kind='synthetic'),
                   VaultItem(account_id=ACCOUNT, media_id=22, kind='video', created_at=utcnow(),
                            content_hash='blue-22', content_hash_kind='synthetic')])
        blue_item = CatalogItem(account_id=ACCOUNT, kind='video', label='Blue set',
            description_for_ai='A blue beach collection.', tags='["beach", "blue"]',
            media_ids='[21,22]', preview_media_ids='[21]', price_cents=1200, enabled=True)
        s.add(blue_item); s.flush(); blue = blue_item.id
    FansWebhookAdapter(store).bind_simulated(TENANT, ACCOUNT, 'acct_catalog_fixture', 'creator-fixture')
    model = SyntheticModel({})
    engine = EngineService(store, model)
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    return store, authority, engine, model, red, blue


def coverage(store, complete=True):
    return synthetic_coverage(store, TENANT, ACCOUNT, complete=complete,
        evidence_ref='synthetic:catalogue-observed', valid_until=utcnow()+timedelta(minutes=5))


def test_catalogue_search_uses_approved_aliases_and_distinguishes_uncertainty(database):
    store, authority, engine, model, red, blue = setup(database)
    first = approve_alias(store, TENANT, ACCOUNT, red, 'Sunrise album', actor=ACTOR)
    assert approve_alias(store, TENANT, ACCOUNT, red, 'sunrise album', actor=ACTOR) == first
    proof = coverage(store)
    result = search(store, TENANT, ACCOUNT, FAN, 'show me the sunrise album', proof)
    assert result['state'] == 'matches' and result['items'][0]['itemId'] == str(red)
    assert result['matchQuality'] == 'all_terms' and result['coverage'] == 'complete'
    broad = search(store, TENANT, ACCOUNT, FAN, 'sunrise unknownword', proof)
    assert broad['state'] == 'matches' and broad['matchQuality'] == 'broadened'
    assert search(store, TENANT, ACCOUNT, FAN, 'show me', proof)['state'] == 'unclear'
    assert search(store, TENANT, ACCOUNT, FAN, 'unicorn', proof)['state'] == 'confirmed_none'
    assert search(store, TENANT, ACCOUNT, FAN, 'unicorn')['state'] == 'incomplete'
    assert search(store, TENANT, ACCOUNT, FAN, 'unicorn', coverage(store, complete=False))['state'] == 'incomplete'
    with pytest.raises(Refused):
        search(store, 'other-tenant', ACCOUNT, FAN, 'sunrise', proof)
    with Session(database[1]) as s, s.begin():
        s.get(VaultItem, (ACCOUNT, 12)).content_hash = 'changed-version'
    assert search(store, TENANT, ACCOUNT, FAN, 'sunrise', proof)['state'] == 'stale'
    fresh = coverage(store)
    assert search(store, TENANT, ACCOUNT, FAN, 'sunrise album', fresh)['state'] == 'unavailable'
    approve_alias(store, TENANT, ACCOUNT, red, 'Sunrise album', actor=ACTOR)
    assert search(store, TENANT, ACCOUNT, FAN, 'sunrise album', coverage(store))['state'] == 'matches'
    store.close()


def send_offer(store, authority, engine, model, item, media, text, mid, provider_message):
    grant = approve_simulated_offer(store, TENANT, ACCOUNT, FAN, item,
        SyntheticCoverage(ACCOUNT, FAN, item, tuple(media), 'synthetic:complete-ownership', utcnow()+timedelta(minutes=5)))
    model.replies[text] = SyntheticProposal('untrusted', action='offer', offer_grant_id=grant)
    incoming(store, mid, text)
    decision = engine.plan(TENANT, ACCOUNT, FAN, mid)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    unthrottle(store.engine)
    provider = SyntheticFansProvider(message_id=provider_message)
    assert authority.dispatch(TENANT, ACCOUNT, intent, FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)) == 'accepted'
    return intent


def test_offer_reference_names_older_offer_and_asks_when_ambiguous(database):
    store, authority, engine, model, red, blue = setup(database)
    approve_alias(store, TENANT, ACCOUNT, red, 'Sunrise album', actor=ACTOR)
    first = send_offer(store, authority, engine, model, red, (11,12,13), 'show red', 1, 9701)
    second = send_offer(store, authority, engine, model, blue, (21,22), 'show blue', 2, 9702)
    model.replies['send it'] = SyntheticProposal('Which set did you mean?')
    incoming(store, 3, 'send it')
    engine.plan(TENANT, ACCOUNT, FAN, 3)
    assert model.calls[-1]['offer_reference']['state'] == 'ambiguous'
    # A changed current price does not rewrite the old accepted offer.
    with Session(database[1]) as s, s.begin():
        s.get(CatalogItem, red).price_cents = 1500
    model.replies['I mean the sunrise album'] = SyntheticProposal('The Red set was offered for USD 10.00.')
    incoming(store, 4, 'I mean the sunrise album')
    engine.plan(TENANT, ACCOUNT, FAN, 4)
    reference = model.calls[-1]['offer_reference']
    assert reference['state'] == 'matched' and reference['match'] == 'named'
    assert reference['offers'][0]['itemId'] == str(red) and reference['offers'][0]['intentId'] == first
    assert reference['offers'][0]['priceCents'] == 1000 and reference['offers'][0]['intentId'] != second
    store.close()


def test_engine_sees_scoped_catalogue_and_rechecks_it_before_sending(database):
    store, authority, engine, model, red, blue = setup(database)
    approve_alias(store, TENANT, ACCOUNT, red, 'Sunrise album', actor=ACTOR)
    model.replies['show me the sunrise album'] = SyntheticProposal('Here is what I found.')
    incoming(store, 1, 'show me the sunrise album')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    visible = model.calls[-1]['catalogue_search']
    assert visible['state'] == 'matches' and visible['coverage'] == 'incomplete'
    assert visible['items'][0]['itemId'] == str(red)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    with Session(database[1]) as s, s.begin():
        s.get(CatalogItem, red).price_cents += 100
    provider = SyntheticFansProvider()
    assert authority.dispatch(TENANT, ACCOUNT, intent, FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)) in {'refused', 'cancelled'}
    assert provider.calls == []
    store.close()


def test_followup_catalogue_search_uses_scoped_recent_incoming_context(database):
    store, authority, engine, model, red, blue = setup(database)
    model.replies['show me the red set'] = SyntheticProposal('I found the red set.')
    model.replies['what about those?'] = SyntheticProposal('The red set is available for review.')
    incoming(store, 1, 'show me the red set')
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    incoming(store, 2, 'what about those?')
    engine.plan(TENANT, ACCOUNT, FAN, 2)
    result = model.calls[-1]['catalogue_search']
    assert result['state'] == 'matches' and result['items'][0]['itemId'] == str(red)
    assert result['contextUsed'] is True and result['querySourceMessageId'] == '1'
    assert result['scope'] == 'approved_available_catalogue' and result['coverage'] == 'incomplete'
    store.close()
