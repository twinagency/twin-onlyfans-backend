"""Step 3 counterexamples a-f: scoped catalogue, one approved-terms contract, ownership, no-reprice.

Each test was written and run against be7108d first (log: claude-step3-lab-20261111/logs/base_*.log); the production
change that makes it pass is named in its commit. Everything is offline: no provider, no model, no deployment.
"""
import inspect
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from commercial_terms_fixture import (ACTOR, add_item, add_vault, approve, bulk_commercial, runtime_of)
from conversation_engine import catalogue
from conversation_engine.catalogue import search_scoped, synthetic_coverage
from db.models import CatalogItem, Message, VaultFolder, VaultFolderItem, VaultSend
from db.onlyfans_engine_models import OfEngineOfferGrant
from onlyfans_runtime.store import Refused, utcnow
from test_engine_catalogue_pg import coverage as syn_coverage, send_offer, setup as engine_setup
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_onlyfans_vault_pg import media as native_media, sync
from test_onlyfans_connections_pg import ACTOR as AGENCY


def seen(engine):
    statements = []
    def hook(conn, cursor, statement, *rest):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', hook)
    return statements, lambda: event.remove(engine, 'before_cursor_execute', hook)


def runtime(store, s):
    return store._runtime(s, TENANT, ACCOUNT)


# ===================================================== (a) no silent 200-row bound for commercial accounts ============

def test_a_item_2300_of_2500_is_found_with_a_bounded_shortlist_and_truthful_coverage(database):
    store, authority, engine, model, red, blue = engine_setup(database)
    ids = bulk_commercial(database[1], count=2500, binding_id=runtime_of(store), special={2299: 'Zebra stripes'})
    proof = syn_coverage(store)                      # base: raises, coverage is unavailable above 200 rows
    statements, stop = seen(database[1])
    with Session(database[1]) as s, s.begin():
        result = search_scoped(s, runtime(store, s), FAN, 'show me the zebra stripes photo', proof)
    stop()
    assert result['state'] == 'matches' and result['items'][0]['itemId'] == str(ids[2299])
    assert result['coverage'] == 'complete' and result.get('truncated') is False
    assert len(result['items']) <= 5 and result['productCount'] == len(result['items'])
    assert len(statements) <= 90, f'{len(statements)} statements for 2,500 products'
    # legacy shelf items in a commercial account are not approved commercial products
    assert str(red) not in {i['itemId'] for i in result['items']}
    store.close()


def test_a_bundle_of_six_media_is_one_product_with_six_media(database):
    store, authority, engine, model, red, blue = engine_setup(database)
    add_vault(database[1], {200000 + i: 'image' for i in range(1, 7)})
    bundle = add_item(database[1], label='Beach bundle', media=list(range(200001, 200007)), price=2400,
                      description='Six approved beach photographs.')
    approve(store, bundle, standard=2400)
    with Session(database[1]) as s, s.begin():
        result = search_scoped(s, runtime(store, s), FAN, 'beach bundle', syn_coverage(store))
    assert result['state'] == 'matches' and result['productCount'] == 1 and result['mediaCount'] == 6
    item = result['items'][0]
    assert item['productKind'] == 'bundle_photo' and item['counts'] == {'media': 6, 'pictures': 6, 'videos': 0, 'previews': 0}
    assert item['priceCents'] == 2400 and item['standardCents'] == 2400 and item['variant'] == 'standard'
    store.close()


def test_a_page_cap_reports_incomplete_and_never_confirms_none(database):
    store, authority, engine, model, red, blue = engine_setup(database)
    bulk_commercial(database[1], count=300, binding_id=runtime_of(store), hold_at=set(range(300)))
    with Session(database[1]) as s, s.begin():
        result = search_scoped(s, runtime(store, s), FAN, 'studio', syn_coverage(store))
    assert result['state'] != 'confirmed_none' and result['items'] == []
    assert result['coverage'] == 'incomplete' and result['truncated'] is True
    store.close()


def test_a_legacy_account_keeps_its_old_bounds(database):
    store, authority, engine, model, red, blue = engine_setup(database)
    bulk_commercial(database[1], count=250, binding_id=runtime_of(store), special={229: 'Zebra stripes'}, with_terms=False)
    with pytest.raises(Refused):                       # legacy coverage over 200 rows stays unavailable
        syn_coverage(store)
    with Session(database[1]) as s, s.begin():
        result = search_scoped(s, runtime(store, s), FAN, 'zebra stripes')
    assert result['state'] == 'incomplete' and result['items'] == []     # id 230 is beyond the old 200-row view
    store.close()


# ===================================================== (b) approval: manual agency prices only ========================

@pytest.fixture
def cat(database, monkeypatch):
    rows = [native_media(), native_media(12), native_media(13, 'video'), native_media(14, 'video')]
    store, account, _, calls = sync(database, monkeypatch, rows=rows)
    yield store, account
    store.close()


_keys = iter(range(1, 10**6))


def drafts(cat, name, media, price, products, key=None):
    from onlyfans_runtime.vault_editing import get_category_price, get_category_review, set_category_price
    from onlyfans_runtime.vault_management import create_category_drafts
    store, account = cat
    n = next(_keys)
    with Session(store.engine) as s, s.begin():
        folder = VaultFolder(account_id=account, name=name, created_by=AGENCY)
        s.add(folder)
        s.flush()
        fid = folder.id
        s.add_all([VaultFolderItem(account_id=account, folder_id=fid, media_id=m) for m in media])
    current = get_category_price(store, TENANT, account, fid)
    set_category_price(store, TENANT, account, fid, price_cents=price, expected_fingerprint=current['fingerprint'],
                       command_key=f'{n:064x}', actor=AGENCY)
    reviewed = get_category_review(store, TENANT, account, fid, [str(m) for m in sorted({int(x) for p in products for x in p['mediaIds']})])
    category = {'folderId': fid, 'priceCents': price, 'classificationConfirmed': True, 'rightsConfirmed': True,
                'reviewedState': reviewed, 'categoryPriceRevision': 1, 'products': products}
    out = create_category_drafts(store, TENANT, account, category=category, command_key=key or f'{n + 10**7:064x}', actor=AGENCY)
    return fid, [int(i['itemId']) for i in out['items']]


def prod(label, media, total=None, previews=()):
    value = {'label': label, 'description': f'{label}, an approved description.', 'mediaIds': [str(m) for m in media],
             'previewMediaIds': [str(m) for m in previews]}
    if total is not None:
        value['approvedTotalCents'] = total
    return value


def live(store, item):
    with Session(store.engine) as s:
        return s.execute(text("SELECT * FROM of_catalogue_terms WHERE item_id=:i AND status='approved'"), {'i': item}).mappings().one()


def test_b_every_product_kind_is_approved_with_manual_agency_prices(cat):
    store, account = cat
    fid, singles = drafts(cat, 'Scenic photos', (11, 12), 1200, [prod('Photo A', (11,)), prod('Photo B', (12,))])
    _, [bundle_photo] = drafts(cat, 'Scenic bundles', (11, 12), 800, [prod('Photo pair', (11, 12), total=1900)])
    _, [video] = drafts(cat, 'Clips', (13,), 2500, [prod('Clip A', (13,))])
    _, [bundle_video] = drafts(cat, 'Clip bundles', (13, 14), 900, [prod('Clip pair', (13, 14), total=4000)])
    _, [mixed] = drafts(cat, 'Mixed', (12, 13), 900, [prod('Mixed set', (12, 13), total=3100)])
    expected = {singles[0]: ('single_photo', 1200, 'agency_category'), bundle_photo: ('bundle_photo', 1900, 'agency_bundle_total'),
                video: ('single_video', 2500, 'agency_category'), bundle_video: ('bundle_video', 4000, 'agency_bundle_total'),
                mixed: ('bundle_mixed', 3100, 'agency_bundle_total')}
    for item, (kind, price, source) in expected.items():
        row = live(store, item)
        assert (row['product_kind'], row['standard_price_cents'], row['price_source']) == (kind, price, source)
        assert row['revision'] == 1 and row['discount_price_cents'] is None and row['approved_by'] == AGENCY
        assert len(row['snapshot_sha256']) == 64
        with Session(store.engine) as s:
            members = s.execute(text('SELECT count(*) FROM of_catalogue_terms_member WHERE item_id=:i'), {'i': item}).scalar()
            assert members == row['media_count']
            c = s.get(CatalogItem, item)
            assert c.enabled is False and c.pricing_mode == 'fixed'      # approval is not enabling
    # a bundle total below the sum of its singles is allowed; the backend only exposes the warning flag
    from onlyfans_runtime.catalogue_terms import bundle_below_singles
    with Session(store.engine) as s:
        assert bundle_below_singles(s, account, bundle_photo) is True
        assert bundle_below_singles(s, account, singles[0]) is False


def test_b_approve_terms_replays_and_refuses_history_ai_or_unaudited_prices(cat, monkeypatch):
    from onlyfans_runtime import catalogue_terms
    from onlyfans_runtime.catalogue_terms import approve_terms
    store, account = cat
    assert {'standard_cents', 'discount_cents'} <= set(inspect.signature(approve_terms).parameters)
    assert not {p for p in inspect.signature(approve_terms).parameters if 'histor' in p or 'suggest' in p or 'model' in p or 'ai' == p}
    monkeypatch.setattr(catalogue_terms, 'insert_revision', lambda *a, **k: None)          # a draft that predates terms
    _, [item] = drafts(cat, 'Scenic photos', (11,), 1200, [prod('Photo A', (11,))])
    monkeypatch.undo()
    key = 'a1' * 32
    first = approve_terms(store, TENANT, account, item, standard_cents=1200, command_key=key, actor=AGENCY)
    assert first['revision'] == 1 and approve_terms(store, TENANT, account, item, standard_cents=1200, command_key=key, actor=AGENCY) == first
    with pytest.raises(Refused, match='idempotency'):
        approve_terms(store, TENANT, account, item, standard_cents=1200, discount_cents=900, command_key=key, actor=AGENCY)
    with pytest.raises(Refused, match='agency'):
        approve_terms(store, TENANT, account, item, standard_cents=1200, command_key='a2' * 32, actor='worker-import:' + 'a' * 64)
    with pytest.raises(Refused):
        approve_terms(store, 'other-tenant', account, item, standard_cents=1200, command_key='a3' * 32, actor=AGENCY)
    with pytest.raises(Refused, match='category price'):                       # not the agency price
        approve_terms(store, TENANT, account, item, standard_cents=1300, command_key='a4' * 32, actor=AGENCY)
    plain = add_item(store.engine, label='Plain', media=[12], price=1500, mode='dynamic', enabled=False, account=account)
    with pytest.raises(Refused, match='agency category'):                       # no agency audit chain: no approval
        approve_terms(store, TENANT, account, plain, standard_cents=1500, command_key='a5' * 32, actor=AGENCY)
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(CatalogItem)) >= 2


def test_b_bundle_needs_its_own_explicit_total(cat, monkeypatch):
    from onlyfans_runtime import catalogue_terms
    from onlyfans_runtime.catalogue_terms import approve_terms
    store, account = cat
    monkeypatch.setattr(catalogue_terms, 'insert_revision', lambda *a, **k: None)
    _, [bundle] = drafts(cat, 'Scenic bundles', (11, 12), 800, [prod('Photo pair', (11, 12), total=1900)])
    monkeypatch.undo()
    with pytest.raises(Refused, match='approved total'):
        approve_terms(store, TENANT, account, bundle, standard_cents=800, command_key='b1' * 32, actor=AGENCY)   # category single price
    with pytest.raises(Refused, match='approved total'):
        approve_terms(store, TENANT, account, bundle, standard_cents=1600, command_key='b2' * 32, actor=AGENCY)  # sum of singles
    assert approve_terms(store, TENANT, account, bundle, standard_cents=1900, command_key='b3' * 32, actor=AGENCY)['revision'] == 1


@pytest.mark.parametrize('bad', [1200, 1300, 299, 0, -100, True, 9.5, '900', '20%', None.__class__])
def test_b_discount_must_be_a_whole_cent_amount_between_the_floor_and_standard(cat, bad):
    from onlyfans_runtime.catalogue_terms import set_discount
    store, account = cat
    _, [item] = drafts(cat, 'Scenic photos', (11,), 1200, [prod('Photo A', (11,))])
    with pytest.raises((Refused, Exception)) as raised:
        set_discount(store, TENANT, account, item, discount_cents=bad, command_key='c1' * 32, actor=AGENCY)
    assert raised.type.__name__ in {'Refused', 'InvalidVault'}
    assert live(store, item)['revision'] == 1


def test_b_discount_is_a_new_revision_and_never_stacks_or_edits_history(cat):
    from onlyfans_runtime.catalogue_terms import set_discount
    store, account = cat
    _, [item] = drafts(cat, 'Scenic photos', (11,), 1200, [prod('Photo A', (11,))])
    one = set_discount(store, TENANT, account, item, discount_cents=900, command_key='d1' * 32, actor=AGENCY)
    assert one['revision'] == 2 and set_discount(store, TENANT, account, item, discount_cents=900, command_key='d1' * 32, actor=AGENCY) == one
    two = set_discount(store, TENANT, account, item, discount_cents=800, command_key='d2' * 32, actor=AGENCY)
    row = live(store, item)
    assert two['revision'] == 3 and (row['standard_price_cents'], row['discount_price_cents']) == (1200, 800)   # from standard, not 900
    with Session(store.engine) as s:
        history = s.execute(text('SELECT revision,status,standard_price_cents,discount_price_cents FROM of_catalogue_terms '
                                 'WHERE item_id=:i ORDER BY revision'), {'i': item}).all()
        assert history == [(1, 'superseded', 1200, None), (2, 'superseded', 1200, 900), (3, 'approved', 1200, 800)]
    with pytest.raises(Refused, match='agency'):
        set_discount(store, TENANT, account, item, discount_cents=700, command_key='d3' * 32, actor='worker-import:' + 'b' * 64)
    removed = set_discount(store, TENANT, account, item, discount_cents=None, command_key='d4' * 32, actor=AGENCY)
    assert removed['revision'] == 4 and live(store, item)['discount_price_cents'] is None


def test_b_discount_is_chosen_by_application_rules_only_when_the_fan_asks_for_cheaper(database):
    from conversation_engine.price_intent import asks_cheaper, variant_for
    for text_ in ('can you do it cheaper?', 'any discount?', 'do you have a lower price', 'got any deal for me', 'cheaper option?'):
        assert asks_cheaper(text_), text_
    for text_ in ('that is too expensive', 'not now, i cannot afford it', 'how much is it?', 'send it', 'no', 'i love it',
                  'it is cheap enough', '20% off?'):
        assert not asks_cheaper(text_), text_
    store, authority, engine, model, red, blue = engine_setup(database)
    approve(store, blue, standard=1200, discount=900)
    with Session(database[1]) as s, s.begin():
        rt = runtime(store, s)
        assert variant_for(s, rt, FAN, blue, 'show me') == 'standard'
        assert variant_for(s, rt, FAN, blue, 'that is too expensive') == 'standard'
        assert variant_for(s, rt, FAN, blue, 'can you do cheaper?') == 'discount'
        assert variant_for(s, rt, FAN, red, 'can you do cheaper?') == 'standard'       # no approved discount, no discount
    store.close()


def test_b_a_sent_card_keeps_its_price_and_cheaper_later_is_a_reply_not_a_new_card(database):
    from conversation_engine.price_intent import variant_for
    from db.onlyfans_delivery_models import OfTextIntent
    from db.models import ContentOffer
    store, authority, engine, model, red, blue = engine_setup(database)
    approve(store, blue, standard=1200)
    send_offer(store, authority, engine, model, blue, (21, 22), 'show blue', 1, 9801)
    with Session(database[1]) as s:
        before = (s.execute(select(OfTextIntent.price_cents, OfTextIntent.catalog_snapshot_json)).all(),
                  s.execute(select(ContentOffer.price_cents)).all())
    from onlyfans_runtime.catalogue_terms import set_discount
    set_discount(store, TENANT, ACCOUNT, blue, discount_cents=900, command_key='e1' * 32, actor=AGENCY)
    with Session(database[1]) as s, s.begin():
        assert variant_for(s, runtime(store, s), FAN, blue, 'can you do cheaper?') == 'standard'   # a card exists for this fan
        after = (s.execute(select(OfTextIntent.price_cents, OfTextIntent.catalog_snapshot_json)).all(),
                 s.execute(select(ContentOffer.price_cents)).all())
    assert after == before and before[0][0][0] == 1200 and before[1][0][0] == 1200
    store.close()


# ===================================================== (c) one contract: every consumer agrees or all go stale ========

def queued_blue(database, price=1200, discount=None):
    store, authority, engine, model, red, blue = engine_setup(database)
    approve(store, blue, standard=price, discount=discount)
    grant = __import__('conversation_engine.commerce', fromlist=['x']).approve_simulated_offer(store, TENANT, ACCOUNT, FAN, blue,
        __import__('conversation_engine.commerce', fromlist=['x']).SyntheticCoverage(ACCOUNT, FAN, blue, (21, 22),
            'synthetic:complete-ownership', utcnow() + timedelta(minutes=5)))
    from conversation_engine.engine import SyntheticProposal
    from test_engine_simulated_journey_pg import incoming
    model.replies['show blue'] = SyntheticProposal('untrusted', action='offer', offer_grant_id=grant)
    incoming(store, 1, 'show blue')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    return store, authority, engine, blue, grant, intent


def dispatch(store, authority, intent):
    from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
    from test_onlyfans_text_delivery_pg import unthrottle
    unthrottle(store.engine)
    provider = SyntheticFansProvider(message_id=9901)
    return authority.dispatch(TENANT, ACCOUNT, intent, FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)), provider


def test_c_control_a_queued_commercial_card_carries_the_approved_block_and_sends_at_the_standard_price(database):
    from db.onlyfans_delivery_models import OfTextIntent
    store, authority, engine, blue, grant, intent = queued_blue(database)
    outcome, provider = dispatch(store, authority, intent)
    assert outcome == 'accepted'
    with Session(database[1]) as s:
        row = s.get(OfTextIntent, (ACCOUNT, intent))
        snapshot = json.loads(row.catalog_snapshot_json)
        assert row.price_cents == 1200 and snapshot['commercial']['variant'] == 'standard'
        assert snapshot['commercial']['standardCents'] == 1200 and snapshot['commercial']['discountCents'] is None
        assert snapshot['commercial']['v'] == 'commercial-terms.v1' and snapshot['commercial']['revision'] == 1
    store.close()


@pytest.mark.parametrize('mutation', ['discount_revision', 'price_edit', 'member_edit', 'withdrawn'])
def test_c_any_terms_mutation_after_queueing_stops_the_card_at_the_final_wire_check(database, mutation):
    from onlyfans_runtime.catalogue_terms import set_discount, withdraw_terms
    store, authority, engine, blue, grant, intent = queued_blue(database)
    if mutation == 'discount_revision':
        set_discount(store, TENANT, ACCOUNT, blue, discount_cents=900, command_key='f1' * 32, actor=AGENCY)
    elif mutation == 'withdrawn':
        withdraw_terms(store, TENANT, ACCOUNT, blue, command_key='f2' * 32, actor=AGENCY)
    else:
        with Session(database[1]) as s, s.begin():
            item = s.get(CatalogItem, blue)
            if mutation == 'price_edit':
                item.price_cents = 1300
            else:
                item.media_ids = '[21]'
                item.preview_media_ids = '[]'
    outcome, provider = dispatch(store, authority, intent)
    assert outcome in {'refused', 'cancelled'} and provider.calls == []
    store.close()


def test_c_price_rules_are_the_approved_amounts_and_nothing_else(database):
    from onlyfans_runtime.catalogue_terms import commercial_facts, set_discount
    from onlyfans_runtime.media_delivery import catalog_terms
    store, authority, engine, model, red, blue = engine_setup(database)
    approve(store, blue, standard=1200)
    with Session(database[1]) as s, s.begin():
        for price in (1100, 900, 1300, 20001, 299, 0):
            with pytest.raises(Refused):
                catalog_terms(s, ACCOUNT, blue, price_cents=price)
        base = catalog_terms(s, ACCOUNT, blue)
        assert base['price_cents'] == 1200 and base['commercial']['variant'] == 'standard'
    set_discount(store, TENANT, ACCOUNT, blue, discount_cents=900, command_key='f3' * 32, actor=AGENCY)
    with Session(database[1]) as s, s.begin():
        cheap = catalog_terms(s, ACCOUNT, blue, price_cents=900)
        standard = catalog_terms(s, ACCOUNT, blue, price_cents=1200)
        assert cheap['commercial']['variant'] == 'discount' and standard['commercial']['variant'] == 'standard'
        assert cheap['price_cents'] == 900 and cheap['commercial']['revision'] == 2 == standard['commercial']['revision']
        facts = commercial_facts(cheap)
        assert facts['variant'] == 'discount' and facts['priceCents'] == 900 and facts['standardCents'] == 1200
        assert facts['discountApproved'] is True and facts['isBundle'] is False
        assert commercial_facts(standard)['discountApproved'] is False
        for price in (1000, 1100, 899, 1201):
            with pytest.raises(Refused):
                catalog_terms(s, ACCOUNT, blue, price_cents=price)
        # the saved catalogue price must itself equal the approved standard amount
        s.get(CatalogItem, blue).price_cents = 1150
        s.flush()
        with pytest.raises(Refused):
            catalog_terms(s, ACCOUNT, blue)
        s.rollback()
    store.close()


def test_c_legacy_items_without_terms_keep_their_exact_terms_shape_and_price_rules(database):
    from onlyfans_runtime.media_delivery import catalog_terms
    store, authority, engine, model, red, blue = engine_setup(database)
    with Session(database[1]) as s, s.begin():
        legacy = catalog_terms(s, ACCOUNT, red)
        assert set(legacy) == {'item', 'media_ids', 'preview_ids', 'price_cents', 'files'}      # dynamic: no source, no commercial
        assert catalog_terms(s, ACCOUNT, red, price_cents=1300)['price_cents'] == 1300           # dynamic override above saved
        with pytest.raises(Refused):
            catalog_terms(s, ACCOUNT, red, price_cents=900)                                      # never below the saved price
        s.get(CatalogItem, red).pricing_mode = 'fixed'
        s.flush()
        fixed = catalog_terms(s, ACCOUNT, red)
        assert set(fixed) == {'item', 'media_ids', 'preview_ids', 'price_cents', 'files', 'source'} and 'commercial' not in fixed
        with pytest.raises(Refused):
            catalog_terms(s, ACCOUNT, red, price_cents=1300)
        s.rollback()
    store.close()


# ===================================================== (d) ownership: whole-bundle exclusion, unknown never offers ====

def ownership_world(database):
    store, authority, engine, model, red, blue = engine_setup(database)
    add_vault(database[1], {31: 'image', 32: 'image', 33: 'image'})
    one = add_item(database[1], label='Alpha one', media=[31], price=1200)
    two = add_item(database[1], label='Alpha two', media=[32], price=1200)
    trio = add_item(database[1], label='Alpha trio bundle', media=[31, 32, 33], price=2800)
    for item, price in ((one, 1200), (two, 1200), (trio, 2800)):
        approve(store, item, standard=price)
    return store, one, two, trio


def buy(engine, media):
    with Session(engine) as s, s.begin():
        s.add(VaultSend(account_id=ACCOUNT, fan_id=FAN, media_id=media, message_id=None, price_cents=1200,
                        sent_at=utcnow(), was_purchased=True))


def labels(result):
    return sorted(i['label'] for i in result['items'])


def test_d_a_bundle_with_one_owned_paid_asset_is_excluded_but_a_sibling_single_is_still_offered(database):
    from conversation_engine.ownership_gate import ownership_gate
    from onlyfans_runtime.media_delivery import catalog_terms
    store, one, two, trio = ownership_world(database)
    with Session(database[1]) as s, s.begin():
        assert labels(search_scoped(s, runtime(store, s), FAN, 'alpha', syn_coverage(store))) == ['Alpha one', 'Alpha trio bundle', 'Alpha two']
    buy(database[1], 32)
    with Session(database[1]) as s, s.begin():
        result = search_scoped(s, runtime(store, s), FAN, 'alpha', syn_coverage(store))
        assert labels(result) == ['Alpha one']                      # the owned single and the bundle containing it are gone
        ownership_gate(s, ACCOUNT, FAN, catalog_terms(s, ACCOUNT, one, lock=False))
        for owned in (two, trio):
            with pytest.raises(Refused, match='owned'):
                ownership_gate(s, ACCOUNT, FAN, catalog_terms(s, ACCOUNT, owned, lock=False))
    store.close()


@pytest.mark.parametrize('unknown', ['read_overflow', 'commerce_hold', 'unmapped_paid_message'])
def test_d_unknown_ownership_offers_nothing_and_is_never_read_as_unowned(database, monkeypatch, unknown):
    from conversation_engine.ownership_gate import ownership_gate
    from onlyfans_runtime.media_delivery import catalog_terms
    store, one, two, trio = ownership_world(database)
    if unknown == 'read_overflow':
        import ownership

        def overflow(*args, **kwargs):
            raise ValueError('ownership evidence exceeds bounded read')
        monkeypatch.setattr(ownership, 'owned_media_in_session', overflow)
    elif unknown == 'commerce_hold':
        from onlyfans_runtime import commerce as of_commerce
        monkeypatch.setattr(of_commerce, 'held', lambda *a, **k: True)
    else:
        with Session(database[1]) as s, s.begin():
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=7001, direction='out', body='locked',
                          media_ids='[]', media_count=2, price_cents=1500, is_paid=True))
    with Session(database[1]) as s, s.begin():
        terms = catalog_terms(s, ACCOUNT, one, lock=False)
        with pytest.raises(Refused, match='ownership|review'):
            ownership_gate(s, ACCOUNT, FAN, terms)
        result = search_scoped(s, runtime(store, s), FAN, 'alpha', syn_coverage(store))
    assert result['state'] == 'unavailable' and result['items'] == [] and result['reason'] == 'ownership_unknown'
    store.close()


def test_d_engine_purchase_list_is_scoped_unbounded_and_none_when_ownership_is_unknown(database):
    from conversation_engine.engine import _verified_purchases
    store, one, two, trio = ownership_world(database)
    bulk_commercial(database[1], count=260, binding_id=runtime_of(store), first_media=300000)
    buy(database[1], 32)
    buy(database[1], 300005)
    with Session(database[1]) as s, s.begin():
        rows = _verified_purchases(s, runtime(store, s), FAN)
    assert rows is not None and {r['label'] for r in rows} >= {'Alpha two', 'Studio set 5'} and 'Alpha trio bundle' not in {r['label'] for r in rows}
    store.close()


# ===================================================== (e) terms change: successor only when provably unsent ==========

def test_e_a_terms_revision_on_an_unsent_grant_issues_exactly_one_successor_with_its_predecessor(database):
    from conversation_engine.commerce import _hash, SyntheticCoverage, approve_simulated_offer, terms_successor, verified_terms
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_engine_models import OfEngineOwner
    from onlyfans_runtime.catalogue_terms import set_discount
    from onlyfans_runtime.media_delivery import catalog_terms
    store, authority, engine, model, red, blue = engine_setup(database)
    approve(store, blue, standard=1200)
    grant_id = approve_simulated_offer(store, TENANT, ACCOUNT, FAN, blue,
        SyntheticCoverage(ACCOUNT, FAN, blue, (21, 22), 'synthetic:complete-ownership', utcnow() + timedelta(minutes=5)))
    set_discount(store, TENANT, ACCOUNT, blue, discount_cents=900, command_key='f4' * 32, actor=AGENCY)
    with Session(database[1]) as s, s.begin():
        rt = runtime(store, s)
        owner, policy = s.get(OfEngineOwner, (ACCOUNT, FAN)), s.get(OfDeliveryPolicy, ACCOUNT)
        prior = s.get(OfEngineOfferGrant, (ACCOUNT, grant_id))
        with pytest.raises(Refused, match='terms'):
            verified_terms(s, rt, owner, prior)                        # stale, not silently repriced
        successor = terms_successor(s, rt, owner, prior, policy)
        again = terms_successor(s, rt, owner, prior, policy)
        s.flush()
        assert again.grant_id == successor.grant_id
        assert (successor.predecessor_grant_id, successor.issuance, successor.issuance_reason) == (grant_id, 1, 'terms_revision')
        assert successor.terms_hash == _hash(catalog_terms(s, ACCOUNT, blue)) != prior.terms_hash
        authorization = json.loads(successor.authorization_json)
        assert authorization['kind'] == 'terms_successor' and authorization['predecessor'] == grant_id
        assert authorization['oldRevision'] == 1 and authorization['newRevision'] == 2 and authorization['oldTermsHash'] == prior.terms_hash
        assert verified_terms(s, rt, owner, successor)['commercial']['revision'] == 2
        with pytest.raises(Refused, match='successor'):
            verified_terms(s, rt, owner, prior)
        assert s.scalar(select(func.count()).select_from(OfEngineOfferGrant)) == 2
    store.close()


def test_e_a_discount_successor_is_a_variant_change_and_a_repeat_preparer_gets_the_same_one(database):
    from conversation_engine.commerce import SyntheticCoverage, approve_simulated_offer, terms_successor
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from db.onlyfans_engine_models import OfEngineOwner
    from onlyfans_runtime.catalogue_terms import set_discount
    store, authority, engine, model, red, blue = engine_setup(database)
    approve(store, blue, standard=1200, discount=900)
    grant_id = approve_simulated_offer(store, TENANT, ACCOUNT, FAN, blue,
        SyntheticCoverage(ACCOUNT, FAN, blue, (21, 22), 'synthetic:complete-ownership', utcnow() + timedelta(minutes=5)))
    with Session(database[1]) as s, s.begin():
        rt = runtime(store, s)
        owner, policy = s.get(OfEngineOwner, (ACCOUNT, FAN)), s.get(OfDeliveryPolicy, ACCOUNT)
        prior = s.get(OfEngineOfferGrant, (ACCOUNT, grant_id))
        successor = terms_successor(s, rt, owner, prior, policy, variant='discount')
        assert successor.issuance_reason == 'discount_variant'
        s.flush()
        assert terms_successor(s, rt, owner, prior, policy, variant='discount').grant_id == successor.grant_id   # a second preparer
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfEngineOfferGrant).where(
            OfEngineOfferGrant.predecessor_grant_id == grant_id)) == 1
    store.close()


def test_e_a_sent_card_is_never_replaced_and_its_rows_stay_byte_identical(database):
    from conversation_engine.commerce import terms_successor
    from db.models import ContentOffer
    from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
    from db.onlyfans_engine_models import OfEngineOwner
    from onlyfans_runtime.catalogue_terms import set_discount
    store, authority, engine, model, red, blue = engine_setup(database)
    approve(store, blue, standard=1200)
    send_offer(store, authority, engine, model, blue, (21, 22), 'show blue', 1, 9802)
    with Session(database[1]) as s:
        grant = s.scalar(select(OfEngineOfferGrant))
        intent = s.scalar(select(OfTextIntent))
        frozen = (intent.price_cents, intent.catalog_snapshot_json, intent.media_json, intent.status, grant.terms_hash)
        offers = s.execute(select(ContentOffer.id, ContentOffer.price_cents, ContentOffer.item_id)).all()
    set_discount(store, TENANT, ACCOUNT, blue, discount_cents=900, command_key='f5' * 32, actor=AGENCY)
    with Session(database[1]) as s, s.begin():
        rt = runtime(store, s)
        with pytest.raises(Refused, match='fenced|dispatch|offer'):
            terms_successor(s, rt, s.get(OfEngineOwner, (ACCOUNT, FAN)), s.get(OfEngineOfferGrant, (ACCOUNT, grant.grant_id)),
                            s.get(OfDeliveryPolicy, ACCOUNT), variant='discount')
    with Session(database[1]) as s:
        grant2, intent2 = s.scalar(select(OfEngineOfferGrant)), s.scalar(select(OfTextIntent))
        assert (intent2.price_cents, intent2.catalog_snapshot_json, intent2.media_json, intent2.status, grant2.terms_hash) == frozen
        assert s.execute(select(ContentOffer.id, ContentOffer.price_cents, ContentOffer.item_id)).all() == offers
        assert s.scalar(select(func.count()).select_from(OfEngineOfferGrant)) == 1
        with pytest.raises(DBAPIError):
            s.execute(text("UPDATE of_catalogue_terms SET standard_price_cents=1300 WHERE item_id=:i"), {'i': blue})
    store.close()
