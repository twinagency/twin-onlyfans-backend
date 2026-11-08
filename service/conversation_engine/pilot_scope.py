"""Durable, fail-closed configuration for the one supervised Plan C pilot.

This is configuration and conservative request reservation, not a second
message outbox or financial ledger. A normal supervised worker requires an
explicitly activated, fully configured scope; no function starts a worker or
makes a provider/model call. All mutations lock the existing account runtime.
"""
from hashlib import sha256
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CatalogItem
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEnginePilotScope, OfFansConnection
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow

_ACTIONS = frozenset({'configure', 'activate', 'approve', 'reject', 'edit', 'stop', 'takeover', 'handback', 'reconcile'})
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
    _exact(config, ('fans', 'product', 'operator', 'model', 'limits'))
    fans = _ids(config['fans'])
    product, operator, model, limits = config['product'], config['operator'], config['model'], config['limits']
    _exact(product, ('id', 'price_cents', 'media_ids', 'preview_ids'))
    item = _integer(product['id'], 1, 2**31-1)
    price = _integer(product['price_cents'], 300, 20000)
    media = _ids(product['media_ids'])
    preview = _ids(product['preview_ids'], empty=True)
    if not set(preview).issubset(media):
        raise Refused('pilot previews must belong to the product')
    _exact(operator, ('actor', 'name', 'permissions'))
    actor, name, permissions = operator['actor'], operator['name'], operator['permissions']
    if (type(actor) is not str or not _ACTOR.fullmatch(actor) or type(name) is not str or not name.strip()
            or name != name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name)
            or type(permissions) is not list or any(type(value) is not str or value not in _ACTIONS for value in permissions)
            or len(set(permissions)) != len(permissions)
            or not {'configure', 'activate', 'approve', 'stop'}.issubset(permissions)):
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
    return {'fans':fans, 'product':{'id':item, 'price_cents':price, 'media_ids':media, 'preview_ids':preview},
            'operator':{'actor':actor, 'name':name, 'permissions':sorted(permissions)},
            'model':{'provider':'openrouter', 'primary':model['primary'], 'upstream':model['upstream'],
                     'reported':model['reported'], 'version':model['version'],
                     'daily_cap_microusd':daily, 'creator_cap_microusd':creator},
            'limits':{'send_units':send_units, 'daily_send_units':daily_units, 'throttle_seconds':throttle,
                      'provider_requests':requests, 'provider_credit_budget':credits,
                      'provider_credits_per_request':per_request}}


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
    client = model.client
    if (type(client).__module__ != 'model_transport.openrouter' or type(client).__name__ != 'OpenRouterClient'
            or getattr(client, '_upstream_provider', None) != config['model']['upstream']
            or getattr(client, '_price_caps', None) != {model.config.primary_model: model.config.prices_for(model.config.primary_model)}):
        raise Refused('model transport does not match the pilot scope')
    policy = s.get(OfDeliveryPolicy, runtime.account_id)
    if not policy_matches(policy, config, enabled=not draft_only) or not _product_current(s, runtime.account_id, config['product']):
        raise Refused('pilot delivery policy or product changed')
    return {'revision':row.revision, 'fingerprint':row.fingerprint, 'status':row.status}


def _product_current(s, account, product):
    item = s.get(CatalogItem, product['id'])
    try:
        media = json.loads(item.media_ids)
        previews = json.loads(item.preview_media_ids)
        if type(media) is not list or type(previews) is not list:
            return False
        return (item.account_id == account and item.enabled is True and item.script_id is None
                and item.price_cents == product['price_cents']
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
            row.status, row.config_json, row.fingerprint, row.updated_at = 'configured', encoded, digest, now
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
        result = {'accountId':account, 'revision':row.revision, 'status':row.status, 'fingerprint':row.fingerprint}
        _audit(s, runtime, command_key, actor, reason, request_hash, before, result)
        return {**result, 'replayed':False}


def describe(store, tenant, account):
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account, lock=False)
        row, config = current(s, runtime)
        if row is None:
            return {'accountId':account, 'status':'unconfigured', 'revision':0, 'config':None}
        return {'accountId':account, 'status':row.status, 'revision':row.revision, 'fingerprint':row.fingerprint,
                'config':config, 'providerRequestsUsed':row.provider_requests_used,
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

    def __call__(self, method, url):
        if method not in ('GET', 'POST') or type(url) is not str:
            raise Refused('invalid pilot provider request')
        reserve_provider_request(self.store, self.tenant, self.account, expected_revision=self.revision)
