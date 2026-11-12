"""Durable, fail-closed configuration for the one supervised Plan C pilot.

This is configuration and conservative request reservation, not a second
message outbox or financial ledger. A normal supervised worker requires an
explicitly activated, fully configured scope; no function starts a worker or
makes a provider/model call. All mutations lock the existing account runtime.
"""
from hashlib import sha256
from datetime import datetime
import json
import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CatalogItem, VaultItem
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEnginePilotScope, OfFansConnection
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from .scope_contract import product_pin

_ACTIONS = frozenset({'configure', 'activate', 'approve', 'reject', 'edit', 'stop', 'takeover', 'handback', 'reconcile', 'correct', 'read'})
_ACTOR = re.compile(r'op-session:[0-9a-f]{64}\Z')
_KEY = re.compile(r'[0-9a-f]{64}\Z')
_CFG = re.compile(r'cfg-[0-9a-f]{12}\Z')
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z')


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise Refused('invalid pilot configuration')
    return value


def _ids(values, *, empty=False, maximum=100):
    if type(values) is not list or len(values) > maximum or (not empty and not values):
        raise Refused('invalid pilot identifiers')
    out = [_integer(value, 1, 2**63-1) for value in values]
    if len(set(out)) != len(out):
        raise Refused('duplicate pilot identifiers')
    return out


def _exact(obj, keys):
    if type(obj) is not dict or set(obj) != set(keys):
        raise Refused('invalid pilot configuration fields')


def validated_operator(operator, authorization_mode):
    """The named operator and its explicit permissions; shared by every scope contract."""
    _exact(operator, ('actor', 'name', 'permissions'))
    actor, name, permissions = operator['actor'], operator['name'], operator['permissions']
    if (type(actor) is not str or not _ACTOR.fullmatch(actor) or type(name) is not str or not name.strip()
            or name != name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name)
            or type(permissions) is not list or any(type(value) is not str or value not in _ACTIONS for value in permissions)
            or len(set(permissions)) != len(permissions)
            or not {'configure', 'activate', 'stop', 'read'}.issubset(permissions)
            or authorization_mode == 'supervised' and 'approve' not in permissions):
        raise Refused('invalid pilot operator permissions')
    return {'actor':actor, 'name':name, 'permissions':sorted(permissions)}


def validated_model(model):
    """The pinned single-provider model and its spend caps; shared by every scope contract."""
    _exact(model, ('provider', 'primary', 'upstream', 'reported', 'version', 'daily_cap_microusd', 'creator_cap_microusd'))
    if (model['provider'] != 'openrouter' or type(model['primary']) is not str or not _MODEL.fullmatch(model['primary'])
            or model['primary'].startswith('openrouter/auto') or model['primary'].endswith(':auto')
            or type(model['upstream']) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9._:/-]{0,127}', model['upstream'])
            or type(model['reported']) is not str or not model['reported'].strip() or model['reported'] != model['reported'].strip()
            or len(model['reported']) > 64 or any(ord(c) < 32 for c in model['reported'])
            or type(model['version']) is not str or not _CFG.fullmatch(model['version'])):
        raise Refused('invalid pilot model configuration')
    daily = _integer(model['daily_cap_microusd'], 1, 10**12)
    creator = _integer(model['creator_cap_microusd'], 1, daily)
    return {'provider':'openrouter', 'primary':model['primary'], 'upstream':model['upstream'],
            'reported':model['reported'], 'version':model['version'],
            'daily_cap_microusd':daily, 'creator_cap_microusd':creator}


def validate(config):
    required = {'fans', 'product', 'operator', 'model', 'limits', 'qualification', 'authorization_mode', 'creator_profile'}
    if type(config) is not dict or not required <= set(config) <= required | {'offer_coverage', 'delivery_mode'}:
        raise Refused('invalid pilot configuration fields')
    if 'delivery_mode' in config and config['delivery_mode'] not in ('text_only', 'full'):
        raise Refused('invalid pilot delivery mode')
    coverage = config.get('offer_coverage')
    if 'offer_coverage' in config:
        fields = {'account_id', 'binding_id', 'complete', 'evidence_ref', 'valid_until', 'snapshot_hash'}
        if type(coverage) is not dict or set(coverage) not in (fields, fields | {'snapshot_kind'}):
            raise Refused('invalid pilot offer coverage fields')
        if 'snapshot_kind' in coverage and coverage['snapshot_kind'] != 'pilot-product.v1':
            raise Refused('unsupported pilot offer coverage version')
        if (type(coverage['account_id']) is not str or not coverage['account_id'] or len(coverage['account_id']) > 128
                or type(coverage['binding_id']) is not str or not coverage['binding_id'] or len(coverage['binding_id']) > 128
                or coverage['complete'] is not True or type(coverage['evidence_ref']) is not str
                or not coverage['evidence_ref'].startswith('vault:') or not 6 < len(coverage['evidence_ref']) <= 128
                or type(coverage['snapshot_hash']) is not str or not re.fullmatch('[0-9a-f]{64}', coverage['snapshot_hash'])
                or type(coverage['valid_until']) is not str):
            raise Refused('invalid pilot offer coverage')
        try:
            expiry = datetime.fromisoformat(coverage['valid_until'])
            if expiry.tzinfo is not None or expiry.isoformat() != coverage['valid_until']:
                raise ValueError()
        except ValueError:
            raise Refused('pilot offer coverage needs an explicit UTC expiry') from None
    if config['authorization_mode'] not in ('supervised', 'autonomous'):
        raise Refused('explicit pilot authorization mode is required')
    from .writer_context import CreatorProfile, profile_data
    try:
        profile = CreatorProfile.from_dict(config['creator_profile'])
    except (KeyError, ValueError, TypeError):
        raise Refused('invalid pilot creator profile') from None
    if profile.missing_inputs():
        raise Refused('pilot creator boundaries are required')
    fans = _ids(config['fans'])
    if len(fans) != 2:
        raise Refused('this pilot requires exactly two verified fan IDs')
    product, operator, model, limits = config['product'], config['operator'], config['model'], config['limits']
    qualification = config['qualification']
    _exact(qualification, ('mode', 'reference'))
    if (qualification['mode'] not in ('fixture', 'production') or type(qualification['reference']) is not str
            or not qualification['reference'].strip() or len(qualification['reference']) > 200):
        raise Refused('pilot qualification evidence is required')
    _exact(product, ('id', 'price_cents', 'media_ids', 'preview_ids'))
    item = _integer(product['id'], 1, 2**31-1)
    price = _integer(product['price_cents'], 300, 20000)
    media = _ids(product['media_ids'])
    preview = _ids(product['preview_ids'], empty=True)
    if price != 300 or len(media) != 1:
        raise Refused('this pilot requires one photo at USD 3.00')
    if not set(preview).issubset(media):
        raise Refused('pilot previews must belong to the product')
    operator_spec = validated_operator(operator, config['authorization_mode'])
    model_spec_ = validated_model(model)
    _exact(limits, ('send_units', 'daily_send_units', 'throttle_seconds', 'provider_requests',
                    'provider_credit_budget', 'provider_credits_per_request'))
    send_units = _integer(limits['send_units'], 1, 10000)
    daily_units = _integer(limits['daily_send_units'], 1, min(send_units, 20000))
    throttle = _integer(limits['throttle_seconds'], 1, 86400)
    requests = _integer(limits['provider_requests'], 1, 10**7)
    credits = _integer(limits['provider_credit_budget'], 1, 10**12)
    per_request = _integer(limits['provider_credits_per_request'], 1, credits)
    result = {'creator_profile':profile_data(profile), 'authorization_mode':config['authorization_mode'], 'qualification':{'mode':qualification['mode'], 'reference':qualification['reference']},
            'fans':fans, 'product':{'id':item, 'price_cents':price, 'media_ids':media, 'preview_ids':preview},
            'operator':operator_spec, 'model':model_spec_,
            'limits':{'send_units':send_units, 'daily_send_units':daily_units, 'throttle_seconds':throttle,
                      'provider_requests':requests, 'provider_credit_budget':credits,
                      'provider_credits_per_request':per_request}}
    if 'offer_coverage' in config:
        result['offer_coverage'] = dict(coverage)
    if 'delivery_mode' in config:
        result['delivery_mode'] = config['delivery_mode']
    return result


def current_offer_coverage(s, runtime, config):
    """The operator's existing VaultCatalogueCoverage, frozen in the existing pilot policy.

    No expiry/reference is inferred. Absent or expired approval stays unavailable;
    reading an expired policy remains possible for stop/recovery operations.
    """
    from .catalogue import VaultCatalogueCoverage, _catalogue_version
    if config.get('delivery_mode') == 'text_only':
        raise Refused('pilot_text_only')
    raw = config.get('offer_coverage')
    if raw is None:
        raise Refused('offer_coverage_missing')
    if raw['account_id'] != runtime.account_id or raw['binding_id'] != runtime.binding_id:
        raise Refused('offer_coverage_scope_changed')
    expiry = datetime.fromisoformat(raw['valid_until'])
    if expiry <= utcnow():
        raise Refused('offer_coverage_expired')
    # Legacy approvals retain whole-catalogue semantics; they are never silently reinterpreted.
    version = (_pilot_product_version(s, runtime, config['product'])
               if raw.get('snapshot_kind') == 'pilot-product.v1' else _catalogue_version(s, runtime))
    if raw['snapshot_hash'] != version:
        raise Refused('offer_coverage_stale')
    return VaultCatalogueCoverage(runtime.account_id, runtime.binding_id, True, raw['evidence_ref'], expiry, raw['snapshot_hash'])


def _pilot_product_version(s, runtime, product):
    """Only pilot dependencies; general catalogue coverage remains unchanged.

    updated_at remains a frozen version under catalog_terms. Even a sync-only
    timestamp refresh invalidates this version until explicitly reapproved.
    """
    from .commerce import _real_catalogue_terms
    from .catalogue import aliases_for
    if not _product_current(s, runtime.account_id, product):
        raise Refused('offer_coverage_product_changed')
    terms = _real_catalogue_terms(s, runtime.account_id, product['id'], lock=False)
    item = s.get(CatalogItem, product['id'])
    aliases = aliases_for(s, runtime, product['id'], terms)
    if len(aliases) > 1000:
        raise Refused('offer_coverage_alias_limit')
    provenance = [{'media_id': f['media_id'],
        'content_hash_kind': s.get(VaultItem, (runtime.account_id, f['media_id'])).content_hash_kind}
        for f in terms['files']]
    return sha256(json.dumps({'version': 'pilot-product.v1', 'binding': runtime.binding_id,
        'product': product, 'currency': 'USD', 'terms': terms, 'tags': item.tags,
        'aliases': aliases, 'media_provenance': provenance},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()).hexdigest()


def offer_coverage_snapshot(store, tenant, account, *, product, evidence_ref, valid_until):
    """Read a versioned snapshot for an explicit stopped configure command.

    This records or authorizes nothing. configure owns actor authorization,
    stopped/disabled requirements, revision checks and the retained audit.
    """
    if (type(evidence_ref) is not str or not evidence_ref.startswith('vault:')
            or not 6 < len(evidence_ref) <= 128 or type(valid_until) is not datetime
            or valid_until.tzinfo is not None or valid_until <= utcnow()):
        raise Refused('invalid pilot coverage evidence or expiry')
    with Session(store.engine) as s:
        runtime = store._runtime(s, tenant, account, lock=False)
        return {'account_id': account, 'binding_id': runtime.binding_id, 'complete': True,
            'evidence_ref': evidence_ref, 'valid_until': valid_until.isoformat(),
            'snapshot_kind': 'pilot-product.v1', 'snapshot_hash': _pilot_product_version(s, runtime, product)}


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _fingerprint(value):
    return sha256(_encoded(value).encode()).hexdigest()


def model_spec(config):
    return {'provider':config.provider, 'primary':config.primary_model, 'upstream':config.upstream_provider,
            'reported':config.upstream_reported_name, 'version':config.version(),
            'daily_cap_microusd':config.daily_cap_microusd, 'creator_cap_microusd':config.creator_cap_microusd}


def require_model(scope_config, account, config):
    if (not config.enabled or config.fallback_enabled or config.enabled_accounts != frozenset({account})
            or config.allowed_models != (config.primary_model,) or not config.upstream_provider
            or model_spec(config) != scope_config['model']):
        raise Refused('model does not match the durable pilot scope')


def planning_scope(s, runtime, fan_id, model, *, draft_only):
    """Check the same durable scope on every worker before and after model I/O."""
    row, config = current(s, runtime)
    if row is None:
        return None
    from .model_runtime import ModelRuntime
    if (type(model) is not ModelRuntime or fan_id not in config['fans']
            or row.status != ('configured' if draft_only else 'active')):
        raise Refused('pilot planning scope is unavailable')
    require_model(config, runtime.account_id, model.config)
    require_profile(config, model.profiles.get(runtime.account_id))
    client = model.client
    if config['qualification']['mode'] == 'fixture' and not getattr(client, 'fixture_transport', lambda: False)():
        raise Refused('fixture scope requires a simulated model transport')
    if (type(client).__module__ != 'model_transport.openrouter' or type(client).__name__ != 'OpenRouterClient'
            or getattr(client, '_upstream_provider', None) != config['model']['upstream']
            or getattr(client, '_price_caps', None) != {model.config.primary_model: model.config.prices_for(model.config.primary_model)}):
        raise Refused('model transport does not match the pilot scope')
    policy = s.get(OfDeliveryPolicy, runtime.account_id)
    if not policy_matches(policy, config, enabled=not draft_only):
        raise Refused('pilot delivery policy or product changed')
    return {'revision':row.revision, 'fingerprint':row.fingerprint, 'status':row.status,
            'creator_profile_hash':_fingerprint(config['creator_profile'])}


def require_profile(config, profile):
    from .writer_context import profile_data
    if profile_data(profile) != config['creator_profile']:
        raise Refused('creator profile does not match the durable pilot scope')


def _product_current(s, account, product):
    item = s.get(CatalogItem, product['id'])
    try:
        media = json.loads(item.media_ids)
        previews = json.loads(item.preview_media_ids)
        if type(media) is not list or type(previews) is not list:
            return False
        photo = s.get(VaultItem, (account, product['media_ids'][0]))
        return (item.account_id == account and item.enabled is True and item.script_id is None
                and item.kind in ('image', 'photo') and photo is not None and photo.kind in ('image', 'photo')
                and photo.removed_at is None and item.price_cents == product['price_cents']
                and tuple(map(str, media)) == tuple(map(str, product['media_ids']))
                and tuple(map(str, previews)) == tuple(map(str, product['preview_ids'])))
    except (AttributeError, TypeError, ValueError):
        return False


def current(s, runtime, *, lock=False):
    """This account's legacy or commercial scope. A preparation row (or none) is "no pilot", exactly as before."""
    from . import scope_contract as contracts
    row = contracts.row_for(s, runtime, lock=lock)
    if row is None:
        return None, None
    kind = contracts.claim(row)
    if kind not in (contracts.LEGACY, contracts.COMMERCIAL):
        return None, None
    return row, contracts.validated(row, runtime, kind)


def require(s, runtime, *, active=False, lock=False):
    row, config = current(s, runtime, lock=lock)
    if row is None or active and row.status != 'active':
        raise Refused('durable pilot scope is not active' if active else 'durable pilot scope is unavailable')
    return row, config


def authorize(config, actor, permission):
    if (actor != config['operator']['actor'] or permission not in config['operator']['permissions']):
        raise Refused('pilot operator permission required')


def authorize_fan(s, runtime, fan, actor, permission):
    row, config = current(s, runtime)
    if row is not None:
        if fan not in config['fans']:
            raise Refused('fan is outside the pilot scope')
        authorize(config, actor, permission)



def digest_resolution_key(binding, epoch, event_key, payload_sha256):
    event_hash = sha256(event_key.encode()).hexdigest()
    return sha256(f'plan-c-digest-resolve:{binding}:{epoch}:{event_hash}:{payload_sha256}'.encode()).hexdigest()


def queue_observation_key(binding, epoch, event_key, digest):
    return sha256(f'coexist-queue:{binding}:{epoch}:{event_key}:{digest}'.encode()).hexdigest()


def classify_queue_observation(s, runtime, row):
    """Audit an exact, non-conflicting queue notification under coexistence.

    The bytes and original hold stay immutable. A later variant makes every
    observation of that event unresolved again through the shared query.
    """
    from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt as Receipt, OfEnginePilotWebhookDigest as Row, OfFansWebhookConflict, OfFansWebhook
    from .coexistence import enabled
    from fans_api_adapter.queue_review import queue_envelope
    if not enabled(s, runtime):
        return False
    conn = s.get(OfFansConnection, (runtime.account_id, row.epoch))
    receipt = s.get(Receipt, (runtime.account_id, row.epoch, row.event_key, row.payload_sha256))
    if conn is None or conn.binding_id != runtime.binding_id or receipt is None:
        return False
    raw = bytes(receipt.raw_body)
    if sha256(raw).hexdigest() != row.payload_sha256:
        return False
    try:
        body, _ = queue_envelope(raw)
    except (Refused, ValueError, TypeError, KeyError):
        return False
    if body['account_id'] != conn.provider_account_id:
        return False
    if s.scalar(select(Row.payload_sha256).where(Row.account_id == runtime.account_id,
            Row.epoch == row.epoch, Row.event_key == row.event_key,
            Row.payload_sha256 != row.payload_sha256).limit(1)) is not None:
        return False
    if s.scalar(select(OfFansWebhookConflict.payload_sha256).where(
            OfFansWebhookConflict.account_id == runtime.account_id, OfFansWebhookConflict.epoch == row.epoch,
            OfFansWebhookConflict.event_key == row.event_key).limit(1)) is not None:
        return False
    original = s.get(OfFansWebhook, (runtime.account_id, row.epoch, row.event_key))
    if original is not None and original.payload_sha256 != row.payload_sha256:
        return False
    key = queue_observation_key(runtime.binding_id, row.epoch, row.event_key, row.payload_sha256)
    if s.get(OfLaunchCommand, (runtime.account_id, key)) is None:
        before = {'epoch':row.epoch, 'eventKey':row.event_key, 'payloadSha256':row.payload_sha256}
        result = {'classification':'coexistence_queue_notification', 'sendAuthorized':False}
        s.add(OfLaunchCommand(account_id=runtime.account_id, command_key=key, binding_id=runtime.binding_id,
            kind='plan_c_queue_observation', request_hash=sha256(raw).hexdigest(),
            actor='fans-webhook', reason='Exact retained queue notification under staff coexistence',
            before_json=json.dumps(before,sort_keys=True), result_json=json.dumps(result,sort_keys=True),
            created_at=utcnow()))
    return True


def classify_retained_queue_observations(s, runtime):
    """One-time paginated classification of pre-upgrade retained evidence."""
    from sqlalchemy import tuple_
    from db.onlyfans_engine_models import OfEnginePilotWebhookDigest as Row
    from .coexistence import enabled
    if not enabled(s, runtime):
        return
    after = None
    while True:
        query = unresolved_webhook_digests(runtime).order_by(Row.received_at, Row.epoch, Row.event_key, Row.payload_sha256)
        if after is not None:
            query = query.where(tuple_(Row.received_at, Row.epoch, Row.event_key, Row.payload_sha256) > after)
        rows = list(s.scalars(query.limit(_DIGEST_SCAN)))
        if not rows:
            break
        for row in rows:
            classify_queue_observation(s, runtime, row)
        s.flush()
        last = rows[-1]
        after = (last.received_at, last.epoch, last.event_key, last.payload_sha256)


def unresolved_webhook_digests(runtime):
    """One shared view for barriers, queue and readiness; original evidence never changes.

    A later conflicting payload re-holds even a previously resolved benign event.
    Legacy digest pairs missing a safety_hold flag are also protected.
    """
    from sqlalchemy import and_, or_, func, literal, cast, String
    from sqlalchemy.orm import aliased
    from db.onlyfans_engine_models import OfEnginePilotWebhookDigest
    row = OfEnginePilotWebhookDigest
    other = aliased(row)
    conflict = select(other.payload_sha256).where(other.account_id == row.account_id,
        other.epoch == row.epoch, other.event_key == row.event_key,
        other.payload_sha256 != row.payload_sha256).correlate(row).exists()
    key_text = (literal('plan-c-digest-resolve:' + runtime.binding_id + ':') + cast(row.epoch, String) + ':'
        + func.encode(func.sha256(func.convert_to(row.event_key, 'UTF8')), 'hex') + ':' + row.payload_sha256)
    key = func.encode(func.sha256(func.convert_to(key_text, 'UTF8')), 'hex')
    resolved = select(OfLaunchCommand.command_key).where(OfLaunchCommand.account_id == row.account_id,
        OfLaunchCommand.binding_id == runtime.binding_id, OfLaunchCommand.command_key == key,
        OfLaunchCommand.kind == 'plan_c_webhook_resolution').correlate(row).exists()
    queue_resolved = select(OfLaunchCommand.command_key).where(OfLaunchCommand.account_id == row.account_id,
        OfLaunchCommand.binding_id == runtime.binding_id, OfLaunchCommand.command_key == key,
        OfLaunchCommand.kind == 'plan_c_queue_digest_resolution').correlate(row).exists()
    # Every conflicting variant must have its own queue-review marker. A new
    # variant immediately invalidates the group's old disposition for all fans.
    other_key_text = (literal('plan-c-digest-resolve:' + runtime.binding_id + ':') + cast(other.epoch, String) + ':'
        + func.encode(func.sha256(func.convert_to(other.event_key, 'UTF8')), 'hex') + ':' + other.payload_sha256)
    other_key = func.encode(func.sha256(func.convert_to(other_key_text, 'UTF8')), 'hex')
    other_reviewed = select(OfLaunchCommand.command_key).where(OfLaunchCommand.account_id == other.account_id,
        OfLaunchCommand.binding_id == runtime.binding_id, OfLaunchCommand.command_key == other_key,
        OfLaunchCommand.kind == 'plan_c_queue_digest_resolution').correlate(other).exists()
    unreviewed_variant = select(other.payload_sha256).where(other.account_id == row.account_id,
        other.epoch == row.epoch, other.event_key == row.event_key, ~other_reviewed).correlate(row).exists()
    from sqlalchemy.dialects.postgresql import JSONB
    # Automatic classification is separate from a human review and valid only
    # while this exact scope still permits coexistence. Conflicts always re-hold.
    auto_text = (literal('coexist-queue:' + runtime.binding_id + ':') + cast(row.epoch, String) + ':'
        + row.event_key + ':' + row.payload_sha256)
    auto_key = func.encode(func.sha256(func.convert_to(auto_text, 'UTF8')), 'hex')
    observed = select(OfLaunchCommand.command_key).where(OfLaunchCommand.account_id == row.account_id,
        OfLaunchCommand.binding_id == runtime.binding_id, OfLaunchCommand.command_key == auto_key,
        OfLaunchCommand.kind == 'plan_c_queue_observation').correlate(row).exists()
    coexist_scope = select(OfEnginePilotScope.scope_id).where(
        OfEnginePilotScope.account_id == runtime.account_id,
        OfEnginePilotScope.binding_id == runtime.binding_id,
        OfEnginePilotScope.scope_id != 1,
        func.jsonb_extract_path_text(cast(OfEnginePilotScope.config_json, JSONB), 'contract_version') == 'commercial.v1',
        func.jsonb_extract_path_text(cast(OfEnginePilotScope.config_json, JSONB),
                                    'coexistence', 'mode') == 'coexist').exists()
    classified = observed & coexist_scope
    return select(row).join(OfFansConnection, and_(OfFansConnection.account_id == row.account_id,
        OfFansConnection.epoch == row.epoch)).where(row.account_id == runtime.account_id,
        OfFansConnection.binding_id == runtime.binding_id, or_(row.safety_hold.is_(True), conflict),
        or_(~resolved, conflict), or_(~queue_resolved, unreviewed_variant), or_(~classified, conflict))


def delivery_block(s, runtime, fan, *, commercial=False):
    """Pending human/provider evidence blocks pilot sends until safely classified."""
    pilot, config = current(s, runtime)
    if pilot is None:
        return None
    if commercial and config.get('delivery_mode') == 'text_only':
        return 'pilot_text_only'
    from .pilot_ingress import review_pending
    if review_pending(s, runtime, pilot, config, fan):
        return 'pilot_contact_review_pending'
    from db.onlyfans_fans_intake_models import OfFansEvidence, OfFansLink, OfFansProjection
    from db.onlyfans_engine_models import OfFansWebhook, OfEnginePilotWebhookDigest
    conn = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == runtime.account_id,
        OfFansConnection.binding_id == runtime.binding_id, OfFansConnection.status == 'active'))
    link = s.get(OfFansLink, (runtime.account_id, conn.provider_creator_id)) if conn is not None else None
    if conn is None or link is None or link.send_blocked or link.state != 'connected':
        return 'pilot_provider_link_blocked'
    from fans_api_adapter.event_scope import event_fan
    from sqlalchemy import or_
    sent_fan = event_fan(OfFansWebhook.event_type, OfFansWebhook.raw_body)
    unmapped_outside_deletion = None
    if trial_exception_covers_pilot(s, runtime):
        from db.models import Message
        from db.onlyfans_delivery_models import OfTextIntent
        from sqlalchemy import BigInteger, cast as sql_cast, and_ as sql_and, func
        from sqlalchemy.dialects.postgresql import JSONB
        deleted_id = sql_cast(func.jsonb_extract_path_text(
            sql_cast(func.convert_from(OfFansWebhook.raw_body, 'UTF8'), JSONB), 'payload', 'id'), BigInteger)
        # A held deletion of a message the pilot never had (no native row, intent or evidence) is outside-writer
        # activity on another chat. It stays held and recorded; it does not block the trial fans.
        unmapped_outside_deletion = sql_and(OfFansWebhook.event_type == 'messages.deleted',
            OfFansProjection.status == 'held', OfFansProjection.detail == 'deletion_mapping_missing_or_ambiguous',
            ~select(Message.message_id).where(Message.account_id == runtime.account_id,
                                              Message.message_id == deleted_id).exists(),
            ~select(OfTextIntent.intent_id).where(OfTextIntent.account_id == runtime.account_id,
                                                  OfTextIntent.provider_message_id == deleted_id).exists(),
            ~select(OfFansEvidence.evidence_id).where(OfFansEvidence.account_id == runtime.account_id,
                                                      OfFansEvidence.message_id == deleted_id).exists())
    pending = s.scalar(select(OfFansProjection.event_key).join(OfFansWebhook,
        (OfFansWebhook.account_id == OfFansProjection.account_id) &
        (OfFansWebhook.epoch == OfFansProjection.epoch) &
        (OfFansWebhook.event_key == OfFansProjection.event_key)).join(OfFansConnection,
        (OfFansConnection.account_id == OfFansWebhook.account_id) &
        (OfFansConnection.epoch == OfFansWebhook.epoch)).where(
        OfFansProjection.account_id == runtime.account_id, OfFansProjection.status.in_(('pending','held')),
        OfFansConnection.binding_id == runtime.binding_id,
        or_(OfFansWebhook.event_type.in_(('accounts.disconnected','accounts.session_expired','accounts.authentication_failed',
                                               'accounts.otp_code_required','accounts.face_otp_required',
                                               'messages.deleted','subscriptions.renewed','subscriptions.expired')),
            (OfFansWebhook.event_type.in_(('messages.sent','messages.received') +
                (('messages.ppv.unlocked','transactions.new','chargebacks.new') if commercial else ()))) &
            or_(sent_fan == str(fan), sent_fan.is_(None))),
        *(() if unmapped_outside_deletion is None else (~unmapped_outside_deletion,))).limit(1))
    excepted = trial_takeover_exception(s, runtime.account_id, runtime.binding_id, fan)
    human_query = select(OfFansEvidence.evidence_id).where(
        OfFansEvidence.account_id == runtime.account_id, OfFansEvidence.fan_id == fan,
        OfFansEvidence.kind == 'human_sent', OfFansEvidence.status.in_(('pending','held')))
    if excepted:
        # A queued outside broadcast stays held as evidence but does not block this test fan.
        human_query = human_query.where(or_(OfFansEvidence.reason.is_(None),
                                            OfFansEvidence.reason != 'queued_send_to_engine_owned_fan'))
    human = s.scalar(human_query.limit(1))
    money = s.scalar(select(OfFansEvidence.evidence_id).where(
        OfFansEvidence.account_id == runtime.account_id, OfFansEvidence.fan_id == fan,
        OfFansEvidence.kind.in_(('unlock','transaction','chargeback')),
        OfFansEvidence.status.in_(('pending','held'))).limit(1)) if commercial else None
    # Step 5.2: conflicting money evidence for a tip holds THIS fan only until a person dismisses the held row (never automatic).
    tip_hold = s.scalar(select(OfFansEvidence.evidence_id).where(OfFansEvidence.account_id == runtime.account_id,
        OfFansEvidence.fan_id == fan, OfFansEvidence.kind == 'tip', OfFansEvidence.status == 'held').limit(1))
    blocked = (pending is not None or human is not None or money is not None or tip_hold is not None
               or digest_blocks_fan(s, runtime, fan))
    return 'pilot_evidence_pending' if blocked else None


_DIGEST_SCAN = 500


def digest_hold_scope(s, runtime):
    """(account_wide, fans): what the unresolved digest holds cover.

    A held digest covers the whole account unless its retained exact bytes prove it is a MATERIAL
    event for one fan, signed for this connection's provider account, with no conflicting
    payload under the same key; that digest then holds only that fan. Missing bytes,
    unknown/malformed/irrelevant-but-held evidence, conflicts and a scan larger than the bound
    all hold the account (never the safe direction).
    """
    from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt as Receipt, OfEnginePilotWebhookDigest as Row
    from fans_api_adapter.event_scope import classify_raw, MATERIAL
    classify_retained_queue_observations(s, runtime)
    keys = s.execute(unresolved_webhook_digests(runtime).with_only_columns(
        Row.epoch, Row.event_key, Row.payload_sha256).order_by(Row.received_at, Row.event_key,
        Row.payload_sha256).limit(_DIGEST_SCAN + 1)).all()
    if len(keys) > _DIGEST_SCAN:
        return True, frozenset()
    excepted = trial_exception_covers_pilot(s, runtime)
    from .coexistence import enabled as coexistence_enabled
    coexist = coexistence_enabled(s, runtime)
    fans = set()
    for epoch, event_key, digest in keys:
        receipt = s.get(Receipt, (runtime.account_id, epoch, event_key, digest))
        connection = s.get(OfFansConnection, (runtime.account_id, epoch))
        if receipt is None or connection is None or sha256(bytes(receipt.raw_body)).hexdigest() != digest:
            return True, frozenset()
        if excepted:
            # Owner trial exception: a documented chat_queue notification for THIS provider account, with retained
            # exact bytes, is outside-writer activity. It stays recorded and unresolved but does not hold sending.
            try:
                from fans_api_adapter.queue_review import queue_envelope
                body, _ = queue_envelope(bytes(receipt.raw_body))
                if body['account_id'] == connection.provider_account_id:
                    continue
            except (Refused, ValueError, TypeError, KeyError):
                pass
        if coexist:
            # Step 5.1: a documented chat_queue notification for THIS provider account is outside activity on a coexistence account.
            # Exact documented shape only, exact retained bytes only, and never when any other payload exists under the same key
            # (a conflicting variant stays held). Everything else (unknown, malformed, other accounts) falls through to the hold.
            try:
                from fans_api_adapter.queue_review import queue_envelope
                body, _ = queue_envelope(bytes(receipt.raw_body))
                conflicting = s.scalar(select(Row.payload_sha256).where(Row.account_id == runtime.account_id, Row.epoch == epoch,
                    Row.event_key == event_key, Row.payload_sha256 != digest).limit(1))
                if body['account_id'] == connection.provider_account_id and conflicting is None:
                    continue
            except (Refused, ValueError, TypeError, KeyError):
                pass
        verdict = classify_raw(bytes(receipt.raw_body))
        if verdict.cls != MATERIAL or verdict.fan is None:
            return True, frozenset()
        try:
            from fans_api_adapter.events import _parse
            if _parse(bytes(receipt.raw_body))['account_id'] != connection.provider_account_id:
                return True, frozenset()
        except Refused:
            return True, frozenset()
        if s.scalar(select(Row.payload_sha256).where(Row.account_id == runtime.account_id, Row.epoch == epoch,
                Row.event_key == event_key, Row.payload_sha256 != digest).limit(1)) is not None:
            return True, frozenset()
        fans.add(verdict.fan)
    return False, frozenset(fans)


def digest_blocks_fan(s, runtime, fan):
    """True when an unresolved digest hold covers this fan (see digest_hold_scope)."""
    account_wide, fans = digest_hold_scope(s, runtime)
    return account_wide or fan in fans


def planning_block(s, runtime, fan):
    """Known send-blocking holds stop new model calls; resolved work resumes through normal processing."""
    row, _ = current(s, runtime)
    if row is None or row.status != 'active':
        return None
    return delivery_block(s, runtime, fan)


def policy_matches(policy, config, *, enabled):
    limits = config['limits']
    return bool(policy is not None and policy.transport_kind == 'fansapi'
                and policy.enabled is enabled and policy.media_enabled is (config.get('delivery_mode') != 'text_only')
                and 0 < policy.limit_units <= limits['send_units']
                and policy.daily_limit_units is not None and 0 < policy.daily_limit_units <= limits['daily_send_units']
                and policy.throttle_seconds >= limits['throttle_seconds'])


def _command(key, actor, reason):
    if (type(key) is not str or not _KEY.fullmatch(key) or type(actor) is not str or not _ACTOR.fullmatch(actor)
            or type(reason) is not str or not reason.strip() or len(reason) > 512):
        raise Refused('invalid pilot configuration command')


def _prior(s, account, key, request_hash):
    prior = s.get(OfLaunchCommand, (account, key))
    if prior is None:
        return None
    if prior.kind != 'plan_c_scope' or prior.request_hash != request_hash:
        raise Refused('pilot command key conflict')
    return json.loads(prior.result_json)


def _audit(s, runtime, key, actor, reason, request_hash, before, result):
    s.add(OfLaunchCommand(account_id=runtime.account_id, command_key=key, binding_id=runtime.binding_id,
                          kind='plan_c_scope', request_hash=request_hash, actor=actor, reason=reason,
                          before_json=_encoded(before), result_json=_encoded(result), created_at=utcnow()))


def configure(store, tenant, account, config, *, expected_revision, command_key, actor, reason):
    _command(command_key, actor, reason)
    if type(expected_revision) is not int or expected_revision < 0:
        raise Refused('invalid pilot revision')
    from . import scope_contract as contracts
    commercial = contracts.is_commercial(config)
    config = contracts.validate_commercial(config) if commercial else validate(config)
    request_hash = _fingerprint(['configure', account, config, expected_revision, actor, reason])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, account, command_key, request_hash)
        if prior is not None:
            return {**prior, 'replayed':True}
        row = s.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.account_id == account).with_for_update())
        if row is None:
            if s.scalar(select(OfEnginePilotScope.scope_id).where(OfEnginePilotScope.scope_id == 1).with_for_update()) is not None:
                raise Refused('the pilot is bound to another creator')
        elif row.scope_id != 1:
            # A preparation row is promoted, never configured; a commercial row takes only commercial configuration.
            if contracts.claim(row) != contracts.COMMERCIAL:
                raise Refused('promote the preparation scope through the commercial command first')
            if not commercial:
                raise Refused('a commercial scope takes only commercial.v1 configuration')
            return _configure_commercial(s, runtime, row, config, expected_revision=expected_revision, command_key=command_key,
                                         actor=actor, reason=reason, request_hash=request_hash)
        elif row.binding_id != runtime.binding_id:
            raise Refused('the pilot is bound to another creator')
        if commercial:
            raise Refused('a commercial scope exists only by promoting a preparation scope')
        if row is not None:
            _, old = current(s, runtime)
            authorize(old, actor, 'configure')
        else:
            authorize(config, actor, 'configure')
        if (row.revision if row else 0) != expected_revision:
            raise Refused('pilot revision conflict')
        policy = s.get(OfDeliveryPolicy, account)
        if policy is None or policy.enabled is not False or policy.transport_kind != 'fansapi':
            raise Refused('disable the fansapi policy before configuring the pilot')
        if config.get('delivery_mode') == 'text_only' and policy.media_enabled:
            raise Refused('disable media before configuring text-only pilot')
        if row is not None and row.status == 'active':
            raise Refused('stop the pilot before reconfiguring it')
        if row is not None:
            removed = set(old['fans']) - set(config['fans'])
            if removed:
                from db.onlyfans_delivery_models import OfTextIntent
                from db.onlyfans_engine_models import OfEngineIndependentAdmission
                unresolved = s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
                    OfTextIntent.binding_id == runtime.binding_id, OfTextIntent.fan_id.in_(removed),
                    OfTextIntent.status.in_(('dispatching', 'unknown'))).limit(1))
                independent = s.scalar(select(OfEngineIndependentAdmission.attempt_id).where(
                    OfEngineIndependentAdmission.account_id == account, OfEngineIndependentAdmission.fan_id.in_(removed),
                    OfEngineIndependentAdmission.status.in_(('dispatching', 'unknown'))).limit(1))
                if unresolved is not None or independent is not None:
                    raise Refused('resolve retained fan delivery before removing the fan')
        # Text-only mode sells and sends nothing, so the unused product is not a prerequisite (the binding, creator profile,
        # permissions and the commercial/media prohibition are still required). Full mode still needs the verified product.
        if config.get('delivery_mode') != 'text_only' and not _product_current(s, account, config['product']):
            raise Refused('pilot product or price is unavailable')
        if 'offer_coverage' in config and config.get('delivery_mode') != 'text_only':
            current_offer_coverage(s, runtime, config)
        if row is not None and (row.provider_requests_used > config['limits']['provider_requests']
                or row.provider_credits_reserved > config['limits']['provider_credit_budget']):
            raise Refused('new limits cannot erase existing reservations')
        now = utcnow()
        before = {'revision':row.revision, 'status':row.status, 'fingerprint':row.fingerprint} if row else {}
        # Retain exact explicit coverage changes for later expiry renewal. Status
        # commands/unrelated configuration cannot manufacture this evidence.
        before['offerCoverageBefore'] = old.get('offer_coverage') if row else None
        before['offerCoverageAfter'] = config.get('offer_coverage')
        encoded, digest = _encoded(config), _fingerprint(config)
        if row is None:
            row = OfEnginePilotScope(scope_id=1, account_id=account, binding_id=runtime.binding_id, revision=1,
                status='configured', config_json=encoded, fingerprint=digest, provider_requests_used=0,
                provider_credits_reserved=0, created_at=now, updated_at=now)
            s.add(row)
        elif row.config_json != encoded or row.status != 'configured':
            row.revision += 1
            row.status, row.active_since, row.config_json, row.fingerprint, row.updated_at = 'configured', None, encoded, digest, now
        result = {'accountId':account, 'revision':row.revision, 'status':row.status, 'fingerprint':row.fingerprint}
        _audit(s, runtime, command_key, actor, reason, request_hash, before, result)
        return {**result, 'replayed':False}


def _fans_removable(s, runtime, removed):
    if not removed:
        return
    from db.onlyfans_delivery_models import OfTextIntent
    from db.onlyfans_engine_models import OfEngineIndependentAdmission
    unresolved = s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == runtime.account_id,
        OfTextIntent.binding_id == runtime.binding_id, OfTextIntent.fan_id.in_(removed),
        OfTextIntent.status.in_(('dispatching', 'unknown'))).limit(1))
    independent = s.scalar(select(OfEngineIndependentAdmission.attempt_id).where(
        OfEngineIndependentAdmission.account_id == runtime.account_id, OfEngineIndependentAdmission.fan_id.in_(removed),
        OfEngineIndependentAdmission.status.in_(('dispatching', 'unknown'))).limit(1))
    if unresolved is not None or independent is not None:
        raise Refused('resolve retained fan delivery before removing the fan')


def _configure_commercial(s, runtime, row, config, *, expected_revision, command_key, actor, reason, request_hash):
    """The named operator's edit of a promoted scope's audience, profile, model, limits and mode. Never touches identity."""
    from . import scope_contract as contracts
    from .commercial_lifecycle import check_catalogue
    account = runtime.account_id
    _, old = current(s, runtime)
    authorize(old, actor, 'configure')
    if row.revision != expected_revision:
        raise Refused('pilot revision conflict')
    if row.status == 'active':
        raise Refused('stop the pilot before reconfiguring it')
    if any(config[name] != old[name] for name in contracts.IDENTITY_KEYS):
        raise Refused('commercial identity and lineage are retained')
    policy = s.get(OfDeliveryPolicy, account)
    if policy is None or policy.enabled is not False or policy.transport_kind != 'fansapi':
        raise Refused('disable the fansapi policy before configuring the pilot')
    if config['delivery_mode'] == 'text_only' and policy.media_enabled:
        raise Refused('disable media before configuring text-only pilot')
    _fans_removable(s, runtime, set(old['fans']) - set(config['fans']))
    check_catalogue(runtime, config)
    if (row.provider_requests_used > config['limits']['provider_requests']
            or row.provider_credits_reserved > config['limits']['provider_credit_budget']):
        raise Refused('new limits cannot erase existing reservations')
    now = utcnow()
    before = {'revision': row.revision, 'status': row.status, 'fingerprint': row.fingerprint,
              'catalogueBefore': old.get('catalogue'), 'catalogueAfter': config.get('catalogue')}
    encoded, digest = _encoded(config), _fingerprint(config)
    if row.config_json != encoded or row.status != 'configured':
        row.revision += 1
        row.status, row.active_since, row.config_json, row.fingerprint, row.updated_at = 'configured', None, encoded, digest, now
    result = {'accountId': account, 'revision': row.revision, 'status': row.status, 'fingerprint': row.fingerprint}
    _audit(s, runtime, command_key, actor, reason, request_hash, before, result)
    return {**result, 'replayed': False}


def _activate_text_only(s, store, runtime, row, config, *, actor, reason):
    """Preserve the existing text-only activation boundary."""
    return _activate_fansapi_scope(s, store, runtime, row, config, actor=actor, reason=reason, media_enabled=False)


def _activate_fansapi_scope(s, store, runtime, row, config, *, actor, reason, media_enabled):
    """Enable a verified stopped scope atomically; paid activation also needs the current commercial catalogue."""
    from .scope_contract import is_commercial
    if media_enabled and (not is_commercial(config) or config.get('delivery_mode') != 'full'):
        raise Refused('paid activation requires a commercial full scope')
    activation_name = 'commercial' if media_enabled else 'text-only'
    from db.models import Fan, OfConversationState
    from db.onlyfans_models import OfIngress, OfRecovery
    from db.onlyfans_delivery_models import OfTextIntent
    from db.onlyfans_engine_models import OfEngineIndependentAdmission
    from db.onlyfans_fans_intake_models import OfFansLink
    from onlyfans_runtime.text_delivery import TextAuthority
    from .controls import reason as hold_reason

    if runtime.enabled or row.status not in ('configured', 'stopped'):
        raise Refused(activation_name + ' activation requires stopped runtime')
    policy = s.scalar(select(OfDeliveryPolicy).where(
        OfDeliveryPolicy.account_id == runtime.account_id).with_for_update())
    if (policy is None or policy.enabled or policy.transport_kind != 'fansapi'
            or not media_enabled and policy.media_enabled):
        raise Refused(activation_name + ' activation requires disabled fansapi policy')
    limits = config['limits']
    send_limit = min(policy.limit_units, limits['send_units']) if policy.limit_units else limits['send_units']
    daily_limit = (min(policy.daily_limit_units, limits['daily_send_units'])
                   if policy.daily_limit_units is not None else limits['daily_send_units'])
    throttle = max(policy.throttle_seconds, limits['throttle_seconds'])
    if (policy.active_intent_id or policy.queued_count or policy.used_units >= send_limit
            or not policy.limit_units and policy.used_units or daily_limit <= 0):
        raise Refused('delivery allowance or queue unavailable')
    unresolved = s.scalar(select(OfTextIntent.intent_id).where(
        OfTextIntent.account_id == runtime.account_id,
        OfTextIntent.status.in_(('queued', 'dispatching', 'unknown'))).limit(1))
    independent = s.scalar(select(OfEngineIndependentAdmission.attempt_id).where(
        OfEngineIndependentAdmission.account_id == runtime.account_id,
        OfEngineIndependentAdmission.status.in_(('dispatching', 'unknown'))).limit(1))
    if unresolved is not None or independent is not None:
        raise Refused('unresolved delivery holds activation')
    conn = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == runtime.account_id,
        OfFansConnection.binding_id == runtime.binding_id, OfFansConnection.status == 'active').with_for_update())
    link = s.get(OfFansLink, (runtime.account_id, conn.provider_creator_id)) if conn else None
    preparation_proof = None
    if (conn is not None and link is not None and link.send_blocked
            and link.block_reason == 'preparation_only'
            and config.get('contract_version') == 'commercial.v1'):
        # A named operator may end preparation for this exact verified binding.
        # Authentication/reconnect holds are never released here. The enclosing
        # activation transaction rolls this change back if any later check fails.
        proofs = s.scalars(select(OfLaunchCommand).where(
            OfLaunchCommand.account_id == runtime.account_id,
            OfLaunchCommand.binding_id == runtime.binding_id,
            OfLaunchCommand.kind == 'preparation_connect')).all()
        for proof in proofs:
            try:
                saved = json.loads(proof.result_json)
            except (ValueError, TypeError):
                continue
            if (type(saved) is dict and saved.get('identityVerified') is True
                    and type(saved.get('epoch')) is int and saved.get('epoch') == conn.epoch
                    and saved.get('provider_account') == conn.provider_account_id
                    and saved.get('creator_id') == conn.provider_creator_id):
                preparation_proof = proof.command_key
                break
        if preparation_proof is not None:
            link.send_blocked, link.block_reason = False, None
            link.updated_at = utcnow()
    if (conn is None or conn.epoch < 1 or link is None or link.state != 'connected' or link.send_blocked
            or link.provider_account_id != conn.provider_account_id):
        raise Refused('verified pilot connection unavailable')
    fan_recovery = []
    for fan_id in config['fans']:
        rec = s.get(OfRecovery, (runtime.account_id, fan_id))
        fan = s.get(Fan, (runtime.account_id, fan_id))
        state = s.get(OfConversationState, (runtime.account_id, fan_id))
        pending = s.scalar(select(OfIngress.event_key).where(OfIngress.account_id == runtime.account_id,
            OfIngress.fan_id == fan_id, OfIngress.status.in_(('pending', 'quarantined'))).limit(1))
        if (rec is None or rec.status != 'complete' or rec.last_complete_at is None
                or rec.last_complete_at < conn.created_at or pending is not None):
            raise Refused('pilot fan history unavailable')
        if state is None or fan is None:
            raise Refused('pilot conversation unavailable')
        fan_recovery.append({'fan': fan_id, 'completedAt': rec.last_complete_at.isoformat(),
                             'controlRevision': state.revision})
        if (hold_reason(s, runtime.account_id, fan_id, state, fan,
                        unresolved=store._unresolved(s, runtime.account_id, fan_id, state)) is not None
                or delivery_block(s, runtime, fan_id) is not None):
            raise Refused('pilot fan control or evidence holds activation')
    if (row.provider_requests_used >= config['limits']['provider_requests']
            or row.provider_credits_reserved + config['limits']['provider_credits_per_request']
            > config['limits']['provider_credit_budget']):
        raise Refused('pilot provider budget is exhausted')
    previous_policy_revision = policy.revision
    TextAuthority(store)._configure_in_session(s, runtime, expected_revision=policy.revision,
        enabled=True, limit_units=send_limit, daily_limit_units=daily_limit,
        throttle_seconds=throttle, media_enabled=media_enabled, transport_kind='fansapi',
        actor=actor, reason=reason)
    runtime.enabled = True
    return {'runtimeEnabledBefore': False, 'policyRevisionBefore': previous_policy_revision,
            'policyRevisionAfter': policy.revision, 'connectionEpoch': conn.epoch,
            'preparationConnectionProof': preparation_proof,
            'providerAccountId': conn.provider_account_id, 'providerCreatorId': conn.provider_creator_id,
            'fanRecovery': fan_recovery}


def set_status(store, tenant, account, status, *, expected_revision, command_key, actor, reason):
    _command(command_key, actor, reason)
    if status not in ('active', 'stopped') or type(expected_revision) is not int or expected_revision < 1:
        raise Refused('invalid pilot status command')
    request_hash = _fingerprint(['status', account, status, expected_revision, actor, reason])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, account, command_key, request_hash)
        if prior is not None:
            return {**prior, 'replayed':True}
        row, config = require(s, runtime, lock=True)
        authorize(config, actor, 'activate' if status == 'active' else 'stop')
        if row.revision != expected_revision:
            raise Refused('pilot revision conflict')
        from . import scope_contract as contracts
        commercial = contracts.is_commercial(config)
        activation_evidence = None
        if status == 'active':
            if config['qualification']['mode'] == 'fixture' and os.environ.get('TWIN_PLAN_C_FIXTURE_ONLY') != '1':
                raise Refused('fixture scope cannot activate outside an isolated test')
            if commercial:
                from .commercial_lifecycle import assert_launchable
                assert_launchable(s, runtime, row, config)
            policy = s.get(OfDeliveryPolicy, account)
            connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                                                                  OfFansConnection.status == 'active'))
            if ((not commercial and config.get('delivery_mode') != 'text_only' and not _product_current(s, account, config['product']))
                    or connection is None or connection.binding_id != runtime.binding_id):
                raise Refused('pilot activation prerequisites are not met')
            if config.get('delivery_mode') == 'text_only':
                activation_evidence = _activate_text_only(s, store, runtime, row, config, actor=actor, reason=reason)
                policy = s.get(OfDeliveryPolicy, account)
            elif commercial and not runtime.enabled:
                activation_evidence = _activate_fansapi_scope(s, store, runtime, row, config,
                    actor=actor, reason=reason, media_enabled=True)
                policy = s.get(OfDeliveryPolicy, account)
            if not runtime.enabled or not policy_matches(policy, config, enabled=True):
                raise Refused('pilot activation prerequisites are not met')
            if (row.provider_requests_used >= config['limits']['provider_requests']
                    or row.provider_credits_reserved + config['limits']['provider_credits_per_request']
                    > config['limits']['provider_credit_budget']):
                raise Refused('pilot provider budget is exhausted')
        if status == 'stopped' and (config.get('delivery_mode') == 'text_only' or commercial):
            from onlyfans_runtime.launch_operations import stop_runtime_in_session
            from onlyfans_runtime.text_delivery import TextAuthority
            policy = s.scalar(select(OfDeliveryPolicy).where(
                OfDeliveryPolicy.account_id == account).with_for_update())
            if policy is None:
                raise Refused('pilot delivery policy unavailable')
            stop_runtime_in_session(s, runtime, command_key=command_key, actor=actor,
                                    request_hash=request_hash, now=utcnow())
            # A commercial scope that was never launched has no enabled policy to close (and its prepared policy values
            # need not satisfy the scope's limits); the legacy pilot keeps its unconditional policy write.
            if not commercial or policy.enabled:
                TextAuthority(store)._configure_in_session(s, runtime,
                    expected_revision=policy.revision, enabled=False, limit_units=policy.limit_units,
                    throttle_seconds=policy.throttle_seconds, media_enabled=False,
                    actor=actor, reason=reason)
        before = {'revision':row.revision, 'status':row.status, 'fingerprint':row.fingerprint}
        if activation_evidence is not None:
            before['textActivation'] = activation_evidence
        if row.status != status:
            row.revision += 1
            row.status, row.updated_at = status, utcnow()
            row.active_since = row.updated_at if status == 'active' else None
        result = {'accountId':account, 'revision':row.revision, 'status':row.status, 'fingerprint':row.fingerprint}
        _audit(s, runtime, command_key, actor, reason, request_hash, before, result)
        return {**result, 'replayed':False}


def describe(store, tenant, account, *, actor=None, require_read=False):
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account, lock=require_read)
        row, config = current(s, runtime)
        if require_read and row is not None:
            authorize(config, actor, 'read')
        if row is None:
            return {'accountId':account, 'status':'unconfigured', 'revision':0, 'config':None}
        shown = {'accountId':account, 'status':row.status, 'revision':row.revision, 'fingerprint':row.fingerprint,
                'activeSince':row.active_since.isoformat() + 'Z' if row.active_since else None, 'config':config, 'providerRequestsUsed':row.provider_requests_used,
                'providerCreditsReserved':row.provider_credits_reserved}
        if config.get('contract_version') == 'commercial.v1':
            shown['contractVersion'] = 'commercial.v1'
        return shown


def reserve_provider_request(store, tenant, account, *, expected_revision):
    """Conservatively reserve the configured worst-case credits before one HTTP attempt. Never refund uncertainty."""
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        row, config = require(s, runtime, active=True, lock=True)
        if row.revision != expected_revision:
            raise Refused('pilot scope changed before provider request')
        limits = config['limits']
        if (row.provider_requests_used >= limits['provider_requests']
                or row.provider_credits_reserved + limits['provider_credits_per_request'] > limits['provider_credit_budget']):
            raise Refused('pilot provider request budget exhausted')
        row.provider_requests_used += 1
        row.provider_credits_reserved += limits['provider_credits_per_request']
        row.updated_at = utcnow()
        return row.provider_requests_used


class ProviderRequestGuard:
    """Account- and revision-bound provider budget guard used by every pilot HTTP request."""
    def __init__(self, store, tenant, account, revision):
        self.store, self.tenant, self.account, self.revision = store, tenant, account, revision

    def __call__(self, method, url, mock_wire=False):
        if method not in ('GET', 'POST') or type(url) is not str:
            raise Refused('invalid pilot provider request')
        with Session(self.store.engine) as s, s.begin():
            runtime = self.store._runtime(s, self.tenant, self.account)
            scope, config = require(s, runtime, active=True)
            if scope.revision != self.revision:
                raise Refused('provider guard revision changed')
            if config['qualification']['mode'] == 'fixture':
                if mock_wire is not True:
                    raise Refused('fixture scope requires a simulated provider transport')
        reserve_provider_request(self.store, self.tenant, self.account, expected_revision=self.revision)


def authorize_stopped_backfill(store, tenant, account, *, expected_revision, actor, reason,
                               command_key, start_date, request_limit, base):
    """Record one bounded read-only preparation authorization before provider I/O."""
    from sqlalchemy import func
    from fans_api_adapter.history import ALLOWED_BASES
    _command(command_key, actor, reason)
    if (type(expected_revision) is not int or expected_revision < 1
            or type(start_date) is not datetime or start_date.tzinfo is not None
            or start_date.year < 2000 or start_date > utcnow()
            or type(request_limit) is not int or not 2 <= request_limit <= 201
            or base not in ALLOWED_BASES):
        raise Refused('invalid stopped backfill authorization')
    request_hash = _fingerprint(['stopped_backfill', tenant, account, expected_revision,
                                 actor, reason, start_date.isoformat(), request_limit, base])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        row, config = require(s, runtime, lock=True)
        authorize(config, actor, 'reconcile')
        policy = s.get(OfDeliveryPolicy, account)
        if (row.status != 'stopped' or row.revision != expected_revision
                or runtime.enabled or policy is None or policy.enabled):
            raise Refused('stopped backfill requires disabled runtime and policy')
        connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
            OfFansConnection.binding_id == runtime.binding_id, OfFansConnection.status == 'active'))
        if connection is None:
            raise Refused('stopped backfill requires an active provider binding')
        prior = s.get(OfLaunchCommand, (account, command_key))
        if prior is not None:
            if prior.kind != 'plan_c_backfill' or prior.request_hash != request_hash:
                raise Refused('pilot command key conflict')
            saved = json.loads(prior.result_json)
        else:
            count = s.scalar(select(func.count()).select_from(OfLaunchCommand).where(
                OfLaunchCommand.account_id == account, OfLaunchCommand.kind.in_(('plan_c_scope', 'account_control'))))
            saved = {'binding':runtime.binding_id, 'revision':row.revision, 'fans':config['fans'],
                'provider_account':connection.provider_account_id, 'creator':connection.provider_creator_id,
                'epoch':connection.epoch, 'base':base, 'start':start_date.isoformat(),
                'request_limit':request_limit, 'requests_before':row.provider_requests_used,
                'scope_commands':count, 'policy_revision':policy.revision, 'product':product_pin(config)}
            s.add(OfLaunchCommand(account_id=account, command_key=command_key, binding_id=runtime.binding_id,
                kind='plan_c_backfill', request_hash=request_hash, actor=actor, reason=reason,
                before_json=_encoded({'status':row.status, 'revision':row.revision,
                                      'requests':row.provider_requests_used}),
                result_json=_encoded(saved), created_at=utcnow()))
    return StoppedBackfillGuard(store, tenant, account, command_key, actor, saved)


class StoppedBackfillGuard:
    """Only identity and scoped history/financial GETs; never authorizes a send."""
    def __init__(self, store, tenant, account, command_key, actor, evidence):
        self.store, self.tenant, self.account = store, tenant, account
        self.command_key, self.actor, self.evidence = command_key, actor, evidence
        self.revision = evidence['revision']

    def _check(self, s, *, mock_wire):
        from sqlalchemy import func
        runtime = self.store._runtime(s, self.tenant, self.account)
        row, config = require(s, runtime, lock=True)
        authorize(config, self.actor, 'reconcile')
        policy = s.get(OfDeliveryPolicy, self.account)
        receipt = s.get(OfLaunchCommand, (self.account, self.command_key))
        count = s.scalar(select(func.count()).select_from(OfLaunchCommand).where(
            OfLaunchCommand.account_id == self.account, OfLaunchCommand.kind.in_(('plan_c_scope', 'account_control'))))
        conn = s.get(OfFansConnection, (self.account, self.evidence['epoch']))
        if (receipt is None or receipt.kind != 'plan_c_backfill' or receipt.actor != self.actor
                or receipt.result_json != _encoded(self.evidence)
                or receipt.binding_id != runtime.binding_id or row.binding_id != self.evidence['binding']
                or row.status != 'stopped' or row.revision != self.revision or runtime.enabled
                or policy is None or policy.enabled or policy.revision != self.evidence['policy_revision']
                or count != self.evidence['scope_commands']
                or config['fans'] != self.evidence['fans'] or conn is None or conn.status != 'active'
                or conn.binding_id != runtime.binding_id or conn.provider_account_id != self.evidence['provider_account']
                or conn.provider_creator_id != self.evidence['creator']):
            raise Refused('stopped backfill authorization changed')
        if config['qualification']['mode'] == 'fixture' and mock_wire is not True:
            raise Refused('fixture scope requires a simulated provider transport')
        return row, config

    def check_run(self, client, *, reason, start_date, pages_budget, chats):
        if (reason != 'operator' or start_date != datetime.fromisoformat(self.evidence['start'])
                or tuple(map(str, chats)) != tuple(map(str, self.evidence['fans']))
                or type(pages_budget) is not int or pages_budget > self.evidence['request_limit'] - 1
                or client.account != self.evidence['provider_account']
                or client.creator_id != self.evidence['creator'] or client.epoch != self.evidence['epoch']
                or client.base != self.evidence['base']):
            raise Refused('backfill does not match stopped authorization')

    def __call__(self, method, url, mock_wire=False):
        from urllib.parse import urlsplit
        if method != 'GET' or type(url) is not str:
            raise Refused('stopped backfill permits only GET')
        parts = urlsplit(url)
        origin = urlsplit(self.evidence['base'])
        paths = {'/api/accounts', '/api/' + self.evidence['provider_account'] + '/transactions',
                 '/api/' + self.evidence['provider_account'] + '/chargebacks',
                 '/api/' + self.evidence['provider_account'] + '/mass-messaging'}
        paths.update('/api/' + self.evidence['provider_account'] + '/chats/' + str(fan) + '/messages'
                     for fan in self.evidence['fans'])
        if (parts.scheme != origin.scheme or parts.netloc != origin.netloc
                or parts.path not in paths or parts.fragment or parts.username or parts.password):
            raise Refused('provider request outside stopped backfill scope')
        if parts.path.endswith('/mass-messaging') and parts.query:
            raise Refused('queue preparation requires the exact documented list endpoint')
        with Session(self.store.engine) as s, s.begin():
            row, config = self._check(s, mock_wire=mock_wire)
            limits = config['limits']
            if (row.provider_requests_used - self.evidence['requests_before'] >= self.evidence['request_limit']
                    or row.provider_requests_used >= limits['provider_requests']
                    or row.provider_credits_reserved + limits['provider_credits_per_request'] > limits['provider_credit_budget']):
                raise Refused('pilot provider request budget exhausted')
            row.provider_requests_used += 1
            row.provider_credits_reserved += limits['provider_credits_per_request']
            row.updated_at = utcnow()


# ---------------------------------------------------------------- temporary trial exception (owner instruction)
TRIAL_TAKEOVER_EXCEPTION = 'plan_c_trial_takeover_exc'
TRIAL_TAKEOVER_EXCEPTION_END = 'plan_c_trial_takeover_exc_end'


def _trial_exception_keys(binding):
    start = sha256(f'plan-c-trial-takeover-exception:{binding}'.encode()).hexdigest()
    return start, sha256(f'plan-c-trial-takeover-exception-end:{binding}'.encode()).hexdigest()


def trial_takeover_exception(s, account, binding, fan):
    """True while the owner's recorded, unrevoked exception covers this CURRENT pilot fan.

    It only skips the automatic human-takeover request for an outside manual send. The send stays recorded as
    evidence and in history; stops/no-contact, queued-send holds, unknown/duplicate send fences, scope and budgets
    are unaffected. Any other fan, binding or a fan removed from the pilot keeps the normal takeover."""
    start, end = _trial_exception_keys(binding)
    row = s.get(OfLaunchCommand, (account, start))
    if row is None or row.kind != TRIAL_TAKEOVER_EXCEPTION or row.binding_id != binding:
        return False
    if s.get(OfLaunchCommand, (account, end)) is not None:
        return False
    from db.onlyfans_models import OfRuntime
    runtime = s.get(OfRuntime, account)
    if runtime is None or runtime.binding_id != binding:
        return False
    pilot, config = current(s, runtime)
    covered = json.loads(row.before_json).get('fans', [])
    # The owner's trial exception names the two legacy trial fans only; it never covers a commercial account.
    return pilot is not None and pilot.scope_id == 1 and fan in config['fans'] and fan in covered


def set_trial_takeover_exception(store, tenant, account, *, enabled, actor, reason):
    """Record (enabled=True) or end (enabled=False) the exception for exactly the current pilot fans."""
    _command('0' * 64, actor, reason)
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account, lock=True)
        row, config = require(s, runtime, lock=True)
        if row.scope_id != 1:
            raise Refused('the trial takeover exception is limited to the legacy trial pilot')
        authorize(config, actor, 'handback')
        start, end = _trial_exception_keys(runtime.binding_id)
        key, kind = (start, TRIAL_TAKEOVER_EXCEPTION) if enabled else (end, TRIAL_TAKEOVER_EXCEPTION_END)
        if enabled and s.get(OfLaunchCommand, (account, end)) is not None:
            raise Refused('the trial takeover exception was already ended; it cannot be reopened')
        prior = s.get(OfLaunchCommand, (account, key))
        if prior is not None:
            return {'ok': True, 'kind': kind, 'fans': json.loads(prior.before_json).get('fans'), 'replayed': True}
        if not enabled and s.get(OfLaunchCommand, (account, start)) is None:
            raise Refused('no trial takeover exception to end')
        before = {'fans': list(config['fans']), 'scopeRevision': row.revision, 'scopeFingerprint': row.fingerprint,
                  'effect': 'outside manual sends to these fans are recorded but do not request takeover'}
        s.add(OfLaunchCommand(account_id=account, command_key=key, binding_id=runtime.binding_id, kind=kind,
              request_hash=_fingerprint([kind, account, runtime.binding_id, before, actor, reason]), actor=actor,
              reason=reason, before_json=_encoded(before), result_json=_encoded({'ok': True}), created_at=utcnow()))
        return {'ok': True, 'kind': kind, 'fans': before['fans'], 'replayed': False}


def trial_exception_covers_pilot(s, runtime):
    """True while the recorded owner exception covers EVERY current pilot fan (account-level outside-writer holds)."""
    pilot, config = current(s, runtime)
    return pilot is not None and pilot.scope_id == 1 and bool(config['fans']) and all(
        trial_takeover_exception(s, runtime.account_id, runtime.binding_id, fan) for fan in config['fans'])
