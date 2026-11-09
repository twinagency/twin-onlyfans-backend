"""Manual, provider-verified recovery of an account authentication challenge.

The signed webhook records the stop; it cannot prove current provider health.
This operation performs one guarded read, audits the exact result, and leaves
the send block in place for a separate authorized operator release.
"""
import json
from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session

from conversation_engine.pilot_scope import ProviderRequestGuard, _command, _fingerprint, authorize, require
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfFansConnection, OfFansWebhook
from db.onlyfans_fans_intake_models import OfFansLink, OfFansProjection
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from .protocol import FansApiClient


_CHALLENGES = ("accounts.otp_code_required", "accounts.face_otp_required")


def _challenge_snapshot(s, account, epoch):
    query = select(func.count(), func.max(OfFansWebhook.received_at),
                   func.max(OfFansWebhook.event_key)).where(
        OfFansWebhook.account_id == account, OfFansWebhook.epoch == epoch,
        OfFansWebhook.event_type.in_(_CHALLENGES))
    count, newest_at, max_key = s.execute(query).one()
    pending = s.scalar(select(OfFansProjection.event_key).join(OfFansWebhook,
        and_(OfFansWebhook.account_id == OfFansProjection.account_id,
             OfFansWebhook.epoch == OfFansProjection.epoch,
             OfFansWebhook.event_key == OfFansProjection.event_key)).where(
        OfFansProjection.account_id == account, OfFansProjection.epoch == epoch,
        OfFansWebhook.event_type.in_(_CHALLENGES),
        OfFansProjection.status.in_(("pending", "held"))).limit(1))
    return count, newest_at, max_key, pending


def recover_current_challenge(store, tenant, account, client, *, expected_revision, command_key, actor, reason):
    """Verify the current account once. No caller-supplied attestation can lift the hold."""
    _command(command_key, actor, reason)
    if type(expected_revision) is not int or expected_revision < 1 or type(client) is not FansApiClient:
        raise Refused("invalid challenge recovery command")
    request_hash = _fingerprint(["fans_auth_recovery", tenant, account, expected_revision, actor, reason])
    # Serialize the same command across processes without holding a runtime lock
    # over network I/O. A competing caller gets a passive refusal, not a second GET.
    with store.engine.connect() as lock:
        got = lock.execute(text("SELECT pg_try_advisory_lock(hashtext(:account),hashtext(:key))"),
                           {"account": account, "key": command_key}).scalar_one()
        if not got:
            raise Refused("challenge recovery command already in progress")
        try:
            with Session(store.engine) as s, s.begin():
                runtime = store._runtime(s, tenant, account)
                scope, config = require(s, runtime, active=True)
                authorize(config, actor, "reconcile")
                if scope.revision != expected_revision:
                    raise Refused("pilot scope changed")
                prior = s.get(OfLaunchCommand, (account, command_key))
                if prior is not None:
                    if prior.kind != "fans_auth_recovery" or prior.request_hash != request_hash:
                        raise Refused("challenge recovery command key conflict")
                    return {**json.loads(prior.result_json), "replayed": True}
                connection = s.scalar(select(OfFansConnection).where(
                    OfFansConnection.account_id == account, OfFansConnection.binding_id == runtime.binding_id,
                    OfFansConnection.status == "active"))
                link = s.get(OfFansLink, (account, connection.provider_creator_id)) if connection else None
                policy = s.get(OfDeliveryPolicy, account)
                if (connection is None or link is None or link.block_reason != "auth_challenge"
                        or not link.send_blocked or link.state != "auth_failed"
                        or policy is None or policy.enabled is not False):
                    raise Refused("current challenge hold is unavailable")
                guard = getattr(client, "_request_guard", None)
                if (type(guard) is not ProviderRequestGuard or guard.store is not store or
                        (guard.tenant, guard.account, guard.revision) != (tenant, account, expected_revision) or
                        (client.account, client.creator_id, client.epoch) !=
                        (connection.provider_account_id, connection.provider_creator_id, connection.epoch)):
                    raise Refused("provider verification is not scoped or budgeted")
                snapshot = _challenge_snapshot(s, account, connection.epoch)
                if not snapshot[0] or snapshot[3] is not None:
                    raise Refused("challenge event is not safely projected")
                stamp = (runtime.binding_id, connection.epoch, connection.provider_account_id,
                         connection.provider_creator_id, link.updated_at, policy.revision, snapshot)
            proof = client.identity()
            if (proof.state != "verified" or proof.http_status != 200 or not client._identity_verified or
                    proof.data != {"account": stamp[2], "creator_id": stamp[3], "epoch": stamp[1]}):
                raise Refused("current provider authentication could not be verified")
            with Session(store.engine) as s, s.begin():
                runtime = store._runtime(s, tenant, account)
                scope, config = require(s, runtime, active=True)
                authorize(config, actor, "reconcile")
                connection = s.get(OfFansConnection, (account, stamp[1]))
                link = s.get(OfFansLink, (account, stamp[3]), with_for_update=True)
                policy = s.get(OfDeliveryPolicy, account)
                if (scope.revision != expected_revision or runtime.binding_id != stamp[0] or
                        connection is None or connection.status != "active" or
                        connection.binding_id != stamp[0] or connection.provider_account_id != stamp[2] or
                        connection.provider_creator_id != stamp[3] or link is None or
                        link.block_reason != "auth_challenge" or link.state != "auth_failed" or
                        not link.send_blocked or link.updated_at != stamp[4] or policy is None or
                        policy.enabled is not False or policy.revision != stamp[5] or
                        _challenge_snapshot(s, account, stamp[1]) != stamp[6]):
                    raise Refused("challenge or binding changed during provider verification")
                prior = s.get(OfLaunchCommand, (account, command_key))
                if prior is not None:
                    if prior.kind != "fans_auth_recovery" or prior.request_hash != request_hash:
                        raise Refused("challenge recovery command key conflict")
                    return {**json.loads(prior.result_json), "replayed": True}
                now = utcnow()
                link.state, link.block_reason, link.updated_at = "connected", "auth_challenge_verified", now
                # Keep send_blocked true. A named operator must separately use
                # the existing release flow; other holds are not modified.
                result = {"status": "provider_verified", "sendBlocked": True,
                          "connectionEpoch": stamp[1], "scopeRevision": expected_revision}
                before = {"challengeCount": stamp[6][0], "providerTraceId": proof.trace_id,
                          "policyRevision": stamp[5], "bindingId": stamp[0]}
                s.add(OfLaunchCommand(account_id=account, command_key=command_key, binding_id=stamp[0],
                    kind="fans_auth_recovery", request_hash=request_hash, actor=actor, reason=reason,
                    before_json=json.dumps(before, sort_keys=True), result_json=json.dumps(result, sort_keys=True),
                    created_at=now))
                return {**result, "replayed": False}
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(hashtext(:account),hashtext(:key))"),
                         {"account": account, "key": command_key})



def _recover_reconnected_evidence(store, tenant, account, client, *, expected_revision,
                                  event_epoch, event_key, raw_body, signature, secret,
                                  command_key, actor, reason):
    """Verify one exact retained new-account reconnect and bind it without clearing other holds."""
    from hashlib import sha256
    from conversation_engine.pilot_scope import digest_resolution_key
    from db.onlyfans_delivery_models import OfTextIntent
    from db.onlyfans_engine_models import (OfEngineIndependentAdmission, OfEnginePilotWebhookDigest,
                                          OfFansWebhookConflict)
    from .events import _parse
    from .webhooks import verify_signature

    _command(command_key, actor, reason)
    if (type(expected_revision) is not int or expected_revision < 1 or
            type(event_epoch) is not int or event_epoch < 1 or type(event_key) is not str or
            not event_key or len(event_key) > 255 or type(client) is not FansApiClient or
            type(raw_body) is not bytes or not 1 <= len(raw_body) <= 1_000_000 or
            not verify_signature(raw_body, signature, secret)):
        raise Refused("invalid reconnect recovery evidence")
    body = _parse(raw_body)
    if body.get("event") not in ("accounts.connected", "accounts.reconnected"):
        raise Refused("retained event is not a reconnect")
    creator = body["payload"].get("onlyfans_id")
    if type(creator) not in (str, int) or str(creator) != client.creator_id or body["account_id"] != client.account:
        raise Refused("reconnect identity conflicts")
    digest = sha256(raw_body).hexdigest()
    request_hash = _fingerprint(["fans_auth_reconnect", tenant, account, expected_revision,
                                 event_epoch, event_key, digest, actor, reason])
    # Serialise both this command and this retained item across workers.
    item_lock = _fingerprint([account, event_epoch, event_key, digest])
    keys = sorted({command_key, item_lock})
    with store.engine.connect() as lock:
        acquired = []
        try:
            for key in keys:
                got = lock.execute(text("SELECT pg_try_advisory_lock(hashtext(:account),hashtext(:key))"),
                                   {"account": account, "key": key}).scalar_one()
                if not got:
                    raise Refused("reconnect recovery is already in progress")
                acquired.append(key)

            def inspect(s, *, expected=None):
                runtime = store._runtime(s, tenant, account)
                scope, config = require(s, runtime, active=True)
                authorize(config, actor, "reconcile")
                if scope.revision != expected_revision:
                    raise Refused("pilot scope changed")
                prior = s.get(OfLaunchCommand, (account, command_key))
                if prior is not None:
                    if prior.kind != "fans_auth_reconnect" or prior.request_hash != request_hash:
                        raise Refused("reconnect recovery command key conflict")
                    return None, {**json.loads(prior.result_json), "replayed": True}
                anchor = s.get(OfFansConnection, (account, event_epoch))
                link = s.get(OfFansLink, (account, str(creator)), with_for_update=True)
                policy = s.get(OfDeliveryPolicy, account)
                retained = s.get(OfEnginePilotWebhookDigest, (account, event_epoch, event_key, digest))
                challenge_hold = (link is not None and link.block_reason == "auth_challenge"
                                  and link.send_blocked and link.state in ("auth_failed", "disconnected"))
                ordinary_link = (link is None or
                    (link.state == "connected" and not link.send_blocked and link.block_reason is None) or
                    (link.state in ("disconnected", "auth_failed") and link.send_blocked
                     and link.block_reason == link.state))
                if (anchor is None or anchor.binding_id != runtime.binding_id or
                        anchor.provider_creator_id != str(creator) or client.account == anchor.provider_account_id or
                        not (challenge_hold or ordinary_link) or
                        policy is None or policy.enabled is not False or retained is None or not retained.safety_hold):
                    raise Refused("exact retained reconnect hold is unavailable")
                latest_epoch = s.scalar(select(func.max(OfFansConnection.epoch)).where(
                    OfFansConnection.account_id == account))
                if latest_epoch != event_epoch:
                    raise Refused("retained reconnect epoch is stale")
                guard = getattr(client, "_request_guard", None)
                if (type(guard) is not ProviderRequestGuard or guard.store is not store or
                        (guard.tenant, guard.account, guard.revision) != (tenant, account, expected_revision) or
                        client.epoch != event_epoch):
                    raise Refused("provider verification is not scoped or budgeted")
                existing = s.scalar(select(OfFansConnection.epoch).where(
                    OfFansConnection.provider_account_id == client.account).limit(1))
                if existing is not None:
                    raise Refused("new provider account is already bound")
                other = s.scalar(select(OfEnginePilotWebhookDigest.payload_sha256).where(
                    OfEnginePilotWebhookDigest.account_id == account,
                    OfEnginePilotWebhookDigest.epoch == event_epoch,
                    OfEnginePilotWebhookDigest.event_key == event_key,
                    OfEnginePilotWebhookDigest.payload_sha256 != digest).limit(1))
                raw_row = s.get(OfFansWebhook, (account, event_epoch, event_key))
                conflict = s.scalar(select(OfFansWebhookConflict.payload_sha256).where(
                    OfFansWebhookConflict.account_id == account,
                    OfFansWebhookConflict.epoch == event_epoch,
                    OfFansWebhookConflict.event_key == event_key).limit(1))
                if other is not None or raw_row is not None or conflict is not None:
                    raise Refused("reconnect evidence has a conflicting event key")
                resolution_key = digest_resolution_key(runtime.binding_id, event_epoch, event_key, digest)
                if command_key == resolution_key or s.get(OfLaunchCommand, (account, resolution_key)) is not None:
                    raise Refused("reconnect evidence is already resolved or conflicts")
                uncertain = s.scalar(select(OfTextIntent.intent_id).where(
                    OfTextIntent.account_id == account, OfTextIntent.binding_id == runtime.binding_id,
                    OfTextIntent.status.in_(("dispatching", "unknown"))).limit(1))
                independent = s.scalar(select(OfEngineIndependentAdmission.attempt_id).where(
                    OfEngineIndependentAdmission.account_id == account,
                    OfEngineIndependentAdmission.status.in_(("dispatching", "unknown"))).limit(1))
                if uncertain is not None or independent is not None:
                    raise Refused("unresolved delivery prevents provider rebind")
                snapshot = _challenge_snapshot(s, account, event_epoch)
                if (challenge_hold and not snapshot[0]) or snapshot[3] is not None:
                    raise Refused("challenge event is not safely projected")
                active = s.scalar(select(OfFansConnection).where(
                    OfFansConnection.account_id == account, OfFansConnection.status == "active"))
                if active is not None and (active.binding_id != runtime.binding_id or
                                           active.provider_creator_id != str(creator) or
                                           active.epoch != event_epoch):
                    raise Refused("active provider connection changed")
                stamp = (runtime.binding_id, anchor.provider_account_id, anchor.status,
                         link.updated_at if link is not None else None, link.state if link is not None else None,
                         policy.revision, snapshot, active.epoch if active is not None else None, resolution_key,
                         link.block_reason if link is not None else None,
                         link.send_blocked if link is not None else None, latest_epoch)
                if expected is not None and stamp != expected:
                    raise Refused("challenge or binding changed during provider verification")
                return stamp, None

            with Session(store.engine) as s, s.begin():
                stamp, prior = inspect(s)
                if prior is not None:
                    return prior
            proof = client.identity()
            if (proof.state != "verified" or proof.http_status != 200 or not client._identity_verified or
                    proof.data != {"account": client.account, "creator_id": str(creator), "epoch": event_epoch}):
                raise Refused("current provider authentication could not be verified")
            with Session(store.engine) as s, s.begin():
                checked, prior = inspect(s, expected=stamp)
                if prior is not None:
                    return prior
                runtime = store._runtime(s, tenant, account)
                active = s.scalar(select(OfFansConnection).where(
                    OfFansConnection.account_id == account, OfFansConnection.status == "active").with_for_update())
                if active is not None:
                    active.status = "retired"
                    s.flush()
                latest = s.scalar(select(func.max(OfFansConnection.epoch)).where(
                    OfFansConnection.account_id == account)) or 0
                now = utcnow()
                new_epoch = latest + 1
                s.add(OfFansConnection(account_id=account, epoch=new_epoch, binding_id=stamp[0],
                    provider_account_id=client.account, provider_creator_id=str(creator),
                    status="active", created_at=now))
                link = s.get(OfFansLink, (account, str(creator)), with_for_update=True)
                if link is None:
                    link = OfFansLink(account_id=account, creator_id=str(creator),
                        provider_account_id=client.account, state="connected", send_blocked=True,
                        session_refreshes=0, reconnect_count=0, updated_at=now)
                    s.add(link)
                link.provider_account_id, link.state = client.account, "connected"
                link.block_reason = ("auth_challenge_verified" if stamp[9] == "auth_challenge"
                                     else "provider_reconnect_verified")
                link.send_blocked, link.updated_at = True, now
                link.reconnect_count += 1
                result = {"status":"provider_verified_reconnect", "sendBlocked":True,
                          "oldEpoch":event_epoch, "connectionEpoch":new_epoch, "scopeRevision":expected_revision}
                before = {"bindingId":stamp[0], "eventEpoch":event_epoch, "eventKey":event_key,
                          "payloadSha256":digest, "providerTraceId":proof.trace_id}
                for key, kind in ((command_key, "fans_auth_reconnect"),
                                  (stamp[8], "plan_c_webhook_resolution")):
                    s.add(OfLaunchCommand(account_id=account, command_key=key, binding_id=stamp[0],
                        kind=kind, request_hash=request_hash, actor=actor, reason=reason,
                        before_json=json.dumps(before,sort_keys=True),
                        result_json=json.dumps(result,sort_keys=True), created_at=now))
                return {**result, "replayed":False}
        finally:
            for key in reversed(acquired):
                lock.execute(text("SELECT pg_advisory_unlock(hashtext(:account),hashtext(:key))"),
                             {"account":account, "key":key})


def recover_reconnected_challenge(store, tenant, account, client, *, expected_revision,
                                  event_epoch, event_key, payload_sha256, secret,
                                  command_key, actor, reason):
    """Load exact authenticated receipt; provider proof remains independently required."""
    from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt
    import re
    if (type(payload_sha256) is not str or not re.fullmatch(r"[a-f0-9]{64}", payload_sha256)
            or type(event_epoch) is not int or event_epoch < 1
            or type(event_key) is not str or not event_key or len(event_key) > 255):
        raise Refused("invalid reconnect receipt identity")
    with Session(store.engine) as s:
        receipt = s.get(OfEnginePilotReconnectReceipt, (account, event_epoch, event_key, payload_sha256))
        if receipt is None:
            raise Refused("retained reconnect receipt is unavailable")
        raw, signature = bytes(receipt.raw_body), receipt.signature
    from hashlib import sha256
    if sha256(raw).hexdigest() != payload_sha256:
        raise Refused("retained reconnect receipt digest mismatch")
    return _recover_reconnected_evidence(store, tenant, account, client,
        expected_revision=expected_revision, event_epoch=event_epoch, event_key=event_key,
        raw_body=raw, signature=signature, secret=secret,
        command_key=command_key, actor=actor, reason=reason)
