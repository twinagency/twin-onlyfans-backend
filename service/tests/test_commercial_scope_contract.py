"""Pure checks for the three scope contracts (legacy, preparation, commercial). No database, no network."""
import copy

import pytest

from conversation_engine import pilot_scope, preparation, scope_contract as sc
from conversation_engine.writer_context import CreatorProfile, profile_data
from onlyfans_runtime.store import Refused

ACTOR = 'op-session:' + 'a' * 64
KEY = 'c' * 64
PERMISSIONS = ['configure', 'activate', 'approve', 'reject', 'edit', 'stop', 'takeover', 'handback', 'reconcile', 'correct', 'read']


def profile():
    return profile_data(CreatorProfile(name='Test Creator', limits=('no meetups',), role_statement='the creator'))


def model():
    return {'provider': 'openrouter', 'primary': 'vendor/model-one', 'upstream': 'vendor', 'reported': 'model one',
            'version': 'cfg-0123456789ab', 'daily_cap_microusd': 1000, 'creator_cap_microusd': 500}


def limits(**kw):
    return {'send_units': 20, 'daily_send_units': 10, 'throttle_seconds': 5, 'provider_requests': 50,
            'provider_credit_budget': 100, 'provider_credits_per_request': 2, **kw}


def prep_config():
    return {'contract_version': 'preparation.v1', 'provider_account': 'acct_one', 'creator_id': '101',
            'qualification': {'mode': 'fixture', 'reference': 'isolated qualification'},
            'limits': {'provider_requests': 10, 'provider_credit_budget': 20, 'provider_credits_per_request': 2}}


def body(**kw):
    return {'fans': [11, 12, 13], 'creator_profile': profile(), 'delivery_mode': 'text_only',
            'operator': {'actor': ACTOR, 'name': 'Operator', 'permissions': PERMISSIONS},
            'model': model(), 'limits': limits(), **kw}


def promoted(**kw):
    return sc.promotion_config(prep_config(), 3, 'f' * 64, KEY, body(**kw))


def catalogue(**kw):
    return {'account_id': 'acc', 'binding_id': 'bind', 'complete': True, 'evidence_ref': 'vault:catalogue-1',
            'valid_until': '2099-01-01T00:00:00', 'snapshot_hash': 'a' * 64, 'snapshot_kind': 'commercial-catalogue.v1', **kw}


def legacy_config():
    return {'creator_profile': profile(), 'authorization_mode': 'supervised',
            'qualification': {'mode': 'fixture', 'reference': 'x'}, 'fans': [1, 2],
            'product': {'id': 7, 'price_cents': 300, 'media_ids': [9], 'preview_ids': []},
            'operator': {'actor': ACTOR, 'name': 'Operator', 'permissions': PERMISSIONS},
            'model': model(), 'limits': limits()}


def test_contract_kind_follows_the_stored_claim_and_unknown_claims_fail_closed():
    assert sc.contract_kind(legacy_config()) == sc.LEGACY
    assert sc.contract_kind(prep_config()) == sc.PREPARATION
    assert sc.contract_kind(promoted()) == sc.COMMERCIAL
    for bad in ({'contract_version': 'commercial.v2'}, {'contract_version': None}, {'contract_version': ''}, [], None, 'x'):
        with pytest.raises(Refused):
            sc.contract_kind(bad)


def test_legacy_trial_rules_stay_exactly_two_fans_and_the_three_dollar_photo():
    assert pilot_scope.validate(legacy_config())['fans'] == [1, 2]
    for patch in ({'fans': [1]}, {'fans': [1, 2, 3]}, {'product': {'id': 7, 'price_cents': 400, 'media_ids': [9], 'preview_ids': []}}):
        with pytest.raises(Refused):
            pilot_scope.validate({**legacy_config(), **patch})
    # A legacy row never carries a contract: the claim itself is refused by the legacy validator.
    with pytest.raises(Refused):
        pilot_scope.validate({**legacy_config(), 'contract_version': 'commercial.v1'})


def test_commercial_audience_is_explicit_and_capped_at_one_hundred():
    assert promoted(fans=list(range(1, 101)))['fans'] == list(range(1, 101))
    assert promoted(fans=[])['fans'] == []     # configured is allowed to be empty; activation requires a fan
    for fans in (list(range(1, 102)), [1, 1], [0], [-4], ['7'], [True], 'all', None, {'all': True}):
        with pytest.raises(Refused):
            promoted(fans=fans)
    cfg = promoted()
    cfg['fans'] = [2**63]
    with pytest.raises(Refused):
        sc.validate_commercial(cfg)


def test_commercial_has_no_pinned_product_two_fan_or_three_dollar_rule():
    cfg = promoted(fans=[5])
    assert 'product' not in cfg and cfg['fans'] == [5]
    with pytest.raises(Refused):
        sc.validate_commercial({**cfg, 'product': {'id': 7, 'price_cents': 300, 'media_ids': [9], 'preview_ids': []}})
    with pytest.raises(Refused):
        sc.validate_commercial({**cfg, 'offer_coverage': catalogue()})
    with pytest.raises(Refused, match='commercial_product_contract_unavailable'):
        sc.product_of(cfg)
    assert sc.product_of(legacy_config())['id'] == 7
    assert sc.profile_items(cfg) == () and sc.profile_items(legacy_config()) == ('7',)


def test_validation_is_idempotent_canonical_and_fingerprint_stable():
    cfg = promoted(catalogue=catalogue())
    again = sc.validate_commercial(copy.deepcopy(cfg))
    assert again == cfg and sc.encoded(again) == sc.encoded(cfg) and sc.fingerprint(again) == sc.fingerprint(cfg)
    shuffled = body()
    shuffled['operator'] = {**shuffled['operator'], 'permissions': list(reversed(PERMISSIONS))}
    assert sc.promotion_config(prep_config(), 3, 'f' * 64, KEY, shuffled)['operator']['permissions'] == sorted(PERMISSIONS)
    assert sc.fingerprint(promoted()) != sc.fingerprint(promoted(fans=[11, 12]))
    assert sc.validate(sc.COMMERCIAL, cfg) == cfg and sc.validate_any(cfg) == (sc.COMMERCIAL, cfg)
    assert sc.validate_any(prep_config())[0] == sc.PREPARATION and sc.validate_any(legacy_config())[0] == sc.LEGACY


def test_promotion_copies_identity_and_lineage_from_the_stored_preparation_and_grants_nothing():
    cfg = promoted()
    prep = prep_config()
    assert (cfg['provider_account'], cfg['creator_id'], cfg['qualification']) == (prep['provider_account'], prep['creator_id'], prep['qualification'])
    assert cfg['transition'] == {'from': 'preparation.v1', 'from_revision': 3, 'from_fingerprint': 'f' * 64, 'command_key': KEY}
    assert cfg['authorization_mode'] == 'supervised'
    for forbidden in ({'authorization_mode': 'autonomous'}, {'provider_account': 'acct_other'}, {'creator_id': '999'},
                      {'qualification': {'mode': 'production', 'reference': 'r'}}, {'transition': cfg['transition']},
                      {'contract_version': 'commercial.v1'}, {'product': {}}, {'status': 'active'}, {'active': True}):
        with pytest.raises(Refused):
            sc.promotion_config(prep_config(), 3, 'f' * 64, KEY, {**body(), **forbidden})
    for missing in ('fans', 'creator_profile', 'delivery_mode', 'operator', 'model', 'limits'):
        partial = body()
        del partial[missing]
        with pytest.raises(Refused):
            sc.promotion_config(prep_config(), 3, 'f' * 64, KEY, partial)
    with pytest.raises(Refused, match='preparation'):
        sc.promotion_config(cfg, 3, 'f' * 64, KEY, body())          # a commercial scope cannot be promoted again
    with pytest.raises(Refused):
        sc.promotion_config(legacy_config(), 3, 'f' * 64, KEY, body())


def test_lineage_shape_is_exact():
    good = promoted()['transition']
    for patch in ({'from': 'commercial.v1'}, {'from_revision': 0}, {'from_revision': '3'}, {'from_revision': True},
                  {'from_fingerprint': 'F' * 64}, {'from_fingerprint': 'f' * 63}, {'command_key': 'z' * 64}, {'extra': 1}):
        with pytest.raises(Refused):
            sc.validate_transition({**good, **patch})
    for drop in good:
        with pytest.raises(Refused):
            sc.validate_transition({k: v for k, v in good.items() if k != drop})
    with pytest.raises(Refused):
        sc.validate_commercial({k: v for k, v in promoted().items() if k != 'transition'})


def test_catalogue_shape_full_mode_needs_it_and_text_only_may_omit_it():
    assert promoted()['delivery_mode'] == 'text_only' and 'catalogue' not in promoted()
    assert promoted(delivery_mode='full', catalogue=catalogue())['catalogue'] == catalogue()
    with pytest.raises(Refused, match='catalogue'):
        promoted(delivery_mode='full')
    for patch in ({'snapshot_kind': 'pilot-product.v1'}, {'complete': False}, {'evidence_ref': 'drive:abc123'},
                  {'snapshot_hash': 'a' * 63}, {'valid_until': '2099-01-01T00:00:00Z'}, {'valid_until': '2099-01-01'},
                  {'valid_until': 5}, {'account_id': ''}, {'binding_id': 7}):
        with pytest.raises(Refused):
            promoted(delivery_mode='full', catalogue=catalogue(**patch))
    extra = catalogue()
    extra['price'] = 3
    with pytest.raises(Refused):
        promoted(catalogue=extra)
    missing = catalogue()
    del missing['snapshot_kind']
    with pytest.raises(Refused):
        promoted(catalogue=missing)


def test_operator_and_runtime_authority_are_explicit_and_the_profile_is_complete():
    for operator in ({'actor': 'agency-user:' + '1' * 8 + '-' + '1' * 4 + '-' + '1' * 4 + '-' + '1' * 4 + '-' + '1' * 12 + ':x',
                      'name': 'Agency', 'permissions': PERMISSIONS},
                     {'actor': ACTOR, 'name': 'Operator', 'permissions': ['read']},
                     {'actor': ACTOR, 'name': ' Operator', 'permissions': PERMISSIONS},
                     {'actor': ACTOR, 'name': 'Operator', 'permissions': [p for p in PERMISSIONS if p != 'approve']}):
        with pytest.raises(Refused):
            promoted(operator=operator)
    cfg = promoted()
    # Switching to autonomous is the named operator's later configure; it is valid shape once approve is not required.
    cfg['authorization_mode'] = 'autonomous'
    assert sc.validate_commercial(cfg)['authorization_mode'] == 'autonomous'
    cfg['authorization_mode'] = 'maybe'
    with pytest.raises(Refused):
        sc.validate_commercial(cfg)
    bare = profile_data(CreatorProfile(name='No boundaries'))
    with pytest.raises(Refused, match='boundaries'):
        promoted(creator_profile=bare)


def test_limits_keep_the_preparation_counter_names_and_ranges():
    cfg = promoted()
    assert set(cfg['limits']) == {'send_units', 'daily_send_units', 'throttle_seconds', 'provider_requests',
                                  'provider_credit_budget', 'provider_credits_per_request'}
    for patch in ({'provider_requests': 0}, {'daily_send_units': 21}, {'provider_credits_per_request': 101},
                  {'throttle_seconds': 0}, {'send_units': True}):
        with pytest.raises(Refused):
            promoted(limits=limits(**patch))
    short = limits()
    del short['throttle_seconds']
    with pytest.raises(Refused):
        promoted(limits=short)


def test_preparation_projection_keeps_only_the_shared_read_view():
    cfg = promoted()
    projected = sc.preparation_projection(cfg)
    assert projected == preparation.validate(projected)
    assert projected['limits'] == {'provider_requests': 50, 'provider_credit_budget': 100, 'provider_credits_per_request': 2}
    assert projected['provider_account'] == 'acct_one' and projected['creator_id'] == '101'
