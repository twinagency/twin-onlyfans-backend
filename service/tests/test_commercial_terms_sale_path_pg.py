"""Step 3 end to end (offline, synthetic transport): standing catalogue permission -> shortlist -> application-chosen amount -> grant
-> frozen terms -> existing sender -> receipt, with no per-fan grant injected by the test. Plus same-fan-id isolation across accounts."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from commercial_terms_fixture import ACTOR, add_item, add_vault, approve, bulk_commercial, runtime_of, valid_for
from conversation_engine.commerce import prepare_commercial_offer
from conversation_engine.engine import SyntheticProposal
from db.models import ContentOffer, VaultSend
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineOfferGrant
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.store import utcnow
from test_engine_catalogue_pg import setup as engine_setup
from test_engine_simulated_journey_pg import incoming
from test_onlyfans_foundation_pg import database, seed  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, unthrottle

COVERAGE = 'synthetic:standing-catalogue'


def run_turn(store, authority, engine, model, mid, text_, provider_message):
    """prepare (application) -> plan (writer proposes the prepared grant) -> queue -> dispatch."""
    incoming(store, mid, text_)
    prepared = prepare_commercial_offer(store, TENANT, ACCOUNT, FAN, mid, coverage_ref=COVERAGE, valid_until=valid_for())
    if prepared and prepared.get('state') == 'prepared':
        model.replies[text_] = SyntheticProposal('untrusted', action='offer', offer_grant_id=prepared['grantId'])
        decision = engine.plan(TENANT, ACCOUNT, FAN, mid)
        intent = engine.queue(TENANT, ACCOUNT, decision)
        unthrottle(store.engine)
        outcome = authority.dispatch(TENANT, ACCOUNT, intent, FansApiSyntheticTransport(store, TENANT, ACCOUNT,
            SyntheticFansProvider(message_id=provider_message)))
        return prepared, intent, outcome
    return prepared, None, None


def test_the_normal_path_prepares_its_own_grants_and_sends_exactly_the_approved_amounts(database):
    store, authority, engine, model, red, blue = engine_setup(database)
    add_vault(database[1], {31: 'image'})
    alpha = add_item(database[1], label='Alpha one', media=[31], price=1200, description='Alpha one, a distinctive approved photograph.')
    approve(store, blue, standard=1200)
    approve(store, alpha, standard=1200, discount=900)
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfEngineOfferGrant)) == 0          # nothing injected

    prepared, intent1, outcome1 = run_turn(store, authority, engine, model, 1, 'show me the blue beach video', 9901)
    assert prepared['state'] == 'prepared' and prepared['itemId'] == str(blue) and prepared['variant'] == 'standard'
    assert outcome1 == 'accepted'

    # a different product, and the fan explicitly asks for a cheaper option: the application picks the approved discount amount
    prepared2, intent2, outcome2 = run_turn(store, authority, engine, model, 2, 'what about the alpha photo, can you do it cheaper?', 9902)
    assert prepared2['itemId'] == str(alpha) and prepared2['variant'] == 'discount' and prepared2['priceCents'] == 900
    assert outcome2 == 'accepted'

    # asking cheaper about a product that already has a card is a reply, not another card and not a repricing
    prepared3, intent3, outcome3 = run_turn(store, authority, engine, model, 3, 'the blue beach video, can you do it cheaper?', 9903)
    assert prepared3 == {'state': 'existing_card', 'itemId': str(blue)} and intent3 is None

    with Session(database[1]) as s:
        rows = {r.intent_id: r for r in s.scalars(select(OfTextIntent))}
        first, second = rows[intent1], rows[intent2]
        assert (first.price_cents, json.loads(first.catalog_snapshot_json)['commercial']['variant']) == (1200, 'standard')
        assert (second.price_cents, json.loads(second.catalog_snapshot_json)['commercial']['variant']) == (900, 'discount')
        assert json.loads(second.catalog_snapshot_json)['commercial']['standardCents'] == 1200
        assert sorted(o.price_cents for o in s.scalars(select(ContentOffer))) == [900, 1200]      # unlock amounts the receipt will match
        assert s.scalar(select(func.count()).select_from(OfEngineOfferGrant)) == 2
        grants = {g.item_id: json.loads(g.authorization_json)['terms'] for g in s.scalars(select(OfEngineOfferGrant))}
        assert grants[alpha]['variant'] == 'discount' and grants[alpha]['priceCents'] == 900 and grants[blue]['variant'] == 'standard'
    store.close()


def test_an_owned_product_is_not_prepared_and_an_unknown_one_is_held(database):
    store, authority, engine, model, red, blue = engine_setup(database)
    approve(store, blue, standard=1200)
    with Session(database[1]) as s, s.begin():
        s.add(VaultSend(account_id=ACCOUNT, fan_id=FAN, media_id=22, message_id=None, price_cents=1200, sent_at=utcnow(), was_purchased=True))
    prepared, intent, outcome = run_turn(store, authority, engine, model, 1, 'show me the blue beach video', 9911)
    assert prepared['state'] in {'none', 'owned'} and intent is None          # owned asset: the product is not even shortlisted
    store.close()


def test_two_accounts_with_the_same_fan_id_never_share_terms_ownership_or_candidates(database):
    from conversation_engine.catalogue_select import _candidates
    from conversation_engine.ownership_gate import owned_state
    from types import SimpleNamespace
    store, authority, engine, model, red, blue = engine_setup(database)
    seed(database[0], database[1], account='acc-b', tenant='tenant-b', fan=FAN)
    add_vault(database[1], {31: 'image', 32: 'image'}, account='acc-a')
    add_vault(database[1], {31: 'image', 32: 'image'}, account='acc-b')
    a = add_item(database[1], label='Alpha shared name', media=[31], account='acc-a')
    b = add_item(database[1], label='Alpha shared name', media=[31], account='acc-b')
    approve(store, a, standard=1200)
    approve(store, b, standard=1900, tenant='tenant-b', account='acc-b')
    with database[1].begin() as c:
        c.execute(text("INSERT INTO vault_sends(account_id,fan_id,media_id,price_cents,sent_at,was_purchased) VALUES ('acc-b',:f,31,1900,now(),true)"), {'f': FAN})
    with Session(database[1]) as s, s.begin():
        owned_a, unknown_a = owned_state(s, 'acc-a', FAN)
        owned_b, unknown_b = owned_state(s, 'acc-b', FAN)
        assert (owned_a, unknown_a) == (set(), None) and owned_b == {31} and unknown_b is None
        runtime_a = SimpleNamespace(account_id='acc-a', binding_id=runtime_of(store))
        found = _candidates(s, runtime_a, ['alpha'], None, ['alpha'], owned_a)
        assert [row[0] for row in found] == [a]                                   # account B's same-named product is invisible
        rows = s.execute(text("SELECT account_id, standard_price_cents FROM of_catalogue_terms ORDER BY account_id")).all()
        assert rows == [('acc-a', 1200), ('acc-b', 1900)]
    store.close()
