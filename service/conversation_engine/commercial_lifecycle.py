"""Audited commands that move an account from read-only preparation to a commercial scope.

``promote`` is the managing agency's command: it updates the account's preparation row IN PLACE (same scope_id, binding,
creation time, provider counters and last attempt, so there is no fresh budget) to contract ``commercial.v1`` and grants
nothing: the row stays ``configured``, the runtime and delivery policy stay disabled, and no sender runs. Launching is a
separate decision of the operator named in the configuration (``pilot_scope.set_status``). Preparation, imports and
vault refresh never call anything here. Every command is idempotent by its 64-hex command key and written to the
append-only launch-command audit before the scope row can name it.

Emergency stop and per-account stop are in this module too so that runtime, delivery policy and scope can never disagree.
"""
import json
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineIndependentAdmission, OfEnginePilotScope, OfFansConnection
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from . import preparation, scope_contract as contracts

KIND = 'commercial_scope'


def _view(row, config):
    return {'accountId': row.account_id, 'scopeId': row.scope_id, 'contractVersion': config['contract_version'],
            'revision': row.revision, 'status': row.status, 'fingerprint': row.fingerprint,
            'activeSince': row.active_since.isoformat() + 'Z' if row.active_since else None,
            'providerRequestsUsed': row.provider_requests_used, 'providerCreditsReserved': row.provider_credits_reserved,
            'promotedAt': row.commercial_since.isoformat() + 'Z' if row.commercial_since else None,
            'automaticRepliesEnabled': row.status == 'active', 'config': config}


def _prior(s, account, key, request_hash):
    prior = s.get(OfLaunchCommand, (account, key))
    if prior is None:
        return None
    if prior.kind != KIND or prior.request_hash != request_hash:
        raise Refused('commercial command conflict')
    return json.loads(prior.result_json)


def _unresolved_delivery(s, account):
    intent = s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
        OfTextIntent.status.in_(('dispatching', 'unknown'))).limit(1))
    independent = s.scalar(select(OfEngineIndependentAdmission.attempt_id).where(
        OfEngineIndependentAdmission.account_id == account,
        OfEngineIndependentAdmission.status.in_(('dispatching', 'unknown'))).limit(1))
    return intent is not None or independent is not None


def _connected(s, runtime, config):
    """The verified FansAPI connection and link for exactly this account's prepared provider identity."""
    from db.onlyfans_fans_intake_models import OfFansLink
    conn = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == runtime.account_id,
        OfFansConnection.binding_id == runtime.binding_id, OfFansConnection.status == 'active'))
    link = s.get(OfFansLink, (runtime.account_id, config['creator_id'])) if conn is not None else None
    return (conn if conn is not None and link is not None and link.state == 'connected'
            and (conn.provider_account_id, conn.provider_creator_id) == (config['provider_account'], config['creator_id'])
            and link.provider_account_id == config['provider_account'] else None)


def check_catalogue(runtime, config):
    catalogue = config.get('catalogue')
    if catalogue is None:
        return
    from datetime import datetime
    if catalogue['account_id'] != runtime.account_id or catalogue['binding_id'] != runtime.binding_id:
        raise Refused('commercial catalogue belongs to another creator')
    if datetime.fromisoformat(catalogue['valid_until']) <= utcnow():
        raise Refused('commercial catalogue has expired')


def catalogue_snapshot(store, tenant, account, *, evidence_ref, valid_until):
    """The `catalogue` block of a commercial scope: the agency's explicit approval of the catalogue as it stands NOW.

    Read-only. It makes no provider call and approves nothing by itself: the managing agency passes the result to
    `promote` (or the operator to a stopped `configure`), which is what records the approval. `evidence_ref` names the
    agency's approval record; `valid_until` is its explicit UTC expiry. The aggregate hash is the commercial catalogue version
    (live approved terms revisions, aliases and vault state of every approved member); launch refuses a stale one.
    """
    from datetime import datetime
    from .catalogue import _catalogue_version
    if (type(evidence_ref) is not str or not evidence_ref.startswith('vault:') or not 6 < len(evidence_ref) <= 128
            or type(valid_until) is not datetime or valid_until.tzinfo is not None or valid_until <= utcnow()):
        raise Refused('invalid commercial catalogue approval')
    with Session(store.engine) as s:
        runtime = store._runtime(s, tenant, account, lock=False)
        version = _catalogue_version(s, runtime)
        if version is None:
            raise Refused('commercial catalogue version is unavailable')
        return {'account_id': account, 'binding_id': runtime.binding_id, 'complete': True, 'evidence_ref': evidence_ref,
                'valid_until': valid_until.isoformat(), 'snapshot_hash': version, 'snapshot_kind': contracts.CATALOGUE_KIND}


def promote(store, tenant, account, body, *, expected_revision, command_key, actor, reason):
    """Preparation row -> commercial.v1 / configured, in place. Managing agency actor only; grants nothing."""
    preparation._command(command_key, actor, reason)
    preparation._integer(expected_revision, 1, 2**31 - 1)
    try:
        request_hash = preparation.fingerprint(['promote', tenant, account, body, expected_revision, actor, reason])
    except (TypeError, ValueError):
        raise Refused('invalid promotion request') from None
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, account, command_key, request_hash)
        if prior is not None:
            return {**prior, 'replayed': True}
        row = contracts.row_for(s, runtime, lock=True)
        if row is None or row.scope_id == 1 or contracts.claim(row) != contracts.PREPARATION:
            raise Refused('only a prepared account without a commercial scope can be promoted')
        row, old = preparation.current(s, runtime, lock=True)
        if row.revision != expected_revision:
            raise Refused('preparation revision conflict')
        preparation._disabled(s, runtime)
        if _unresolved_delivery(s, account):
            raise Refused('reconcile unresolved sends before promoting')
        if _connected(s, runtime, old) is None:
            raise Refused('the prepared provider connection must be verified before promotion')
        config = contracts.promotion_config(old, row.revision, row.fingerprint, command_key, body)
        if (config['limits']['provider_requests'] < row.provider_requests_used
                or config['limits']['provider_credit_budget'] < row.provider_credits_reserved):
            raise Refused('commercial limits cannot erase existing reservations')
        check_catalogue(runtime, config)
        now = utcnow()
        before = {'contract': preparation.VERSION, 'scopeId': row.scope_id, 'revision': row.revision, 'status': row.status,
                  'fingerprint': row.fingerprint, 'providerRequestsUsed': row.provider_requests_used,
                  'providerCreditsReserved': row.provider_credits_reserved}
        revision, digest, encoded = row.revision + 1, contracts.fingerprint(config), contracts.encoded(config)
        result = _view(SimpleNamespace(account_id=account, scope_id=row.scope_id, revision=revision, status='configured',
            fingerprint=digest, active_since=None, provider_requests_used=row.provider_requests_used,
            provider_credits_reserved=row.provider_credits_reserved, commercial_since=now), config)
        # The audit row (append-only) must exist before the scope row may name it: flush it first, then move the row.
        s.add(OfLaunchCommand(account_id=account, command_key=command_key, binding_id=runtime.binding_id, kind=KIND,
            request_hash=request_hash, actor=actor, reason=reason, before_json=contracts.encoded(before),
            result_json=contracts.encoded(result), created_at=now))
        s.flush()
        row.revision, row.status, row.active_since = revision, 'configured', None
        row.config_json, row.fingerprint = encoded, digest
        row.transition_command_key, row.commercial_since, row.updated_at = command_key, now, now
        return {**result, 'replayed': False}


def view(store, tenant, account):
    """Read-only commercial state of one account; any account can be asked, only its own row is ever shown."""
    with Session(store.engine) as s:
        runtime = store._runtime(s, tenant, account, lock=False)
        row = contracts.row_for(s, runtime)
        if row is None or row.scope_id == 1:
            return {'accountId': account, 'state': 'none' if row is None else 'legacy', 'eligible': False}
        kind = contracts.claim(row)
        if kind == contracts.COMMERCIAL:
            config = contracts.validated(row, runtime, kind)
            return {**_view(row, config), 'state': 'commercial'}
        if kind != contracts.PREPARATION:
            raise Refused('invalid preparation authority')
        _, config = preparation.current(s, runtime)
        policy = s.get(OfDeliveryPolicy, account)
        eligible = (row.status in ('configured', 'stopped') and not runtime.enabled
                    and policy is not None and not policy.enabled and not policy.media_enabled
                    and _connected(s, runtime, config) is not None)
        return {'accountId': account, 'state': 'preparation', 'eligible': eligible, 'revision': row.revision,
                'status': row.status, 'fingerprint': row.fingerprint,
                'providerRequestsUsed': row.provider_requests_used, 'providerCreditsReserved': row.provider_credits_reserved}


# ---------------------------------------------------------------- launch prerequisites (decision D4)
def catalogue_ready(s, runtime, config):
    """Hook for the catalogue contract of a later step. No product contract exists yet, so a full-delivery
    commercial scope can be configured but never launched: nothing may sell through a half-built contract."""
    raise Refused(contracts.PRODUCT_UNAVAILABLE)


def assert_launchable(s, runtime, row, config):
    """The commercial half of the launch decision, on top of the text-only checks pilot_scope already runs.

    Text-only scopes send and sell nothing, so they may launch before any catalogue contract exists."""
    if config['delivery_mode'] != 'text_only':
        catalogue_ready(s, runtime, config)
    if not config['fans']:
        raise Refused('commercial audience is empty')
    if _connected(s, runtime, config) is None:
        raise Refused('verified commercial connection unavailable')
    from db.onlyfans_vault_models import OfVaultSyncJob
    connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == runtime.account_id,
        OfFansConnection.binding_id == runtime.binding_id, OfFansConnection.status == 'active'))
    latest = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == runtime.account_id,
        OfVaultSyncJob.binding_id == runtime.binding_id, OfVaultSyncJob.connection_revision == connection.epoch,
        OfVaultSyncJob.provider_kind == 'fansapi', OfVaultSyncJob.provider_account == config['provider_account'])
        .order_by(OfVaultSyncJob.requested_at.desc(), OfVaultSyncJob.job_id.desc()).limit(1))
    if latest is None or latest.status != 'complete':
        raise Refused('commercial import is not complete')


# ---------------------------------------------------------------- stop (agency, operator via pilot_scope, emergency)
def stop_scope_in_session(s, store, runtime, row, *, actor, reason, now):
    """Caller holds the runtime lock and has already stopped the runtime and its queued work.

    Closes the delivery policy and moves the scope to stopped in the same transaction, so runtime, policy and scope
    can never disagree after any stop path. Returns the scope's new revision."""
    from onlyfans_runtime.text_delivery import TextAuthority
    policy = s.scalar(select(OfDeliveryPolicy).where(OfDeliveryPolicy.account_id == runtime.account_id).with_for_update())
    if policy is not None and policy.enabled:
        TextAuthority(store)._configure_in_session(s, runtime, expected_revision=policy.revision, enabled=False,
            limit_units=policy.limit_units, throttle_seconds=policy.throttle_seconds, media_enabled=None,
            actor=actor, reason=reason, scope_stop=True)
    if row.status != 'stopped':
        row.revision += 1
        row.status, row.active_since, row.updated_at = 'stopped', None, now
    return row.revision


def stop_active_scope(s, store, runtime, *, actor, reason, now):
    """Emergency-stop hook: stops this account's commercial scope only if it is active. None otherwise."""
    row = contracts.row_for(s, runtime, lock=True)
    if row is None or row.scope_id == 1 or contracts.claim(row) != contracts.COMMERCIAL or row.status != 'active':
        return None
    return stop_scope_in_session(s, store, runtime, row, actor=actor, reason=reason, now=now)


def assert_resume_allowed(s, runtime):
    """Resume re-enables a stopped runtime; it must never launch a commercial scope that is not active."""
    row = contracts.row_for(s, runtime)
    if row is None or row.scope_id == 1:
        return
    if contracts.claim(row) == contracts.COMMERCIAL and row.status == 'active':
        return
    raise Refused('resume requires an active commercial scope')


def stop(store, tenant, account, *, expected_revision, command_key, actor, reason):
    """The managing agency's stop of one account: runtime, queued work, policy and scope in one transaction."""
    from onlyfans_runtime.launch_operations import stop_runtime_in_session
    preparation._command(command_key, actor, reason)
    preparation._integer(expected_revision, 1, 2**31 - 1)
    request_hash = preparation.fingerprint(['stop', tenant, account, expected_revision, actor, reason])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = _prior(s, account, command_key, request_hash)
        if prior is not None:
            return {**prior, 'replayed': True}
        row = contracts.row_for(s, runtime, lock=True)
        if row is None or row.scope_id == 1 or contracts.claim(row) != contracts.COMMERCIAL:
            raise Refused('only a commercial scope can be stopped here')
        config = contracts.validated(row, runtime, contracts.COMMERCIAL)
        if row.revision != expected_revision:
            raise Refused('commercial revision conflict')
        now = utcnow()
        before = {'revision': row.revision, 'status': row.status, 'fingerprint': row.fingerprint,
                  'runtimeEnabled': runtime.enabled}
        stop_runtime_in_session(s, runtime, command_key=command_key, actor=actor, request_hash=request_hash, now=now)
        stop_scope_in_session(s, store, runtime, row, actor=actor, reason=reason, now=now)
        result = _view(row, config)
        s.add(OfLaunchCommand(account_id=account, command_key=command_key, binding_id=runtime.binding_id, kind=KIND,
            request_hash=request_hash, actor=actor, reason=reason, before_json=contracts.encoded(before),
            result_json=contracts.encoded(result), created_at=now))
        return {**result, 'replayed': False}
