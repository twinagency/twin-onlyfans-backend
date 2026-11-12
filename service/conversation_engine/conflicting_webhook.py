"""Operator review of ONE conflicting provider event key that concerns a fan outside the trial.

A provider event key that arrives twice with different payloads re-holds the whole account: both trial fans stay
`pilot_evidence_pending` (see pilot_scope.unresolved_webhook_digests / digest_hold_scope). The existing resolvers cannot
clear it: `_reconcile_provider_event` refuses any conflicting key and the queue review is limited to chat_queue events.

This review follows the same digest-resolution pattern as the queue review (one audited group record plus one
`digest_resolution_key` marker per retained variant, in one transaction) and adds only rules that fail closed:

  * stopped, text-only scope and the named `reconcile` operator, at the revision the operator saw;
  * the key must have at least two retained variants and nothing in of_fans_webhook / of_fans_webhook_conflict
    (anything the pilot path projected or quarantined belongs to its existing recovery owner);
  * the exact bytes of EVERY variant must be proven: retained receipt bytes, or operator-supplied bytes whose SHA-256
    equals the retained digest. No variant is ever assumed;
  * every variant must be a `messages.sent` / `messages.received` event signed for this connection's provider account whose
    documented fan is exactly the fan the operator named, and that fan must be outside the trial;
  * no value anywhere in any variant may equal a trial fan id (a recipient list, an embedded chat id...);
  * money, ownership, subscription and every unknown event type are refused outright, whichever fan they name.

Nothing is merged, edited or deleted: the digests and retained receipts are untouched and the audit record keeps the exact
bytes of both variants. The review releases only the hold created by this one key. It grants no send, activation or
handback authority, and any later variant under the key re-holds the account (pilot_scope keeps that rule).
Dry run by default: the same checks run, nothing is written.
"""
from decimal import Decimal
from hashlib import sha256
import json
import re

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import (OfEnginePilotReconnectReceipt, OfEnginePilotWebhookDigest, OfFansConnection,
                                       OfFansWebhook, OfFansWebhookConflict)
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from .operator import evidence_allowed
from .operator_v2 import (BadRequest, Conflict, NotFound, PLAN_C_TRANSPORT, _actor, _digest, _int, _key, _lock, _prior,
                          _record, _require_gated)
from .pilot_scope import authorize, digest_resolution_key, require, unresolved_webhook_digests

KIND_REVIEW = 'plan_c_conflict_review'
KIND_MARKER = 'plan_c_conflict_marker'
OUTCOME = 'conflicting_event_group_reviewed'
ALLOWED_EVENTS = ('messages.sent', 'messages.received')
MAX_VARIANTS = 16
MAX_VARIANT_BYTES = 65536
_EVENT_KEY = re.compile(r'[\x21-\x7e]{1,255}')


def _body(raw):
    from fans_api_adapter.events import _parse
    from onlyfans_runtime.store import Refused
    try:
        return _parse(raw)
    except Refused:
        raise Conflict('a variant is not a valid signed provider envelope') from None


def _mentions(value, fans):
    """True when any key or value anywhere in the body equals a trial fan id (as a number or decimal text)."""
    if isinstance(value, dict):
        return any(_mentions(k, fans) or _mentions(v, fans) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_mentions(v, fans) for v in value)
    if type(value) is bool or value is None:
        return False
    if isinstance(value, (int, Decimal)):
        return any(value == fan for fan in fans)
    if isinstance(value, str):
        return value.strip().isdecimal() and any(int(value.strip()) == fan for fan in fans)
    return False


def _classify_variant(raw, provider_account, fan, trial_fans):
    """The variant's event name, or Conflict. Every rule here refuses; none relaxes another owner's rule."""
    from fans_api_adapter.event_scope import classify_body, SUPPORTED
    body = _body(raw)
    if body['account_id'] != provider_account:
        raise Conflict('a variant is signed for another provider account')
    event = body.get('event')
    if event not in ALLOWED_EVENTS:
        raise Conflict('only messages.sent or messages.received conflicts can be reviewed here; '
                       'money, ownership, subscription and unknown events stay held (' + str(event)[:60] + ')')
    verdict = classify_body(body)
    if verdict.cls != SUPPORTED or verdict.fan is None:
        raise Conflict('a variant does not have the documented shape of an event for one identified fan')
    if verdict.fan in trial_fans:
        raise Conflict('a variant concerns a trial fan')
    if verdict.fan != fan:
        raise Conflict('a variant concerns a different fan than the one named')
    if _mentions(body, trial_fans):
        raise Conflict('a variant refers to a trial fan')
    return event


def _canonical(obj):
    return [json.dumps(obj, ensure_ascii=a, sort_keys=s, separators=sep).encode()
            for a in (True, False) for s in (True, False) for sep in (None, (',', ':'))]


def _supplied(variants, bodies):
    """Map payload digest -> (bytes, how). Supplied bytes must hash to a retained digest, exactly or as one of the
    canonical serializations of the provider's logged object; anything else is refused, never guessed."""
    found = {}
    for raw in bodies:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_VARIANT_BYTES:
            raise BadRequest('supplied variant bytes must be 1 to 65536 bytes')
        digest, how = sha256(raw).hexdigest(), 'operator_supplied'
        if digest not in variants:
            try:
                candidates = [c for c in _canonical(json.loads(raw)) if sha256(c).hexdigest() in variants]
            except (ValueError, RecursionError, UnicodeError):
                candidates = []
            if not candidates:
                raise BadRequest('supplied bytes match no retained variant digest')
            raw, how = candidates[0], 'operator_supplied_reconstructed'
            digest = sha256(raw).hexdigest()
        if digest in found:
            raise BadRequest('the same variant was supplied twice')
        found[digest] = (raw, how)
    return found


def _unresolved_keys(s, runtime):
    from db.onlyfans_engine_models import OfEnginePilotWebhookDigest as Row
    return {(e, k) for e, k in s.execute(unresolved_webhook_digests(runtime).with_only_columns(Row.epoch, Row.event_key)).all()}


def review(store, tenant, account, *, event_key, event_epoch, expected_fan, scope_revision, command_key, actor,
           evidence_ref, variant_bodies=(), apply=False):
    """Dry run by default. With apply=True: record the audited review and release this one key's hold."""
    if type(apply) is not bool:
        raise BadRequest('apply must be an explicit boolean')
    key, actor = _key(command_key), _actor(actor)
    revision, epoch, fan = _int(scope_revision, 1), _int(event_epoch, 1, 2**63 - 1), _int(expected_fan, 1, 2**63 - 1)
    if type(event_key) is not str or not _EVENT_KEY.fullmatch(event_key):
        raise BadRequest('exact event key required')
    if not evidence_allowed(PLAN_C_TRANSPORT, evidence_ref):
        raise BadRequest('operator evidence statement required')
    if type(variant_bodies) not in (list, tuple):
        raise BadRequest('variant bytes must be a list')
    binding = _require_gated(store, tenant, account)
    request_hash = _digest([account, event_key, epoch, fan, revision, actor, evidence_ref,
                            sorted(sha256(b).hexdigest() for b in variant_bodies if type(b) is bytes)])

    def run():
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            if runtime.binding_id != binding:
                raise Conflict('scope changed')
            scope, config = require(s, runtime, lock=True)
            authorize(config, actor, 'reconcile')
            if apply:
                prior = _prior(s, account, key, KIND_REVIEW, request_hash)
                if prior is not None:
                    return {**prior, 'mode': 'replayed', 'replayed': True, 'applied': True}
            policy = s.get(OfDeliveryPolicy, account)
            if (scope.status != 'stopped' or scope.revision != revision or runtime.enabled or policy is None or policy.enabled
                    or config.get('delivery_mode') != 'text_only'):
                raise Conflict('conflicting-event review requires the current stopped text-only scope at the revision seen')
            connection = s.get(OfFansConnection, (account, epoch))
            if connection is None or connection.binding_id != binding or connection.status != 'active':
                raise Conflict('provider connection changed')
            trial_fans = frozenset(config['fans'])
            if fan in trial_fans:
                raise Conflict('the named fan is a trial fan')
            retained = list(s.scalars(select(OfEnginePilotWebhookDigest).where(
                OfEnginePilotWebhookDigest.account_id == account, OfEnginePilotWebhookDigest.epoch == epoch,
                OfEnginePilotWebhookDigest.event_key == event_key).order_by(OfEnginePilotWebhookDigest.payload_sha256)))
            if not retained:
                raise NotFound('no retained evidence under that event key')
            if len(retained) < 2:
                raise Conflict('not a conflicting key: only one retained variant')
            if len(retained) > MAX_VARIANTS:
                raise Conflict('too many variants for one review')
            if (s.get(OfFansWebhook, (account, epoch, event_key)) is not None or s.scalar(select(OfFansWebhookConflict.payload_sha256).where(
                    OfFansWebhookConflict.account_id == account, OfFansWebhookConflict.epoch == epoch,
                    OfFansWebhookConflict.event_key == event_key).limit(1)) is not None):
                raise Conflict('projected or quarantined event requires its existing recovery owner')
            if (epoch, event_key) not in _unresolved_keys(s, runtime):
                raise Conflict('this key holds nothing: it is already reviewed or has no unresolved variant')
            digests = {r.payload_sha256 for r in retained}
            receipts = {r.payload_sha256: bytes(r.raw_body) for r in s.scalars(select(OfEnginePilotReconnectReceipt).where(
                OfEnginePilotReconnectReceipt.account_id == account, OfEnginePilotReconnectReceipt.epoch == epoch,
                OfEnginePilotReconnectReceipt.event_key == event_key))}
            supplied = _supplied(digests, variant_bodies)
            variants, raws = [], {}
            for digest in sorted(digests):
                kept = receipts.get(digest)
                if kept is not None and sha256(kept).hexdigest() != digest:
                    raise Conflict('retained bytes do not match their digest')
                raw, how = (kept, 'retained') if kept is not None else supplied.get(digest, (None, None))
                if raw is None:
                    raise Conflict('exact bytes of variant ' + digest[:12] + ' are not retained: supply them, never assumed')
                event = _classify_variant(raw, connection.provider_account_id, fan, trial_fans)
                raws[digest] = raw
                variants.append({'payloadSha256': digest, 'event': event, 'bytesSource': how, 'bytes': len(raw)})
            try:
                texts = [raws[d].decode('utf-8') for d in sorted(raws)]
            except UnicodeError:
                raise Conflict('a variant is not UTF-8 and cannot be retained in the audit') from None
            others = len(_unresolved_keys(s, runtime) - {(epoch, event_key)})
            plan = {'eventKey': event_key, 'eventEpoch': epoch, 'fan': fan, 'scopeRevision': revision, 'variants': variants,
                    'releases': 'only the hold created by this one conflicting key', 'keepsRetained': 'every variant, unmerged',
                    'sendAuthorized': False, 'activationAuthorized': False, 'resolved': True, 'outcome': OUTCOME}
            if not apply:
                return {**plan, 'mode': 'dry_run', 'applied': False, 'wouldRelease': True, 'replayed': False,
                        'remainingUnresolvedKeys': others}
            markers = {d: digest_resolution_key(binding, epoch, event_key, d) for d in sorted(digests)}
            if key in set(markers.values()):
                raise BadRequest('command key conflicts with an evidence identity')
            for marker in markers.values():
                prior = s.get(OfLaunchCommand, (account, marker))
                if prior is not None:
                    raise Conflict('another resolution already owns a variant of this key')
            result = {**plan, 'mode': 'applied', 'applied': True, 'replayed': False, 'remainingUnresolvedKeys': others}
            before = {'eventEpoch': epoch, 'eventKey': event_key, 'fan': fan, 'scopeRevision': revision,
                      'payloadSha256s': sorted(digests), 'rawBodies': texts,
                      'providerAccount': connection.provider_account_id, 'trialFans': sorted(trial_fans)}
            _record(s, account, binding, key, KIND_REVIEW, request_hash, actor, evidence_ref, before, result)
            for marker in markers.values():
                _record(s, account, binding, marker, KIND_MARKER, request_hash, actor, evidence_ref,
                        {'groupReview': key, 'eventKey': event_key}, result)
            s.flush()
            if (epoch, event_key) in _unresolved_keys(s, runtime):
                raise Conflict('the review did not release the key; nothing was recorded')
            return result

    with _lock(account, key):
        return run()


def reviewed_conflicting_delivery(local, key, payload, provider, trial_fans):
    """True when a provider delivery of a conflicting key is covered by an audited review (interval recovery).

    `local` carries the current immutable digests and the plan_c_conflict_review audit rows. Every currently retained
    variant must be in the reviewed group, each reviewed variant must re-validate as an event for a fan outside the trial,
    and the delivered payload must be one of the reviewed variants. A variant that arrived later invalidates the proof.
    """
    digests = {d['hash'] for d in local.get('digests', []) if d.get('key') == key}
    if len(digests) < 2:
        return False
    for audit in local.get('conflict_reviews', []):
        before, result = audit.get('before', {}), audit.get('result', {})
        if (before.get('eventKey') != key or before.get('providerAccount') != provider or result.get('outcome') != OUTCOME
                or result.get('resolved') is not True or set(before.get('payloadSha256s', [])) != digests):
            continue
        try:
            reviewed_fans = before['trialFans']
            if type(reviewed_fans) is not list or not reviewed_fans or any(type(f) is not int for f in reviewed_fans):
                continue
            fans_all = frozenset(trial_fans) | frozenset(reviewed_fans)     # the review recorded the trial's own fans
            bodies = before['rawBodies']
            if type(bodies) is not list or not 2 <= len(bodies) <= MAX_VARIANTS:
                continue
            seen, fans, matched = set(), set(), False
            for text in bodies:
                raw = text.encode('utf-8')
                digest = sha256(raw).hexdigest()
                if digest not in digests or digest in seen:
                    raise ValueError('reviewed group changed')
                seen.add(digest)
                from fans_api_adapter.event_scope import classify_body, SUPPORTED
                body = _body(raw)
                verdict = classify_body(body)
                if (body['account_id'] != provider or body['event'] not in ALLOWED_EVENTS or verdict.cls != SUPPORTED
                        or verdict.fan is None or verdict.fan in fans_all or _mentions(body, fans_all)):
                    raise ValueError('reviewed variant no longer qualifies')
                fans.add(verdict.fan)
                matched = matched or body == payload
            if seen == digests and len(fans) == 1 and matched:
                return True
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, Conflict):
            continue
    return False
