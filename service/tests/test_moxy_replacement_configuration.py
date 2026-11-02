"""Offline source/configuration qualification, not engine or model quality proof."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from moxy_replacement.configuration import load, CreatorConfiguration, InvalidConfiguration, ActionBlocked
from moxy_replacement.authority import ProductAuthority, readiness


def state(**changes):
    return {'ownership_verified': True, 'purchase_links_verified': True,
            'commerce_uncertain': False, 'human_owned': False, 'safety_hold': False,
            'financial_hardship': False, 'ordinary_chat_only': False, 'sales_refused': False,
            'no_push': False, 'owned_assets': [], 'purchased_products': [],
            'pending_products': [], 'rejected_products': [], **changes}


def product(key='first', asset=101, **changes):
    return {'id': key, 'revision': 1, 'category': 'nude_booty', 'price_cents': 7500,
            'currency': 'USD', 'assets': [{'id': asset, 'revision': 1,
            'facts': ['Synthetic fixture: approved still image'], 'evidence_ref': 'fixture:asset-approval'}],
            'source': {'document': '01_Amelia.pdf', 'pages': [2]},
            'approval_ref': 'fixture:product-approval', 'available': True,
            'requires': {}, 'caption_instructions': 'Use only the verified asset facts.', **changes}


def authority(creator='amelia', products=None, sequences=None):
    return ProductAuthority(load(creator), products if products is not None else [product()], sequences or {})


@pytest.mark.parametrize('creator', ['amelia', 'cristina'])
def test_all_mapped_rules_have_valid_original_source_pages(creator):
    cfg = load(creator)
    assert len(cfg.fingerprint) == 64
    for rule in cfg.public_context()['rules']:
        assert rule['source']['document'] in cfg.foundation['documents']
        assert rule['source']['pages']


def test_conditional_creator_override_is_not_flattened_or_fallback():
    cfg = load('amelia')
    assert cfg.resolve('sexting', {'spender': True, 'vip': True}).authority == 'creator'
    with pytest.raises(ActionBlocked, match='condition_not_met'):
        cfg.resolve('sexting', {'spender': False, 'vip': True})
    with pytest.raises(ActionBlocked, match='condition_unknown'):
        cfg.resolve('sexting', {'spender': True})


def test_explicit_creator_permission_does_not_conflict_with_pg13_description():
    cfg = load('cristina')
    assert cfg.resolve('sexting', {}).value is True
    assert cfg.resolve('voice', {}).value == 'flirty_suggestive_sarcastic'


def test_foundation_fallback_is_independent():
    cfg = load('cristina')
    assert cfg.resolve('ordinary_chat', {}).authority == 'foundation'
    assert cfg.resolve('identity', {}).value == 'pause_owned_handover'
    assert cfg.resolve('complaints', {}).value == 'investigate_within_authority'


def test_third_creator_needs_configuration_only():
    original = load('amelia')
    third = deepcopy(original.creator)
    third['creator_key'], third['rules'], third['unresolved'] = 'synthetic_third', [], []
    cfg = CreatorConfiguration(original.foundation, third)
    assert cfg.resolve('sexting', {}).value is False
    assert cfg.resolve('privacy', {}).authority == 'foundation'
    assert cfg.public_context()['creator_key'] == 'synthetic_third'


def test_same_creator_ambiguity_is_rejected():
    original = load('amelia')
    creator = deepcopy(original.creator)
    creator['rules'].append(creator['rules'][0])
    with pytest.raises(InvalidConfiguration, match='ambiguity'):
        CreatorConfiguration(original.foundation, creator)


def test_private_creator_facts_do_not_enter_rule_context():
    cfg = load('amelia')
    rendered = json.dumps(cfg.public_context())
    assert 'Carson' not in rendered and 'Pittsburgh' not in rendered
    public = cfg.public_context()
    public['rules'][0]['instruction'] = 'mutated'
    assert cfg.public_context()['rules'][0]['instruction'] != 'mutated'


def test_page_reference_must_be_in_actual_pdf():
    original = load('amelia')
    creator = deepcopy(original.creator)
    creator['rules'][0]['source']['pages'] = [99]
    with pytest.raises(InvalidConfiguration, match='source page'):
        CreatorConfiguration(original.foundation, creator)


@pytest.mark.parametrize('name', ['human_owned', 'safety_hold', 'financial_hardship', 'ordinary_chat_only', 'sales_refused'])
def test_shared_stops_prevent_commercial_action(name):
    with pytest.raises(ActionBlocked, match=name):
        authority().terms('first', state(**{name: True}))


@pytest.mark.parametrize('name', ['human_owned', 'safety_hold', 'financial_hardship', 'ordinary_chat_only', 'sales_refused'])
def test_unknown_stops_do_not_grant_send_rights(name):
    unknown = state(); unknown.pop(name)
    with pytest.raises(ActionBlocked, match=name):
        authority().terms('first', unknown)


def test_budget_does_not_create_discount_ladder():
    with pytest.raises(ActionBlocked, match='price_exceeds_budget'):
        authority().terms('first', state(budget_cents=7000))
    assert authority().terms('first', state(budget_cents=7500))['product']['price_cents'] == 7500


def test_cannot_invent_product_or_lower_source_price():
    with pytest.raises(ActionBlocked, match='mapping_missing'):
        authority().terms('unknown', state())
    with pytest.raises(ActionBlocked, match='minimum'):
        authority(products=[product(price_cents=7400)]).terms('first', state())


@pytest.mark.parametrize('field,value,reason', [
    ('ownership_verified', False, 'ownership_evidence'),
    ('commerce_uncertain', True, 'reconciliation'),
    ('owned_assets', [101], 'already_owned'),
    ('purchased_products', ['first'], 'already_owned'),
    ('rejected_products', ['first'], 'previously_rejected'),
    ('pending_products', ['first'], 'already_pending'),
])
def test_exact_state_is_required_for_offer(field, value, reason):
    with pytest.raises(ActionBlocked, match=reason):
        authority().terms('first', state(**{field: value}))


def test_no_push_allows_requested_item_but_does_not_generate_interest():
    a = authority()
    with pytest.raises(ActionBlocked, match='no_push'):
        a.terms('first', state(no_push=True))
    assert a.terms('first', state(no_push=True, direct_request_product='first'))


def test_resend_requires_request_and_still_checks_ownership():
    a = authority()
    assert a.terms('first', state(pending_products=['first'], requested_resend_product='first'), purpose='resend')
    with pytest.raises(ActionBlocked, match='resend_not_requested'):
        a.terms('first', state(), purpose='resend')
    with pytest.raises(ActionBlocked, match='already_owned'):
        a.terms('first', state(owned_assets=[101], requested_resend_product='first'), purpose='resend')


def test_sequence_continuation_requires_exact_purchase_not_outgoing_receipt():
    a = authority(products=[product(), product('second', 102)], sequences={'vault': ['first', 'second']})
    with pytest.raises(ActionBlocked, match='not_current'):
        a.terms('second', state(pending_products=['first']), purpose='continuation', sequence='vault')
    with pytest.raises(ActionBlocked, match='purchase_links_missing'):
        a.terms('second', state(purchased_products=['first'], purchase_links_verified=False), purpose='continuation', sequence='vault')
    result = a.terms('second', state(purchased_products=['first']), purpose='continuation', sequence='vault')
    assert result['product']['id'] == 'second'
    assert a.next_step('vault', state(purchased_products=['first', 'second'])) is None


def test_continuation_does_not_require_another_inbound_message_or_wait_period():
    a = authority(products=[product(), product('second', 102)], sequences={'vault': ['first', 'second']})
    assert a.terms('second', state(purchased_products=['first']), purpose='continuation', sequence='vault')


def test_acceptance_binds_the_retained_invitation_and_rejects_revision_drift():
    a = authority()
    invite = {'product_id': 'first', 'fingerprint': a.fingerprint, 'sequence': None}
    assert a.accepted_invitation(invite, state())['product']['id'] == 'first'
    changed = authority(products=[product(revision=2)])
    with pytest.raises(ActionBlocked, match='stale_or_ambiguous'):
        changed.accepted_invitation(invite, state())


def test_nipples_have_creator_specific_minimum_and_verified_access_conditions():
    a = authority(products=[product(category='nipples', price_cents=20000)])
    with pytest.raises(ActionBlocked, match='condition_unknown'):
        a.terms('first', state())
    assert a.terms('first', state(vip=True, high_spender=True))
    with pytest.raises(ActionBlocked, match='condition_not_met'):
        a.terms('first', state(vip=False, high_spender=True))


def test_prohibited_asset_category_is_not_enabled_by_vip_or_spend():
    a = authority(products=[product(category='kitty', price_cents=20000)])
    with pytest.raises(ActionBlocked, match='content_prohibited'):
        a.terms('first', state(vip=True, high_spender=True))


def test_readiness_cannot_pass_without_real_mapping_and_operations():
    cfg = load('cristina')
    result = readiness(cfg, account_bound=True, products=[], sequences={}, operational={}, coordination_verified=False)
    assert result['ready'] is False and result['activates_account'] is False
    assert 'authorised_product_asset_mapping_missing' in result['blockers']
    assert 'operational_field_missing:graveyard_shift_definition' in result['blockers']


@pytest.mark.parametrize('creator', ['amelia', 'cristina'])
def test_historical_blanks_are_explicit_not_filled_from_messages(creator):
    assert load(creator).creator['unresolved']


def test_assets_need_fact_evidence_not_historical_caption():
    p = product(); p['assets'][0]['evidence_ref'] = ''
    with pytest.raises(InvalidConfiguration, match='asset facts'):
        authority(products=[p])


def test_readiness_rejects_nonempty_but_invalid_product_mapping():
    cfg = load('amelia')
    result = readiness(cfg, account_bound=True, products=[{'id': 'fake'}], sequences={'vault': ['fake']},
        operational={field: 'fixture' for field in cfg.creator['required_operational_fields']}, coordination_verified=True)
    assert result['ready'] is False
    assert 'invalid_product_or_sequence_mapping' in result['blockers']


def test_unknown_payment_information_does_not_block_unrelated_instruction_resolution():
    cfg = load('amelia')
    assert cfg.resolve('ordinary_chat', {}).value == 'respect_request'
    with pytest.raises(ActionBlocked, match='ownership_evidence_missing'):
        authority().terms('first', state(ownership_verified=False))
