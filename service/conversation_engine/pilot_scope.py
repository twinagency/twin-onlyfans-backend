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


def validate(config):
    required = {'fans', 'product', 'operator', 'model', 'limits', 'qualification', 'authorization_mode', 'creator_profile'}
    if type(config) is not dict or not required <= set(config) <= required | {'offer_coverage'}:
        raise Refused('invalid pilot configuration fields')
    coverage = config.get('offer_coverage')
    if 'offer_coverage' in config:
        _exact(coverage, ('account_id', 'binding_id', 'complete', 'evidence_ref', 'valid_until', 'snapshot_hash'))
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
    _exact(operator, ('actor', 'name', 'permissions'))
    actor, name, permissions = operator['actor'], operator['name'], operator['permissions']
    if (type(actor) is not str or not _ACTOR.fullmatch(actor) or type(name) is not str or not name.strip()
            or name != name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name)
            or type(permissions) is not list or any(type(value) is not str or value not in _ACTIONS for value in permissions)
            or len(set(permissions)) != len(permissions)
            or not {'configure', 'activate', 'stop', 'read'}.issubset(permissions)
            or config['authorization_mode'] == 'supervised' and 'approve' not in permissions):
        raise Refused('invalid pilot operator permissions')
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
            'operator':{'actor':actor, 'name':name, 'permissions':sorted(permissions)},
            'model':{'provider':'openrouter', 'primary':model['primary'], 'upstream':model['upstream'],
                     'reported':model['reported'], 'version':model['version'],
                     'daily_cap_microusd':daily, 'creator_cap_microusd':creator},
            'limits':{'send_units':send_units, 'daily_send_units':daily_units, 'throttle_seconds':throttle,
                      'provider_requests':requests, 'provider_credit_budget':credits,
                      'provider_credits_per_request':per_request}}
    if 'offer_coverage' in config:
        result['offer_coverage'] = dict(coverage)
    return result


def current_offer_coverage(s, runtime, config):
    """The operator's existing VaultCatalogueCoverage, frozen in the existing pilot policy.

    No expiry/reference is inferred. Absent or expired approval stays unavailable;
    reading an expired policy remains possible for stop/recovery operations.
    """
    from .catalogue import VaultCatalogueCoverage, _catalogue_version
    raw = config.get('offer_coverage')
    if raw is None:
        raise Refused('offer_coverage_missing')
    if raw['account_id'] != runtime.account_id or raw['binding_id'] != runtime.binding_id:
        raise Refused('offer_coverage_scope_changed')
    expiry = datetime.fromisoformat(raw['valid_until'])
    if expiry <= utcnow():
        raise Refused('offer_coverage_expired')
    if raw['snapshot_hash'] != _catalogue_version(s, runtime):
        raise Refused('offer_coverage_stale')
    return VaultCatalogueCoverage(runtime.account_id, runtime.binding_id, True, raw['evidence_ref'], expiry, raw['snapshot_hash'])


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
    query = select(OfEnginePilotScope).where(OfEnginePilotScope.scope_id == 1)
    row = s.scalar(query.with_for_update() if lock else query)
    if row is None or row.account_id != runtime.account_id:
        return None, None
    if row.binding_id != runtime.binding_id:
        raise Refused('pilot creator binding changed')
    try:
        config = validate(json.loads(row.config_json))
    except (TypeError, ValueError, Refused):
        raise Refused('pilot configuration is invalid') from None
    if _fingerprint(config) != row.fingerprint or _encoded(config) != row.config_json:
        raise Refused('pilot configuration fingerprint changed')
    return row, config


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


def delivery_block(s, runtime, fan, *, commercial=False):
    """Pending human/provider evidence blocks pilot sends until safely classified."""
    pilot, config = current(s, runtime)
    if pilot is None:
        return None
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
    pending = s.scalar(select(OfFansProjection.event_key).join(OfFansWebhook,
        (OfFansWebhook.account_id == OfFansProjection.account_id) &
        (OfFansWebhook.epoch == OfFansProjection.epoch) &
        (OfFansWebhook.event_key == OfFansProjection.event_key)).join(OfFansConnection,
        (OfFansConnection.account_id == OfFansWebhook.account_id) &
        (OfFansConnection.epoch == OfFansWebhook.epoch)).where(
        OfFansProjection.account_id == runtime.account_id, OfFansProjection.status.in_(('pending','held')),
        OfFansConnection.binding_id == runtime.binding_id,
        or_(OfFansWebhook.event_type.in_(('accounts.disconnected','accounts.session_expired','accounts.authentication_failed')),
            (OfFansWebhook.event_type.in_(('messages.sent','messages.received') +
                (('messages.ppv.unlocked','transactions.new','chargebacks.new') if commercial else ()))) &
            or_(sent_fan == str(fan), sent_fan.is_(None)))).limit(1))
    human = s.scalar(select(OfFansEvidence.evidence_id).where(
        OfFansEvidence.account_id == runtime.account_id, OfFansEvidence.fan_id == fan,
        OfFansEvidence.kind == 'human_sent', OfFansEvidence.status.in_(('pending','held'))).limit(1))
    money = s.scalar(select(OfFansEvidence.evidence_id).where(
        OfFansEvidence.account_id == runtime.account_id, OfFansEvidence.fan_id == fan,
        OfFansEvidence.kind.in_(('unlock','transaction','chargeback')),
        OfFansEvidence.status.in_(('pending','held'))).limit(1)) if commercial else None
    malformed = s.scalar(select(OfEnginePilotWebhookDigest.event_key).join(OfFansConnection,
        (OfFansConnection.account_id == OfEnginePilotWebhookDigest.account_id) &
        (OfFansConnection.epoch == OfEnginePilotWebhookDigest.epoch)).where(
        OfEnginePilotWebhookDigest.account_id == runtime.account_id, OfFansConnection.binding_id == runtime.binding_id,
        OfEnginePilotWebhookDigest.safety_hold.is_(True)).limit(1))
    return 'pilot_evidence_pending' if pending is not None or human is not None or money is not None or malformed is not None else None


def policy_matches(policy, config, *, enabled):
    limits = config['limits']
    return bool(policy is not None and policy.transport_kind == 'fansapi'
                and policy.enabled is enabled and policy.media_enabled is True
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
    config = validate(config)
    request_hash = _fingerprint(['configure', account, config, expected_revision, actor, reason])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, account, command_key, request_hash)
        if prior is not None:
            return {**prior, 'replayed':True}
        row = s.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.scope_id == 1).with_for_update())
        if row is not None and (row.account_id != account or row.binding_id != runtime.binding_id):
            raise Refused('the pilot is bound to another creator')
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
        if not _product_current(s, account, config['product']):
            raise Refused('pilot product or price is unavailable')
        if 'offer_coverage' in config:
            current_offer_coverage(s, runtime, config)
        if row is not None and (row.provider_requests_used > config['limits']['provider_requests']
                or row.provider_credits_reserved > config['limits']['provider_credit_budget']):
            raise Refused('new limits cannot erase existing reservations')
        now = utcnow()
        before = {'revision':row.revision, 'status':row.status, 'fingerprint':row.fingerprint} if row else {}
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
        if status == 'active':
            if config['qualification']['mode'] == 'fixture' and os.environ.get('TWIN_PLAN_C_FIXTURE_ONLY') != '1':
                raise Refused('fixture scope cannot activate outside an isolated test')
            policy = s.get(OfDeliveryPolicy, account)
            connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account,
                                                                  OfFansConnection.status == 'active'))
            if (not runtime.enabled or not policy_matches(policy, config, enabled=True)
                    or not _product_current(s, account, config['product']) or connection is None
                    or connection.binding_id != runtime.binding_id):
                raise Refused('pilot activation prerequisites are not met')
            if (row.provider_requests_used >= config['limits']['provider_requests']
                    or row.provider_credits_reserved + config['limits']['provider_credits_per_request']
                    > config['limits']['provider_credit_budget']):
                raise Refused('pilot provider budget is exhausted')
        before = {'revision':row.revision, 'status':row.status, 'fingerprint':row.fingerprint}
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
        return {'accountId':account, 'status':row.status, 'revision':row.revision, 'fingerprint':row.fingerprint,
                'activeSince':row.active_since.isoformat() + 'Z' if row.active_since else None, 'config':config, 'providerRequestsUsed':row.provider_requests_used,
                'providerCreditsReserved':row.provider_credits_reserved}


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
