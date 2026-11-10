"""Plan C operator surface (v2): drafts, conversation controls and unresolved work for 'fansapi' accounts.

Everything here is a thin, audited layer over modules that already own the behaviour:
  drafts        conversation_engine.approval (approve / reject / edit / pending)
  controls      Store.control (stop = pause_automation, takeover = request_takeover, handback = release_takeover)
  unresolved    read-only views over of_text_intent, of_fans_evidence, of_engine_access_evidence, of_conversation_state,
                of_engine_request
  reconcile     an audited operator observation; it changes canonical state only where an existing mechanism does

Nothing here approves, hands back or sends by itself. Handback is NOT an approval: it only makes the conversation
eligible for a fresh plan (the owner epoch change withdraws every live approval of the fan).

Idempotency: a command carries a 64-hex commandKey. Draft and reconcile commands are recorded in of_launch_command
(kind 'plan_c_*', request hash over path ids and body); conversation controls use of_control_audit (Store.control).
The same key with the same body replays the stored acknowledgement; the same key with a different body is a Conflict.
"""
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import Fan, Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import (OfEngineAccessEvidence, OfEngineApproval, OfEngineDecision, OfEngineModelCall,
                                       OfEngineOwner, OfEngineRequest)
from db.onlyfans_fans_intake_models import OfFansEvidence
from onlyfans_read.agency_scope import ACTOR_PATTERN
from onlyfans_read.contract import conversation_id_for
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from . import approval
from .history_scope import current_binding_message
from .operator import FANSAPI_EVIDENCE_PREFIX, evidence_allowed

PLAN_C_TRANSPORT = 'fansapi'
MESSAGES_SHOWN = 10
STALE_PENDING = timedelta(minutes=15)
CONTROL = {'stop': ('paused', 'pause_automation'),
           'takeover': ('human_controlled', 'request_takeover'),
           'handback': ('automated', 'release_takeover')}
_HEX64 = re.compile(r'[a-f0-9]{64}')
_UUID = re.compile(r'[0-9a-f-]{36}')
_LOCKS = [threading.Lock() for _ in range(64)]


class NotFound(Refused):
    """The scope, draft, conversation or item does not exist (HTTP 404)."""


class Conflict(Refused):
    """Version or revision conflict, an unavailable state, or a reused command key (HTTP 409)."""


class BadRequest(ValueError):
    """The command itself is malformed (HTTP 400)."""


def _iso(value):
    if value is None:
        return None
    text = value.isoformat()
    return text if value.tzinfo is not None else text + 'Z'


def _zulu(text):
    """approval.py reports naive UTC timestamps as text; the browser must not read them as local time."""
    return None if text is None else _iso(datetime.fromisoformat(text))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _lock(account, key):
    return _LOCKS[int(hashlib.sha256(f'{account}:{key}'.encode()).hexdigest()[:8], 16) % len(_LOCKS)]


def _actor(actor):
    """A named human only. The pattern admits operator sessions and agency users; automated names cannot match it."""
    if type(actor) is not str or not re.fullmatch(ACTOR_PATTERN, actor):
        raise BadRequest('named human actor required')
    return actor


def _key(command_key):
    if type(command_key) is not str or not _HEX64.fullmatch(command_key):
        raise BadRequest('invalid command key')
    return command_key


def _int(value, low=0, high=2 ** 31 - 2):
    if type(value) is not int or not low <= value <= high:
        raise BadRequest('invalid integer')
    return value


def _scope(store, tenant, account):
    """(binding id, gated) for the account; NotFound when the account is not in this tenant's scope."""
    with Session(store.engine) as s, s.begin():
        try:
            runtime = store._runtime(s, tenant, account, lock=False)
        except Refused:
            raise NotFound('scope unavailable') from None
        from db.onlyfans_engine_models import OfEnginePilotScope
        selected = s.get(OfEnginePilotScope, 1)
        if selected is not None and selected.account_id != account:
            raise NotFound('scope unavailable')
        policy = s.get(OfDeliveryPolicy, account)
        return runtime.binding_id, policy is not None and policy.transport_kind == PLAN_C_TRANSPORT


def _require_gated(store, tenant, account):
    binding, gated = _scope(store, tenant, account)
    if not gated:
        raise Conflict('the operator surface is limited to fansapi pilot accounts')
    return binding


# ------------------------------------------------------------------ idempotent command record
def _prior(s, account, key, kind, request_hash):
    row = s.get(OfLaunchCommand, (account, key))
    if row is None:
        return None
    if row.kind != kind or row.request_hash != request_hash:
        raise Conflict('command key reused with a different command')
    return json.loads(row.result_json)


def _record(s, account, binding, key, kind, request_hash, actor, reason, before, result):
    s.add(OfLaunchCommand(account_id=account, command_key=key, binding_id=binding, kind=kind, request_hash=request_hash,
                          actor=actor, reason=reason[:512], before_json=json.dumps(before, sort_keys=True),
                          result_json=json.dumps(result, sort_keys=True), created_at=utcnow()))
    s.flush()


def _idempotent(store, account, binding, key, kind, request_hash, actor, reason, before, run):
    """Replay a recorded command, else run it and record its acknowledgement. One process-wide lock per key keeps two
    identical in-flight requests from both running; the primary key settles a race between processes."""
    with _lock(account, key):
        with Session(store.engine) as s, s.begin():
            prior = _prior(s, account, key, kind, request_hash)
        if prior is not None:
            return {**prior, 'replayed': True}
        result = run()
        try:
            with Session(store.engine) as s, s.begin():
                _record(s, account, binding, key, kind, request_hash, actor, reason, before, result)
        except IntegrityError:
            with Session(store.engine) as s, s.begin():
                prior = _prior(s, account, key, kind, request_hash)
            if prior is None:
                raise
            return {**prior, 'replayed': True}
        return {**result, 'replayed': False}


# ------------------------------------------------------------------ drafts
def _recent_messages(s, binding, account, fan_id):
    rows = list(s.scalars(select(Message).where(Message.account_id == account, Message.fan_id == fan_id,
        Message.is_unsent.is_(False), current_binding_message(binding))
        .order_by(Message.created_at.desc(), Message.message_id.desc()).limit(MESSAGES_SHOWN)))
    return [{'direction': 'fan' if m.direction == 'in' else 'creator' if m.direction == 'out' else m.direction,
             'text': m.body or '', 'at': _iso(m.created_at)} for m in reversed(rows)]


def _fan_evidence(s, binding, account, fan_id):
    """Show only evidence provably belonging to this creator and fan."""
    from fans_api_adapter.evidence_scope import webhook_binding
    notes = []
    unknown = s.scalars(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
        OfTextIntent.binding_id == binding, OfTextIntent.fan_id == fan_id,
        OfTextIntent.status.in_(('dispatching', 'unknown')))).all()
    if unknown:
        notes.append(f'{len(unknown)} unresolved send(s) to this fan: do not resend')
    rows = s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
        OfFansEvidence.fan_id == fan_id, OfFansEvidence.kind != 'human_sent',
        (OfFansEvidence.status == 'held') |
        ((OfFansEvidence.kind == 'unlock') & (OfFansEvidence.status == 'applied') &
         (OfFansEvidence.reason == 'unlock_confirmed_finance_unlinked')))).all()
    scoped = [row for row in rows if webhook_binding(s, row) == binding]
    held = sum(row.status == 'held' for row in scoped)
    unlocks = sum(row.kind == 'unlock' and row.status == 'applied' for row in scoped)
    if held:
        notes.append(f'{held} held payment record(s) need review')
    if unlocks:
        notes.append(f'{unlocks} provider-reported unlock(s); transaction and settlement remain unlinked')
    return '; '.join(notes) or None


def require_pilot_reader(store, tenant, account, actor):
    """A configured singleton pilot is readable only by its named administrator."""
    from db.onlyfans_engine_models import OfEnginePilotScope
    from .pilot_scope import current as current_pilot, authorize
    with Session(store.engine) as s, s.begin():
        try:
            runtime = store._runtime(s, tenant, account, lock=False)
        except Refused:
            raise NotFound('scope unavailable') from None
        selected = s.get(OfEnginePilotScope, 1)
        if selected is not None and selected.account_id != account:
            raise NotFound('scope unavailable')
        pilot, config = current_pilot(s, runtime)
        if pilot is not None:
            authorize(config, actor, 'read')


def list_drafts(store, tenant, account, actor=None):
    """Live (awaiting or approved) draft versions in the shape the dashboard parses (planc-dash plan-c.ts)."""
    require_pilot_reader(store, tenant, account, actor)
    binding, gated = _scope(store, tenant, account)
    if not gated:
        return []
    approval.expire_stale(store, tenant, account)
    views = approval.pending(store, tenant, account)
    out = []
    with Session(store.engine) as s, s.begin():
        from .pilot_scope import current as current_pilot
        runtime = store._runtime(s, tenant, account)
        pilot, pilot_config = current_pilot(s, runtime)
        if pilot is not None:
            from .pilot_scope import authorize
            authorize(pilot_config, actor, 'read')
        allowed = set(pilot_config['fans']) if pilot is not None else None
        for view in views:
            fan_id = int(view['fanId'])
            if allowed is not None and fan_id not in allowed:
                continue
            state = s.get(OfConversationState, (account, fan_id))
            parts = view['parts']
            offers = [p for p in parts if p['action'] == 'offer']
            draft = {'requestId': view['requestId'], 'generation': view['generation'], 'draftVersion': view['version'],
                     'draftHash': view['draftHash'], 'status': view['status'],
                     'action': 'offer' if offers else 'reply', 'fanId': str(fan_id),
                     'conversationId': conversation_id_for(account, fan_id), 'expiresAt': _zulu(view['expiresAt']),
                     'parts': parts,
                     'conversation': {'revision': state.revision if state else 0,
                                      'messages': _recent_messages(s, binding, account, fan_id)},
                     'evidence': _fan_evidence(s, binding, account, fan_id),
                     'approvedBy': view['actor'] if view['status'] == 'approved' else None,
                     'approvedAt': _zulu(view['decidedAt']) if view['status'] == 'approved' else None}
            if offers:
                head = offers[0]
                draft['offer'] = {'productId': None if head['catalog_item_id'] is None else str(head['catalog_item_id']),
                                  'mediaIds': [str(m) for m in head['media_ids']], 'mediaCount': len(head['media_ids']),
                                  'priceCents': head['price_cents'], 'currency': head['currency']}
            out.append(draft)
    return out


_DRAFT_KEYS = {'approve': {'expectedVersion', 'commandKey', 'actor'},
               'reject': {'expectedVersion', 'commandKey', 'actor', 'reason'},
               'edit': {'expectedVersion', 'commandKey', 'actor', 'parts'}}


def _refusal(exc):
    """Map an approval/Store refusal to our error classes (the message is a fixed internal string, never fan text)."""
    text = str(exc)
    if text in ('approval not found', 'scope unavailable', 'conversation unavailable', 'engine decision unavailable'):
        return NotFound(text)
    if text.startswith(('approval needs', 'invalid edited text', 'edited text must not', 'an edit keeps', 'an offer draft has')):
        return BadRequest(text)
    return Conflict(text)


def list_receipts(store, tenant, account, actor=None):
    """Recent provider acceptances for approved engine sends in this creator binding.

    A receipt means the provider accepted a message; it does not establish a
    fan read, payment, settlement, or access. Unknown sends are never included.
    """
    require_pilot_reader(store, tenant, account, actor)
    binding, gated = _scope(store, tenant, account)
    if not gated:
        return []
    with Session(store.engine) as s, s.begin():
        try:
            runtime = store._runtime(s, tenant, account)
        except Refused:
            raise NotFound('scope unavailable') from None
        if runtime.binding_id != binding:
            raise NotFound('scope unavailable')
        from .pilot_scope import current as current_pilot
        pilot, pilot_config = current_pilot(s, runtime)
        if pilot is not None:
            from .pilot_scope import authorize
            authorize(pilot_config, actor, 'read')
        allowed = set(pilot_config['fans']) if pilot is not None else None
        approved = select(OfEngineApproval.request_id).where(
            OfEngineApproval.account_id == OfEngineDecision.account_id,
            OfEngineApproval.binding_id == binding, OfEngineApproval.fan_id == OfEngineDecision.fan_id,
            OfEngineApproval.request_id == OfEngineDecision.request_id,
            OfEngineApproval.generation == OfEngineDecision.generation,
            OfEngineApproval.status.in_(('approved', 'consumed', 'invalidated', 'expired')),
            OfEngineApproval.actor.is_not(None), OfEngineApproval.decided_at.is_not(None)).exists()
        rows = s.execute(select(OfTextIntent, OfEngineDecision).join(OfEngineDecision, and_(
            OfEngineDecision.account_id == OfTextIntent.account_id,
            OfEngineDecision.intent_id == OfTextIntent.intent_id)).join(OfEngineOwner, and_(
            OfEngineOwner.account_id == OfTextIntent.account_id, OfEngineOwner.fan_id == OfTextIntent.fan_id))
            .where(OfTextIntent.account_id == account, OfTextIntent.binding_id == binding,
                OfEngineDecision.binding_id == binding, OfEngineOwner.binding_id == binding,
                OfEngineDecision.fan_id == OfTextIntent.fan_id, OfEngineDecision.payload_hash == OfTextIntent.payload_hash,
                OfTextIntent.status == 'accepted',
                OfTextIntent.provider_message_id.is_not(None), OfTextIntent.receipt_json.is_not(None), approved,
                True if allowed is None else OfTextIntent.fan_id.in_(allowed))
            .order_by(OfTextIntent.updated_at.desc(), OfTextIntent.intent_id).limit(50)).all()
        result = []
        for intent, decision in rows:
            if allowed is not None and intent.fan_id not in allowed:
                continue
            try:
                proof = json.loads(intent.receipt_json)
                stamp = proof['occurred_at']
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if (type(proof) is not dict or type(stamp) is not str or parsed.tzinfo is None
                        or parsed.utcoffset() is None or proof.get('account_id') != account
                        or proof.get('fan_id') != intent.fan_id or proof.get('intent_id') != intent.intent_id
                        or proof.get('attempt_id') != intent.attempt_id or proof.get('payload_hash') != intent.payload_hash
                        or proof.get('evidence_ref') != intent.evidence_ref or proof.get('message_id') != intent.provider_message_id):
                    continue
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
            result.append({'intentId': intent.intent_id, 'fanId': str(intent.fan_id),
                           'messageId': str(intent.provider_message_id), 'acceptedAt': stamp,
                           'action': decision.action, 'projected': intent.projected_at is not None})
        return result


def draft_command(store, tenant, account, request_id, generation, action, body):
    """approve | reject | edit one draft version. Returns the acknowledgement dict (replayed true on a replay)."""
    if action not in _DRAFT_KEYS or type(body) is not dict or set(body) != _DRAFT_KEYS[action]:
        raise BadRequest('exact command keys required')
    if type(request_id) is not str or not _UUID.fullmatch(request_id) or type(generation) is not int or not 0 <= generation < 2 ** 31:
        raise BadRequest('invalid draft id')
    key, actor, version = _key(body['commandKey']), _actor(body['actor']), _int(body['expectedVersion'], 1)
    reason = parts = None
    if action == 'reject':
        reason = body['reason']
        if type(reason) is not str or not reason.strip() or len(reason.strip()) > 200 or re.search(r'[\x00-\x1f]', reason):
            raise BadRequest('a reason is required')
        reason = reason.strip()
    if action == 'edit':
        parts = body['parts']
        if (type(parts) is not list or not 1 <= len(parts) <= 4
                or any(type(t) is not str or not t.strip() or len(t) > 2000 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', t) for t in parts)):
            raise BadRequest('invalid edited text')
    binding = _require_gated(store, tenant, account)
    kind = 'plan_c_' + action
    request_hash = _digest([account, action, request_id, generation, {k: v for k, v in body.items() if k != 'commandKey'}])

    def run():
        try:
            if action == 'approve':
                view = approval.approve(store, tenant, account, request_id, generation, expected_version=version, actor=actor)
            elif action == 'reject':
                view = approval.reject(store, tenant, account, request_id, generation, expected_version=version,
                                       actor=actor, reason=reason)
            else:
                view = approval.edit(store, tenant, account, request_id, generation, expected_version=version,
                                     actor=actor, parts=parts)
        except Refused as exc:
            raise _refusal(exc) from None
        # An edit supersedes the draft with generation + 1 (decision rows are immutable): report the generation to use next.
        return {'ok': True, 'accountId': account, 'requestId': request_id, 'generation': view['generation'], 'action': action,
                'commandKey': key, 'recordedVersion': view['version'], 'status': view['status'],
                'draftHash': view['draftHash']}
    return _idempotent(store, account, binding, key, kind, request_hash, actor, f'{action} {request_id}/{generation}',
                       {'expectedVersion': version}, run)


# ------------------------------------------------------------------ conversation controls
def conversation_command(store, tenant, account, fan_id, action, body):
    """stop | takeover | handback. Handback never approves or replays a draft: the control change withdraws every
    live approval and the conversation only becomes eligible for a fresh plan."""
    from db.onlyfans_models import OfControlAudit
    if action not in CONTROL or type(body) is not dict or set(body) != {'commandKey', 'actor', 'expectedRevision'}:
        raise BadRequest('exact command keys required')
    key, actor, revision = _key(body['commandKey']), _actor(body['actor']), _int(body['expectedRevision'])
    binding = _require_gated(store, tenant, account)
    with Session(store.engine) as s, s.begin():
        owner = s.get(OfEngineOwner, (account, fan_id))
        from .pilot_scope import current as current_pilot, authorize
        runtime = store._runtime(s, tenant, account)
        pilot, pilot_config = current_pilot(s, runtime)
        if pilot is not None:
            if fan_id not in pilot_config['fans']:
                raise NotFound('conversation unavailable')
            authorize(pilot_config, actor, action)
        if s.get(Fan, (account, fan_id)) is None:
            raise NotFound('conversation unavailable')
        if owner is None or owner.binding_id != binding:
            raise Conflict('the conversation is not in the pilot scope')
    mode, control_action = CONTROL[action]
    reason = f'Plan C operator: {action}'
    with _lock(account, key):
        with Session(store.engine) as s, s.begin():
            prior = s.get(OfControlAudit, (account, key))
            if prior is not None:
                if (prior.fan_id, prior.mode, prior.previous_revision, prior.actor, prior.reason, prior.action) != (
                        fan_id, mode, revision, actor, reason, control_action):
                    raise Conflict('command key reused with a different command')
                return _control_ack(account, fan_id, action, key, prior.revision, True)
        try:
            recorded = store.control(tenant, account, fan_id, mode=mode, expected_revision=revision, command_key=key,
                                     actor=actor, reason=reason, action=control_action)
        except Refused as exc:
            raise _refusal(exc) from None
        return _control_ack(account, fan_id, action, key, recorded, False)


def _control_ack(account, fan_id, action, key, revision, replayed):
    return {'ok': True, 'accountId': account, 'conversationId': conversation_id_for(account, fan_id), 'action': action,
            'commandKey': key, 'recordedRevision': revision, 'replayed': replayed}


# ------------------------------------------------------------------ unresolved work
def _item(kind, ident, owner, next_step, summary, conversation=None, revision=None, created=None, **extra):
    row = {'id': f'{kind}:{ident}', 'kind': kind, 'owner': owner, 'nextStep': next_step, 'summary': summary[:1000]}
    if conversation is not None:
        row['conversationId'] = conversation
    if revision is not None:
        row['revision'] = revision
    if created is not None:
        row['createdAt'] = _iso(created)
    row.update(extra)
    return row


def _acknowledged(s, account, binding):
    """Item ids an operator already reconciled (plan_c_reconcile records); the record itself is permanent."""
    out = {}
    for row in s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.account_id == account,
            OfLaunchCommand.kind == 'plan_c_reconcile', OfLaunchCommand.binding_id == binding).order_by(OfLaunchCommand.created_at)):
        before = json.loads(row.before_json)
        out[before['id']] = row
    return out


def unresolved(store, tenant, account, actor=None):
    """Every item has an owner and a next step. Owners are roles (the dashboard limits owner to 64 characters)."""
    require_pilot_reader(store, tenant, account, actor)
    binding, gated = _scope(store, tenant, account)
    if not gated:
        return []
    items = []
    with Session(store.engine) as s, s.begin():
        seen = _acknowledged(s, account, binding)
        pilot = {f for (f,) in s.execute(select(OfEngineOwner.fan_id).where(OfEngineOwner.account_id == account,
                                                                             OfEngineOwner.binding_id == binding))}
        from .pilot_scope import current as current_pilot
        runtime = store._runtime(s, tenant, account)
        scope, pilot_config = current_pilot(s, runtime)
        if scope is not None:
            from .pilot_scope import authorize
            authorize(pilot_config, actor, 'read')
        allowed = set(pilot_config['fans']) if scope is not None else None
        if allowed is not None:
            pilot &= allowed

        def conv(fan_id):
            state = s.get(OfConversationState, (account, fan_id))
            return conversation_id_for(account, fan_id), (state.revision if state else 0)

        # 1. Sends whose outcome is unknown. They are never resent and never called "not sent" without proof.
        for row in s.scalars(select(OfTextIntent).where(OfTextIntent.account_id == account, OfTextIntent.binding_id == binding,
                OfTextIntent.status.in_(('dispatching', 'unknown')),
                True if allowed is None else OfTextIntent.fan_id.in_(allowed)).order_by(OfTextIntent.created_at).limit(200)):
            if allowed is not None and row.fan_id not in allowed:
                continue
            cid, revision = conv(row.fan_id)
            item_id = f'unknown_send:{row.intent_id}'
            note = seen.get(item_id)
            summary = (f'A send to fan {row.fan_id} may have reached the fan (status {row.status}). Do not resend. '
                       'Only provider history showing the exact message releases the hold.')
            step = 'reconcile_with_provider_history'
            if note is not None:
                step = 'awaiting_provider_proof'
                summary += f' Operator evidence recorded by {note.actor[:20]}: {note.reason[:120]}; it does not release the hold.'
            items.append(_item('unknown_send', row.intent_id, 'operator', step, summary, cid, revision,
                               row.wire_started_at or row.created_at))
        # 2. Money that is not linked to an unlock (kept apart from access evidence until a person links or dismisses it).
        from fans_api_adapter.evidence_scope import webhook_binding
        unscoped_provider_evidence = False
        stale = utcnow() - STALE_PENDING
        for row in s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
                OfFansEvidence.kind.in_(('transaction', 'chargeback')), OfFansEvidence.status.in_(('pending', 'held')),
                OfFansEvidence.peer_id.is_(None), OfFansEvidence.intent_id.is_(None),
                True if allowed is None else OfFansEvidence.fan_id.in_(allowed))
                .order_by(OfFansEvidence.created_at).limit(200)):
            if webhook_binding(s, row) != binding:
                unscoped_provider_evidence = True
                continue
            if row.fan_id not in pilot:
                continue
            if row.status == 'pending' and row.kind == 'transaction' and row.created_at > stale:
                continue  # allow a newly arrived transaction its normal review window
            cid, revision = conv(row.fan_id) if row.fan_id in pilot else (None, None)
            amount = '' if row.amount_cents is None else f' of {row.amount_cents} cents'
            detail = (f'{row.kind}{amount} for fan {row.fan_id} is unlinked ({row.reason or row.status}). ')
            detail += ('It does not automatically revoke access or adjust a purchase.' if row.kind == 'chargeback'
                       else 'It is not attributed to a message or treated as settled by this view.')
            items.append(_item('unlinked_transaction', row.evidence_id, 'operator',
                               'review_and_reconcile' if row.status == 'held' else 'review_unlinked_financial_evidence',
                               detail, cid, revision, row.created_at, evidenceId=row.evidence_id))
        # 3. Access evidence that is not confirmed.
        for row in s.scalars(select(OfEngineAccessEvidence).join(OfTextIntent,
                and_(OfTextIntent.account_id == OfEngineAccessEvidence.account_id,
                     OfTextIntent.intent_id == OfEngineAccessEvidence.intent_id)).where(
                OfEngineAccessEvidence.account_id == account, OfTextIntent.binding_id == binding,
                OfTextIntent.fan_id == OfEngineAccessEvidence.fan_id,
                (OfEngineAccessEvidence.status != 'confirmed') | OfEngineAccessEvidence.applied.is_(False),
                True if allowed is None else OfEngineAccessEvidence.fan_id.in_(allowed))
                .order_by(OfEngineAccessEvidence.created_at).limit(200)):
            if allowed is not None and row.fan_id not in allowed:
                continue
            item_id = f'unreconciled_access:access-{row.evidence_key}'
            if item_id in seen:
                continue
            cid, revision = conv(row.fan_id) if row.fan_id in pilot else (None, None)
            items.append(_item('unreconciled_access', f'access-{row.evidence_key}', 'operator', 'confirm_access_evidence',
                               f'Access evidence for fan {row.fan_id} is {row.status} (applied={row.applied}). '
                               'Do not assert the fan has access.', cid, revision, row.created_at))
        for row in s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
                OfFansEvidence.kind == 'unlock',
                (OfFansEvidence.status.in_(('pending', 'paired', 'held'))) |
                ((OfFansEvidence.status == 'applied') &
                 (OfFansEvidence.reason == 'unlock_confirmed_finance_unlinked')),
                True if allowed is None else OfFansEvidence.fan_id.in_(allowed))
                .order_by(OfFansEvidence.created_at).limit(200)):
            if webhook_binding(s, row) != binding:
                unscoped_provider_evidence = True
                continue
            if row.fan_id not in pilot:
                continue
            if row.status not in {'held', 'applied'} and row.created_at > stale:
                continue
            cid, revision = conv(row.fan_id) if row.fan_id in pilot else (None, None)
            if row.status == 'applied':
                step = 'review_unlinked_financial_evidence'
                summary = (f'Provider reported an unlock for fan {row.fan_id}, message {row.message_id}. '
                           'A separate transaction and settlement are not linked.')
            else:
                step = 'review_and_reconcile' if row.status == 'held' else 'review_unlock_evidence'
                summary = f'Unlock for fan {row.fan_id} is {row.status} ({row.reason or "no reason"}). Not yet confirmed access.'
            items.append(_item('unreconciled_access', row.evidence_id, 'operator', step, summary,
                               cid, revision, row.created_at, evidenceId=row.evidence_id))
        for row in s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
                OfFansEvidence.kind == 'human_sent', OfFansEvidence.status.in_(('pending', 'held')))
                .order_by(OfFansEvidence.created_at).limit(200)):
            if webhook_binding(s, row) != binding:
                unscoped_provider_evidence = True
                break
        from db.onlyfans_fans_intake_models import OfFansProjection
        if s.scalar(select(OfFansProjection.event_key).where(OfFansProjection.account_id == account,
                OfFansProjection.status == 'held', OfFansProjection.detail == 'historical_creator_binding').limit(1)):
            unscoped_provider_evidence = True
        if scope is not None and scope.active_since is not None:
            from fans_api_adapter.event_scope import event_fan
            from db.onlyfans_engine_models import OfFansWebhook, OfFansConnection
            fan_expr = event_fan(OfFansWebhook.event_type, OfFansWebhook.raw_body)
            prior = s.scalar(select(OfFansProjection.event_key).join(OfFansWebhook, and_(
                OfFansWebhook.account_id == OfFansProjection.account_id,
                OfFansWebhook.epoch == OfFansProjection.epoch, OfFansWebhook.event_key == OfFansProjection.event_key))
                .join(OfFansConnection, and_(OfFansConnection.account_id == OfFansWebhook.account_id,
                      OfFansConnection.epoch == OfFansWebhook.epoch)).where(
                OfFansProjection.account_id == account, OfFansProjection.status == 'pending',
                OfFansConnection.binding_id == binding, OfFansWebhook.event_type == 'messages.received',
                OfFansWebhook.received_at < scope.active_since, fan_expr.in_(tuple(map(str, allowed)))).limit(1))
            if prior is not None:
                items.append(_item('pre_activation_webhook', 'review', 'operator', 'review_earlier_inbound',
                    'An earlier inbound event was retained before this activation. Review it; it is not automatically sent.'))
            from db.onlyfans_models import OfIngress
            completed = select(OfEngineRequest.request_id).where(OfEngineRequest.account_id == OfIngress.account_id,
                OfEngineRequest.fan_id == OfIngress.fan_id, OfEngineRequest.binding_id == binding,
                OfEngineRequest.source_message_id == OfIngress.message_id, OfEngineRequest.status == 'completed').exists()
            old_ingress = s.scalar(select(OfIngress.event_key).where(OfIngress.account_id == account,
                OfIngress.binding_id == binding, OfIngress.fan_id.in_(allowed), OfIngress.source != 'import',
                OfIngress.received_at < scope.active_since, OfIngress.status.in_(('pending','applied')),
                ~completed).limit(1))
            if old_ingress is not None:
                items.append(_item('pre_activation_ingress', 'review', 'operator', 'review_earlier_inbound',
                    'An earlier projected inbound event remains unresolved. It is retained and is not automatically sent.'))
            conflict = s.scalar(select(OfFansProjection.event_key).where(OfFansProjection.account_id == account,
                OfFansProjection.status == 'held', OfFansProjection.detail == 'pilot_event_key_conflict').limit(1))
            if conflict is not None:
                items.append(_item('webhook_conflict', 'review', 'operator', 'review_event_conflict',
                    'A webhook key had conflicting signed payloads. Preserve the evidence and review it before any send.'))
            held = s.scalar(select(OfFansProjection.event_key).join(OfFansWebhook, and_(
                OfFansWebhook.account_id == OfFansProjection.account_id,
                OfFansWebhook.epoch == OfFansProjection.epoch, OfFansWebhook.event_key == OfFansProjection.event_key))
                .join(OfFansConnection, and_(OfFansConnection.account_id == OfFansWebhook.account_id,
                      OfFansConnection.epoch == OfFansWebhook.epoch)).where(
                OfFansProjection.account_id == account, OfFansProjection.status == 'held',
                OfFansConnection.binding_id == binding,
                or_(OfFansWebhook.event_type.in_(('accounts.disconnected','accounts.session_expired','accounts.authentication_failed')),
                    fan_expr.in_(tuple(map(str, allowed))), fan_expr.is_(None))).limit(1))
            if held is not None:
                items.append(_item('unattributed_webhook', 'review', 'operator', 'review_provider_evidence',
                    'Signed provider evidence could not be safely attributed or projected. Sends remain held pending review.'))
            for epoch_, key_, detail_ in s.execute(select(OfFansProjection.epoch, OfFansProjection.event_key,
                    OfFansProjection.detail).join(OfFansWebhook, and_(
                    OfFansWebhook.account_id == OfFansProjection.account_id,
                    OfFansWebhook.epoch == OfFansProjection.epoch, OfFansWebhook.event_key == OfFansProjection.event_key))
                    .join(OfFansConnection, and_(OfFansConnection.account_id == OfFansWebhook.account_id,
                          OfFansConnection.epoch == OfFansWebhook.epoch)).where(
                    OfFansProjection.account_id == account, OfFansProjection.status == 'held',
                    OfFansProjection.detail.in_(_REPROJECTABLE_DETAILS), OfFansConnection.binding_id == binding,
                    or_(fan_expr.in_(tuple(map(str, allowed))), fan_expr.is_(None)))
                    .order_by(OfFansProjection.updated_at, OfFansProjection.event_key).limit(50)):
                items.append(_item('held_projection', projection_resolution_key(binding, epoch_, key_), 'operator',
                    'reproject_held_event', 'A supported provider event is held (' + str(detail_) + '). Re-drive it '
                    'once its missing evidence exists; this grants no send authority.',
                    eventEpoch=epoch_, eventKey=key_, scopeRevision=scope.revision))
            for row in s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
                    OfFansEvidence.kind == 'human_sent', OfFansEvidence.status == 'held',
                    OfFansEvidence.reason == 'queued_send_to_engine_owned_fan',
                    OfFansEvidence.fan_id.in_(allowed)).order_by(OfFansEvidence.created_at).limit(50)):
                cid, revision = conv(row.fan_id)
                items.append(_item('held_send_evidence', row.evidence_id, 'operator', 'review_queued_send',
                    f'A queued send reached fan {row.fan_id}. Only that fan is held until reviewed.',
                    cid, revision, row.created_at, evidenceId=row.evidence_id))

        if scope is not None:
            from .pilot_scope import unresolved_webhook_digests, digest_resolution_key
            from db.onlyfans_engine_models import OfEnginePilotWebhookDigest
            digests = list(s.scalars(unresolved_webhook_digests(runtime).order_by(
                OfEnginePilotWebhookDigest.received_at, OfEnginePilotWebhookDigest.event_key,
                OfEnginePilotWebhookDigest.payload_sha256).limit(201)))
            for row in digests[:200]:
                ident = digest_resolution_key(binding, row.epoch, row.event_key, row.payload_sha256)
                items.append(_item('provider_event', ident, 'operator', 'classify_exact_provider_evidence',
                    'A retained provider event holds sends. Supply the exact original event and review evidence; '
                    'only documented irrelevant evidence can be resolved here.', created=row.received_at,
                    eventEpoch=row.epoch, eventKey=row.event_key, payloadSha256=row.payload_sha256,
                    scopeRevision=scope.revision))
            if len(digests) > 200:
                items.append(_item('provider_event_backlog', 'more', 'operator', 'review_remaining_provider_events',
                    'More retained provider holds remain beyond the 200 shown. Resolving one does not clear others.'))

        if unscoped_provider_evidence:
            items.append(_item('unscoped_provider_evidence', 'review', 'operator', 'review_evidence_scope',
                               'Provider evidence cannot be safely attributed to the current creator binding. '
                               'Preserve it for review.'))
        # Model calls with uncertain usage or unverified identity stay visible.
        # They are informational here: recording a note cannot settle a cost.
        for call in s.scalars(select(OfEngineModelCall).where(OfEngineModelCall.account_id == account,
                OfEngineModelCall.binding_id == binding, OfEngineModelCall.fan_id.in_(pilot),
                (OfEngineModelCall.status.in_(('reserved', 'calling', 'unknown'))) |
                (OfEngineModelCall.outcome.in_(('identity_pending', 'identity_unverified'))))
                .order_by(OfEngineModelCall.created_at).limit(200)):
            cid, revision = conv(call.fan_id)
            identity = call.outcome in ('identity_pending', 'identity_unverified')
            summary = ('The model response identity is not verified. Do not use its text; review the recorded call.'
                       if identity else 'A model call is in progress or its usage is uncertain. '
                       'Review the call and cost before retrying; do not assume it was free.')
            if call.status=='unknown' and call.usage_known and not identity:
                summary='Provider cost is verified; the model outcome remains uncertain. No retry or send was authorized.'
            items.append(_item('model_call', call.call_id, 'operator',
                               'review_model_identity' if identity else 'review_model_outcome' if call.usage_known else 'review_model_usage',
                               summary, cid, revision, call.created_at,
                               usageKnown=call.usage_known, providerGenerationId=call.provider_generation_id,
                               providerEvidenceAvailable=bool(call.provider_generation_id),
                               actualMicrousd=call.actual_microusd if call.usage_known else None))
        # Requests with no live approval remain visible, including ones waiting
        # for a fan or provider; displaying one never starts a follow-up.
        shown_drafts = {request_id for (request_id,) in s.execute(select(OfEngineApproval.request_id).where(
            OfEngineApproval.account_id == account, OfEngineApproval.binding_id == binding,
            OfEngineApproval.status.in_(('awaiting', 'approved'))))}
        for request in s.scalars(select(OfEngineRequest).where(OfEngineRequest.account_id == account,
                OfEngineRequest.binding_id == binding, OfEngineRequest.status.in_(('open', 'waiting_fan', 'waiting_provider')),
                True if allowed is None else OfEngineRequest.fan_id.in_(allowed))
                .order_by(OfEngineRequest.created_at).limit(200)):
            if allowed is not None and request.fan_id not in allowed:
                continue
            if request.request_id in shown_drafts:
                continue
            cid, revision = conv(request.fan_id)
            items.append(_item('open_request', request.request_id, (request.owner or 'operator')[:64],
                               (request.next_step or 'review_request')[:128],
                               f'Request for fan {request.fan_id} is {request.status}.', cid, revision,
                               request.created_at, requestId=request.request_id, requestRevision=request.revision))
        # 4. Conversations a human holds, waiting for an explicit hand back.
        for state, owner in s.execute(select(OfConversationState, OfEngineOwner).join(OfEngineOwner, and_(
                OfEngineOwner.account_id == OfConversationState.account_id, OfEngineOwner.fan_id == OfConversationState.fan_id))
                .where(OfConversationState.account_id == account, OfEngineOwner.binding_id == binding,
                       OfConversationState.mode == 'human_controlled',
                        True if allowed is None else OfConversationState.fan_id.in_(allowed)).order_by(OfConversationState.updated_at).limit(200)):
            if allowed is not None and state.fan_id not in allowed:
                continue
            waiting = state.takeover_state == 'requested'
            items.append(_item('human_hold', state.fan_id, 'human',
                               'await_delivery_resolution_then_handback' if waiting else 'awaiting_handback',
                               f'A human holds fan {state.fan_id}. The engine is silent until an explicit hand back; '
                               'a hand back only allows a fresh plan.', conversation_id_for(account, state.fan_id),
                               state.revision, state.updated_at))
        # 5. Requests in a handover state (resolved through the request workflow: accept, then complete or decline).
        for request in s.scalars(select(OfEngineRequest).where(OfEngineRequest.account_id == account,
                OfEngineRequest.binding_id == binding,
                OfEngineRequest.status.in_(('handover_pending', 'handover_accepted')),
                True if allowed is None else OfEngineRequest.fan_id.in_(allowed)).order_by(OfEngineRequest.updated_at).limit(200)):
            if allowed is not None and request.fan_id not in allowed:
                continue
            cid, revision = conv(request.fan_id)
            items.append(_item('handover_request', request.request_id, 'human',
                               'accept_handover' if request.status == 'handover_pending' else 'complete_or_decline_with_evidence',
                               f'Request for fan {request.fan_id} is {request.status}.', cid, revision, request.updated_at,
                               requestId=request.request_id, requestRevision=request.revision))
    return items[:500]


# ------------------------------------------------------------------ reconcile
def _reconcile_provider_event(store, tenant, account, item_id, body):
    """Exact retained bytes and documented semantics, never a blanket clear/retry."""
    from db.onlyfans_engine_models import OfEnginePilotWebhookDigest, OfFansConnection, OfFansWebhook
    from fans_api_adapter.events import _parse
    from fans_api_adapter.event_scope import classify_body, IRRELEVANT, MATERIAL, COUNTER_EVENTS
    from .pilot_scope import require, authorize, digest_resolution_key
    required = {'commandKey','actor','evidenceRef','scopeRevision','eventEpoch','eventKey','payloadSha256','rawBody'}
    if type(body) is not dict or set(body) != required:
        raise BadRequest('exact provider-event evidence fields required')
    key, actor = _key(body['commandKey']), _actor(body['actor'])
    revision, epoch = _int(body['scopeRevision'], 1), _int(body['eventEpoch'], 1, 2**63-1)
    event_key, digest = body['eventKey'], body['payloadSha256']
    if (type(event_key) is not str or not re.fullmatch(r'[\x21-\x7e]{1,255}',event_key)
            or type(digest) is not str or not _HEX64.fullmatch(digest)
            or type(body['rawBody']) is not str or not evidence_allowed(PLAN_C_TRANSPORT,body['evidenceRef'])):
        raise BadRequest('exact provider-event evidence required')
    try:
        raw = body['rawBody'].encode('utf-8')
    except UnicodeError:
        raise BadRequest('invalid original provider bytes') from None
    if not raw or len(raw) > 8192:
        raise BadRequest('original provider evidence exceeds the operator command boundary')
    binding = _require_gated(store, tenant, account)
    with Session(store.engine) as s:
        runtime = store._runtime(s, tenant, account, lock=False)
        _, config = require(s, runtime)
        authorize(config, actor, 'reconcile')
    ident = digest_resolution_key(binding, epoch, event_key, digest)
    if item_id != 'provider_event:' + ident or key == ident:
        raise BadRequest('provider-event identity mismatch')
    request_hash = _digest([account, item_id, body])
    before = {'id':item_id,'eventEpoch':epoch,'eventKey':event_key,'payloadSha256':digest,'scopeRevision':revision}
    def run():
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            if runtime.binding_id != binding:
                raise Conflict('scope changed')
            scope, config = require(s, runtime)
            authorize(config, actor, 'reconcile')
            if scope.revision != revision:
                raise Conflict('pilot revision changed')
            connection = s.get(OfFansConnection,(account,epoch))
            row = s.get(OfEnginePilotWebhookDigest,(account,epoch,event_key,digest))
            if connection is None or connection.binding_id != binding or row is None:
                raise NotFound('retained provider event unavailable')
            if hashlib.sha256(raw).hexdigest() != digest:
                raise Conflict('original provider bytes do not match retained evidence')
            try:
                parsed = _parse(raw)
            except Refused:
                raise Conflict('malformed provider evidence remains held') from None
            verdict = classify_body(parsed)
            if parsed['account_id'] != connection.provider_account_id or verdict.cls not in (IRRELEVANT, MATERIAL):
                raise Conflict('evidence does not prove a documented irrelevant or reviewable fan event')
            conflict = s.scalar(select(OfEnginePilotWebhookDigest.payload_sha256).where(
                OfEnginePilotWebhookDigest.account_id==account,OfEnginePilotWebhookDigest.epoch==epoch,
                OfEnginePilotWebhookDigest.event_key==event_key,
                OfEnginePilotWebhookDigest.payload_sha256!=digest).limit(1))
            original = s.get(OfFansWebhook,(account,epoch,event_key))
            if conflict is not None or original is not None and original.payload_sha256 != digest:
                raise Conflict('conflicting provider key remains held')
            prior = s.get(OfLaunchCommand,(account,ident))
            material = verdict.cls == MATERIAL
            result = {'ok':True,'accountId':account,'id':item_id,'action':'reconcile','commandKey':key,
                'outcome':'material_event_reviewed' if material else 'documented_irrelevant_event_classified','resolved':True}
            if prior is not None:
                if prior.binding_id != binding or prior.kind != 'plan_c_webhook_resolution':
                    raise Conflict('provider resolution identity conflict')
                return {**result,'outcome':'already_classified'}
            if row.safety_hold is not True:
                raise Conflict('provider event is not awaiting classification')
            _record(s,account,binding,ident,'plan_c_webhook_resolution',request_hash,actor,body['evidenceRef'],
                {**before,'classification':('material_fan_event' if material else 'documented_irrelevant_counter'
                    if parsed['event'] in COUNTER_EVENTS else 'documented_irrelevant_event'),
                 'eventType':parsed['event'],'fan':verdict.fan},result)
            return result
    return _idempotent(store,account,binding,key,'plan_c_reconcile',request_hash,actor,body['evidenceRef'],before,run)


_REPROJECTABLE_DETAILS = ('deletion_mapping_missing_or_ambiguous', 'deletion_binding_unproven',
                          'deletion_epoch_unproven', 'pilot_fan_scope_changed')


def projection_resolution_key(binding, epoch, event_key):
    return hashlib.sha256(f'plan-c-projection-resolve:{binding}:{epoch}:{hashlib.sha256(event_key.encode()).hexdigest()}'.encode()).hexdigest()


def _reconcile_held_projection(store, tenant, account, item_id, body):
    """Re-drive one held projection of a SUPPORTED event from its retained exact bytes.

    Only holds caused by missing/late mapping evidence (deletion before its message, fan scope that has
    since been restored) can be re-driven; conflicts, historical bindings, challenge recovery, payment and
    unknown-recipient holds are refused. Re-driving runs the normal projection owner again, so it can only
    succeed when the evidence is now present, and it creates no send, model or policy authority.
    """
    from db.onlyfans_engine_models import OfFansConnection, OfFansWebhook, OfFansWebhookConflict, OfEnginePilotWebhookDigest
    from db.onlyfans_fans_intake_models import OfFansProjection
    from fans_api_adapter.event_scope import classify_raw, SUPPORTED
    from fans_api_adapter.events import FansEventIntake
    from .pilot_scope import require, authorize
    required = {'commandKey', 'actor', 'evidenceRef', 'scopeRevision', 'eventEpoch', 'eventKey'}
    if type(body) is not dict or set(body) != required:
        raise BadRequest('exact held-projection evidence fields required')
    key, actor = _key(body['commandKey']), _actor(body['actor'])
    revision, epoch = _int(body['scopeRevision'], 1), _int(body['eventEpoch'], 1, 2**63-1)
    event_key = body['eventKey']
    if (type(event_key) is not str or not re.fullmatch(r'[\x21-\x7e]{1,255}', event_key)
            or not evidence_allowed(PLAN_C_TRANSPORT, body['evidenceRef'])):
        raise BadRequest('exact held-projection evidence required')
    binding = _require_gated(store, tenant, account)
    with Session(store.engine) as s:
        runtime = store._runtime(s, tenant, account, lock=False)
        _, config = require(s, runtime)
        authorize(config, actor, 'reconcile')
    if item_id != 'held_projection:' + projection_resolution_key(binding, epoch, event_key):
        raise BadRequest('held-projection identity mismatch')
    request_hash = _digest([account, item_id, body])
    before = {'id': item_id, 'eventEpoch': epoch, 'eventKey': event_key, 'scopeRevision': revision}

    def run():
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            if runtime.binding_id != binding:
                raise Conflict('scope changed')
            scope, config = require(s, runtime)
            authorize(config, actor, 'reconcile')
            if scope.revision != revision:
                raise Conflict('pilot revision changed')
            connection = s.get(OfFansConnection, (account, epoch))
            proj = s.get(OfFansProjection, (account, epoch, event_key), with_for_update=True)
            raw = s.get(OfFansWebhook, (account, epoch, event_key))
            if connection is None or connection.binding_id != binding or proj is None or raw is None:
                raise NotFound('held projection unavailable')
            if proj.status != 'held' or proj.detail not in _REPROJECTABLE_DETAILS:
                raise Conflict('projection is not an evidence-recoverable hold')
            if hashlib.sha256(bytes(raw.raw_body)).hexdigest() != raw.payload_sha256 or raw.status != 'accepted':
                raise Conflict('retained provider bytes do not match')
            conflicting = s.scalar(select(OfFansWebhookConflict.payload_sha256).where(
                OfFansWebhookConflict.account_id == account, OfFansWebhookConflict.epoch == epoch,
                OfFansWebhookConflict.event_key == event_key).limit(1)) or s.scalar(
                select(OfEnginePilotWebhookDigest.payload_sha256).where(
                    OfEnginePilotWebhookDigest.account_id == account, OfEnginePilotWebhookDigest.epoch == epoch,
                    OfEnginePilotWebhookDigest.event_key == event_key,
                    OfEnginePilotWebhookDigest.payload_sha256 != raw.payload_sha256).limit(1))
            if conflicting is not None:
                raise Conflict('conflicting provider key remains held')
            if classify_raw(bytes(raw.raw_body)).cls != SUPPORTED:
                raise Conflict('event is not a supported projection')
            proj.status, proj.detail, proj.updated_at = 'pending', 'operator_reproject', utcnow()
        status = FansEventIntake(store)._project(account, epoch, event_key)
        return {'ok': True, 'accountId': account, 'id': item_id, 'action': 'reconcile', 'commandKey': key,
                'outcome': 'reprojected' if status == 'projected' else 'reprojected_' + str(status),
                'projection': status, 'resolved': status in ('projected', 'ignored')}
    return _idempotent(store, account, binding, key, 'plan_c_reconcile', request_hash, actor, body['evidenceRef'], before, run)


def reconcile(store, tenant, account, item_id, body):
    """Record the operator's checked evidence for one unresolved item.

    unknown_send: audit only. The intent stays unknown and its fan stays held, no key is replaced, nothing is
      resent and it is never marked not-sent: releasing it needs positive provider history (send_recovery.reconcile).
    unlinked_transaction / unreconciled_access on an of_fans_evidence row: a held row (or a stale pending
      transaction) is dismissed with the operator as resolved_by; it is never converted into a grant.
    unreconciled_access on revoked access evidence: audit only (acknowledged, state untouched).
    human_hold / handover_request: refused; they end with hand back or the request workflow."""
    if type(item_id) is str and item_id.startswith('provider_event:'):
        return _reconcile_provider_event(store, tenant, account, item_id, body)
    if type(item_id) is str and item_id.startswith('held_projection:'):
        return _reconcile_held_projection(store, tenant, account, item_id, body)
    if type(body) is not dict or set(body) != {'commandKey', 'actor', 'evidenceRef'}:
        raise BadRequest('exact command keys required')
    key, actor = _key(body['commandKey']), _actor(body['actor'])
    binding = _require_gated(store, tenant, account)
    if type(item_id) is not str or not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', item_id) or ':' not in item_id:
        raise NotFound('item unavailable')
    if not evidence_allowed(PLAN_C_TRANSPORT, body['evidenceRef']):
        raise BadRequest('evidence reference required (%s...)' % FANSAPI_EVIDENCE_PREFIX)
    evidence = body['evidenceRef']
    kind, _, ident = item_id.partition(':')
    request_hash = _digest([account, 'reconcile', item_id, actor, evidence])

    def run():
        now = utcnow()
        with Session(store.engine) as s, s.begin():
            try:
                runtime = store._runtime(s, tenant, account)
            except Refused:
                raise NotFound('scope unavailable') from None
            if runtime.binding_id != binding:
                raise Conflict('scope changed')
            from .pilot_scope import current as current_pilot, authorize
            pilot, pilot_config = current_pilot(s, runtime)
            if pilot is not None:
                authorize(pilot_config, actor, 'reconcile')
            allowed = set(pilot_config['fans']) if pilot is not None else None
            outcome = 'recorded'
            if kind == 'unknown_send':
                row = s.get(OfTextIntent, (account, ident))
                if row is None or row.binding_id != binding or row.status not in ('dispatching', 'unknown') or (allowed is not None and row.fan_id not in allowed):
                    raise NotFound('item unavailable')
                outcome = 'evidence_recorded_hold_retained'
            elif kind in ('unlinked_transaction', 'unreconciled_access') and ident.startswith('access-'):
                row = s.get(OfEngineAccessEvidence, (account, ident[len('access-'):]))
                intent = s.get(OfTextIntent, (account, row.intent_id)) if row is not None else None
                if (row is None or intent is None or intent.binding_id != binding or intent.fan_id != row.fan_id
                        or (allowed is not None and row.fan_id not in allowed) or (row.status == 'confirmed' and row.applied)):
                    raise NotFound('item unavailable')
                outcome = 'acknowledged'
            elif kind == 'held_send_evidence':
                # A queued send to an owned fan holds only that fan. An audited operator review releases only
                # this row: no takeover, no other hold and no send authority change.
                from fans_api_adapter.evidence_scope import webhook_binding
                row = s.get(OfFansEvidence, (account, ident), with_for_update=True)
                if (row is None or row.kind != 'human_sent' or webhook_binding(s, row) != binding
                        or (allowed is not None and row.fan_id not in allowed)
                        or row.reason not in ('queued_send_to_engine_owned_fan', 'operator_reconciled_queued_send')):
                    raise NotFound('item unavailable')
                if row.status == 'dismissed':
                    raise Conflict('item is already resolved')
                if row.status != 'held':
                    raise Conflict('item is not ready for reconciliation')
                row.status, row.resolved_by, row.reason, row.updated_at = (
                    'dismissed', actor, 'operator_reconciled_queued_send', now)
                outcome = 'dismissed'
            elif kind in ('unlinked_transaction', 'unreconciled_access'):
                row = s.get(OfFansEvidence, (account, ident), with_for_update=True)
                expected = ('transaction', 'chargeback') if kind == 'unlinked_transaction' else ('unlock',)
                from fans_api_adapter.evidence_scope import webhook_binding
                owner = s.get(OfEngineOwner, (account, row.fan_id)) if row is not None else None
                if (row is None or row.kind not in expected or webhook_binding(s, row) != binding
                        or owner is None or owner.binding_id != binding or (allowed is not None and row.fan_id not in allowed)):
                    raise NotFound('item unavailable')
                if row.status == 'dismissed':
                    raise Conflict('item is already resolved')
                if (row.kind == 'unlock' and row.status == 'applied'
                        and row.reason == 'unlock_confirmed_finance_unlinked' and kind == 'unreconciled_access'):
                    # A note is audited, but it does not invent a transaction
                    # link or remove the still-unresolved financial issue.
                    outcome = 'evidence_recorded_finance_unlinked'
                elif row.status == 'applied':
                    raise Conflict('item is already resolved')
                elif row.status == 'held' or (row.status == 'pending' and row.kind == 'transaction' and row.peer_id is None
                                              and kind == 'unlinked_transaction'):
                    row.status, row.resolved_by, row.reason, row.updated_at = 'dismissed', actor, 'operator_reconciled', now
                    outcome = 'dismissed'
                else:
                    raise Conflict('item is not ready for reconciliation')
            elif kind in ('human_hold', 'handover_request'):
                raise Conflict('use hand back or the request workflow for this item')
            else:
                raise NotFound('item unavailable')
        return {'ok': True, 'accountId': account, 'id': item_id, 'action': 'reconcile', 'commandKey': key,
                'outcome': outcome, 'resolved': outcome in ('dismissed', 'acknowledged')}
    return _idempotent(store, account, binding, key, 'plan_c_reconcile', request_hash, actor, evidence, {'id': item_id}, run)
