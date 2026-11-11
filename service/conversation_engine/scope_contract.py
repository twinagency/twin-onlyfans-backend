"""The three scope contracts that share the one-row-per-account authority table.

legacy       scope_id 1, no ``contract_version``: the frozen two-fan, USD 3.00 trial pilot (decision D1).
preparation  scope_id > 1, ``preparation.v1``: read-only import authority and a provider read budget. Never sends.
commercial   scope_id > 1, ``commercial.v1``: the same row promoted IN PLACE (same scope_id, binding, created_at,
             counters, last attempt) to an explicit audience, creator profile, named operator and model. Promotion
             grants nothing: the row stays ``configured`` until the named operator activates it.

Product approval (the ``catalogue`` snapshot, owned by a later step) and runtime authority (``operator`` +
``authorization_mode`` + ``status``) are separate fields written by separate commands. This module is pure
validation and dispatch; it performs no provider, model or message I/O.
"""
from datetime import datetime
from hashlib import sha256
import json
import re

from onlyfans_runtime.store import Refused

LEGACY, PREPARATION, COMMERCIAL = 'legacy', 'preparation', 'commercial'
PREPARATION_VERSION = 'preparation.v1'
COMMERCIAL_VERSION = 'commercial.v1'
CATALOGUE_KIND = 'commercial-catalogue.v1'
MAX_AUDIENCE = 100          # decision D2: explicit list only, never "all fans"
PRODUCT_UNAVAILABLE = 'commercial_product_contract_unavailable'

_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_LIMIT_KEYS = ('send_units', 'daily_send_units', 'throttle_seconds', 'provider_requests',
               'provider_credit_budget', 'provider_credits_per_request')
_PROMOTE_KEYS = frozenset({'fans', 'creator_profile', 'delivery_mode', 'operator', 'model', 'limits'})
_CATALOGUE_KEYS = frozenset({'account_id', 'binding_id', 'complete', 'evidence_ref', 'valid_until', 'snapshot_hash', 'snapshot_kind'})
_TRANSITION_KEYS = frozenset({'from', 'from_revision', 'from_fingerprint', 'command_key'})
# Fields that identify the provider account and its qualification evidence; promotion copies them, nothing edits them.
IDENTITY_KEYS = ('contract_version', 'provider_account', 'creator_id', 'qualification', 'transition')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def fingerprint(value):
    return sha256(encoded(value).encode()).hexdigest()


def contract_kind(config):
    """Which contract a stored configuration claims; unknown or malformed claims fail closed."""
    if type(config) is not dict:
        raise Refused('invalid scope configuration')
    if 'contract_version' not in config:
        return LEGACY
    version = config['contract_version']
    if version == PREPARATION_VERSION:
        return PREPARATION
    if version == COMMERCIAL_VERSION:
        return COMMERCIAL
    raise Refused('unsupported scope contract')


def is_commercial(config):
    return type(config) is dict and config.get('contract_version') == COMMERCIAL_VERSION


def validate_catalogue(catalogue):
    """Shape, binding and explicit UTC expiry of a product-approval snapshot. Its hash is computed by its owner."""
    if type(catalogue) is not dict or set(catalogue) != _CATALOGUE_KEYS:
        raise Refused('invalid commercial catalogue fields')
    if catalogue['snapshot_kind'] != CATALOGUE_KIND:
        raise Refused('unsupported commercial catalogue version')
    if (type(catalogue['account_id']) is not str or not catalogue['account_id'] or len(catalogue['account_id']) > 128
            or type(catalogue['binding_id']) is not str or not catalogue['binding_id'] or len(catalogue['binding_id']) > 128
            or catalogue['complete'] is not True or type(catalogue['evidence_ref']) is not str
            or not catalogue['evidence_ref'].startswith('vault:') or not 6 < len(catalogue['evidence_ref']) <= 128
            or type(catalogue['snapshot_hash']) is not str or not _HEX64.match(catalogue['snapshot_hash'])
            or type(catalogue['valid_until']) is not str):
        raise Refused('invalid commercial catalogue')
    try:
        expiry = datetime.fromisoformat(catalogue['valid_until'])
        if expiry.tzinfo is not None or expiry.isoformat() != catalogue['valid_until']:
            raise ValueError()
    except ValueError:
        raise Refused('commercial catalogue needs an explicit UTC expiry') from None
    return dict(catalogue)


def validate_transition(transition):
    if type(transition) is not dict or set(transition) != _TRANSITION_KEYS:
        raise Refused('invalid commercial transition fields')
    revision = transition['from_revision']
    if (transition['from'] != PREPARATION_VERSION or type(revision) is not int or not 1 <= revision < 2**31
            or type(transition['from_fingerprint']) is not str or not _HEX64.match(transition['from_fingerprint'])
            or type(transition['command_key']) is not str or not _HEX64.match(transition['command_key'])):
        raise Refused('invalid commercial transition lineage')
    return dict(transition)


def validate_commercial(config):
    """Normalised commercial.v1 configuration; every other shape is refused (no product, no two-fan rule)."""
    from . import pilot_scope as base
    required = {'contract_version', 'provider_account', 'creator_id', 'qualification', 'limits', 'fans', 'creator_profile',
                'authorization_mode', 'delivery_mode', 'operator', 'model', 'transition'}
    if type(config) is not dict or not required <= set(config) <= required | {'catalogue'}:
        raise Refused('invalid commercial scope fields')
    if config['contract_version'] != COMMERCIAL_VERSION:
        raise Refused('unsupported commercial scope contract')
    from .preparation import validate as validate_identity
    qualification, limits = config['qualification'], config['limits']
    # The immutable identity half is exactly what preparation validates (account shape, creator id, evidence).
    validate_identity({'contract_version': PREPARATION_VERSION, 'provider_account': config['provider_account'],
                       'creator_id': config['creator_id'], 'qualification': qualification,
                       'limits': {'provider_requests': 1, 'provider_credit_budget': 1, 'provider_credits_per_request': 1}})
    if config['delivery_mode'] not in ('text_only', 'full'):
        raise Refused('invalid commercial delivery mode')
    if config['authorization_mode'] not in ('supervised', 'autonomous'):
        raise Refused('explicit commercial authorization mode is required')
    from .writer_context import CreatorProfile, profile_data
    try:
        profile = CreatorProfile.from_dict(config['creator_profile'])
    except (KeyError, ValueError, TypeError):
        raise Refused('invalid commercial creator profile') from None
    if profile.missing_inputs():
        raise Refused('commercial creator boundaries are required')
    fans = base._ids(config['fans'], empty=True, maximum=MAX_AUDIENCE)
    operator = base.validated_operator(config['operator'], config['authorization_mode'])
    model = base.validated_model(config['model'])
    base._exact(limits, _LIMIT_KEYS)
    send_units = base._integer(limits['send_units'], 1, 10000)
    daily_units = base._integer(limits['daily_send_units'], 1, min(send_units, 20000))
    throttle = base._integer(limits['throttle_seconds'], 1, 86400)
    requests = base._integer(limits['provider_requests'], 1, 10**7)
    credits = base._integer(limits['provider_credit_budget'], 1, 10**12)
    per_request = base._integer(limits['provider_credits_per_request'], 1, credits)
    result = {'contract_version': COMMERCIAL_VERSION, 'provider_account': config['provider_account'],
              'creator_id': config['creator_id'],
              'qualification': {'mode': qualification['mode'], 'reference': qualification['reference']},
              'fans': fans, 'creator_profile': profile_data(profile), 'authorization_mode': config['authorization_mode'],
              'delivery_mode': config['delivery_mode'], 'operator': operator, 'model': model,
              'limits': {'send_units': send_units, 'daily_send_units': daily_units, 'throttle_seconds': throttle,
                         'provider_requests': requests, 'provider_credit_budget': credits,
                         'provider_credits_per_request': per_request},
              'transition': validate_transition(config['transition'])}
    if 'catalogue' in config:
        result['catalogue'] = validate_catalogue(config['catalogue'])
    elif config['delivery_mode'] == 'full':
        raise Refused('full commercial delivery needs a catalogue snapshot')
    return result


def validate(kind, config):
    """The normalised configuration for a claimed contract kind."""
    if kind == LEGACY:
        from .pilot_scope import validate as legacy
        return legacy(config)
    if kind == PREPARATION:
        from .preparation import validate as preparation
        return preparation(config)
    if kind == COMMERCIAL:
        return validate_commercial(config)
    raise Refused('unsupported scope contract')


def validate_any(config):
    """(kind, normalised configuration) for a stored configuration of unknown kind."""
    kind = contract_kind(config)
    return kind, validate(kind, config)


def promotion_config(preparation_config, preparation_revision, preparation_fingerprint, command_key, body):
    """The commercial.v1 configuration a preparation row is promoted to.

    Identity, qualification evidence and lineage come from the stored preparation authority, never from the caller.
    The caller names the audience, creator profile, operator, model, delivery mode and limits. Authorization is always
    ``supervised`` at promotion: autonomy is a later decision of the named operator, not of the promoting agency.
    """
    if type(body) is not dict or set(body) not in (_PROMOTE_KEYS, _PROMOTE_KEYS | {'catalogue'}):
        raise Refused('invalid promotion fields')
    if contract_kind(preparation_config) != PREPARATION:
        raise Refused('only a preparation scope can be promoted')
    config = {'contract_version': COMMERCIAL_VERSION, 'provider_account': preparation_config['provider_account'],
              'creator_id': preparation_config['creator_id'], 'qualification': dict(preparation_config['qualification']),
              'authorization_mode': 'supervised',
              'transition': {'from': PREPARATION_VERSION, 'from_revision': preparation_revision,
                             'from_fingerprint': preparation_fingerprint, 'command_key': command_key},
              **{key: body[key] for key in body}}
    return validate_commercial(config)


def preparation_projection(config):
    """The preparation view of a commercial row: identity and the three shared read limits only."""
    limits = config['limits']
    return {'contract_version': PREPARATION_VERSION, 'provider_account': config['provider_account'],
            'creator_id': config['creator_id'], 'qualification': dict(config['qualification']),
            'limits': {'provider_requests': limits['provider_requests'],
                       'provider_credit_budget': limits['provider_credit_budget'],
                       'provider_credits_per_request': limits['provider_credits_per_request']}}


def product_of(config):
    """The single pinned legacy product. A commercial scope has none until the catalogue contract exists."""
    if is_commercial(config):
        raise Refused(PRODUCT_UNAVAILABLE)
    return config['product']


def profile_items(config):
    """Catalogue item ids the creator profile must approve for this scope (legacy: the pilot product only)."""
    return () if is_commercial(config) else (str(config['product']['id']),)
