"""One transaction, one scoped credential, immutable command acknowledgement.

The acknowledgement identifies a recorded event, never current conversation
state. Read the canonical conversation again after a command or a lost reply.
"""
import asyncio
import hmac
import json
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from onlyfans_read.contract import ID_PATTERN, conversation_id_for, fan_id_from_conversation_id
from onlyfans_read.agency_scope import ACTOR_PATTERN, actor_allowed, current_scope
from onlyfans_runtime.store import Refused
from .settings import ACTIONS, HEADER, PREFIX, SCHEMA, configured, manual_configured

router = APIRouter(prefix=PREFIX, tags=["onlyfans-controls"])
log = logging.getLogger("onlyfans.controls")


def response(body, status=200):
    return JSONResponse({"schemaVersion": SCHEMA, **body}, status_code=status,
                        headers={"Cache-Control": "private, no-store", HEADER: SCHEMA})


def refused(code, status, message):
    return response({"ok": False, "code": code, "error": message}, status)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


@router.post("/accounts/{account_id}/bulk-conversations")
async def bulk_control(request: Request, account_id: str):
    from onlyfans_runtime.bulk_controls import ACTIONS as BULK_ACTIONS, validate_items, apply_batch
    config = configured()
    if config is None:
        return refused("controls_unavailable", 503, "Conversation controls are not configured.")
    token, tenant = config
    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return refused("unauthorized", 401, "The control credential was not accepted.")
    if request.headers.get(HEADER) != SCHEMA:
        return refused("unsupported_schema", 400, "The control contract version is required.")
    store = getattr(request.app.state, "control_store", None)
    if store is None:
        return refused("controls_unavailable", 503, "Conversation controls are not available.")
    try:
        if not ID_PATTERN.fullmatch(account_id) or request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ValueError()
        raw = bytearray()
        async for part in request.stream():
            if len(raw) + len(part) > 65536:
                return refused("request_too_large", 413, "The control request is too large.")
            raw.extend(part)
        body = json.loads(raw, object_pairs_hook=unique_keys)
        if type(body) is not dict or set(body) != {"action", "items", "commandKey", "actor"}:
            raise ValueError()
        action, key, actor = body["action"], body["commandKey"], body["actor"]
        if (type(action) is not str or action not in BULK_ACTIONS or type(key) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", key) or not actor_allowed(actor)):
            raise ValueError()
        items = validate_items(account_id, body["items"])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return refused("invalid_request", 400, "That bulk conversation request is invalid.")
    try:
        results = await asyncio.to_thread(apply_batch, store, tenant, account_id,
            action=action, items=items, command_key=key, actor=actor)
    except Refused:
        return refused("scope_unavailable", 409, "The bulk change could not be completed. Refresh to check recorded changes.")
    return response({"ok": True, "accountId": account_id, "action": action,
        "commandKey": key, "results": results})


@router.post("/accounts/{account_id}/conversations/{conversation_id}")
async def control(request: Request, account_id: str, conversation_id: str):
    config = configured()
    if config is None:
        return refused("controls_unavailable", 503, "Conversation controls are not configured.")
    token, tenant = config
    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return refused("unauthorized", 401, "The control credential was not accepted.")
    if request.headers.get(HEADER) != SCHEMA:
        return refused("unsupported_schema", 400, "The control contract version is required.")
    store = getattr(request.app.state, "control_store", None)
    if store is None:
        return refused("controls_unavailable", 503, "Conversation controls are not available.")
    try:
        if not ID_PATTERN.fullmatch(account_id):
            raise ValueError()
        fan = fan_id_from_conversation_id(conversation_id, account_id)
        if fan is None or not 0 < fan < 2**63 or conversation_id_for(account_id, fan) != conversation_id:
            raise ValueError()
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ValueError()
        raw = bytearray()
        async for part in request.stream():
            if len(raw) + len(part) > 65536:
                return refused("request_too_large", 413, "The control request is too large.")
            raw.extend(part)
        def nonfinite(_):
            raise ValueError("nonfinite number")
        body = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=nonfinite)
        manual = type(body) is dict and body.get("action") == "manual_message"
        resolution = type(body) is dict and body.get("action") == "resolve_delivery_evidence"
        keys = {"action", "expectedRevision", "commandKey", "actor"} | ({"body"} if manual else {"attemptId", "providerMessageId", "decision"} if resolution else set())
        if manual and "media" in body:
            keys.add("media")
        if manual and "catalogItemId" in body:
            keys.add("catalogItemId")
        if not manual and len(raw) > 4096:
            return refused("request_too_large", 413, "The control request is too large.")
        if type(body) is not dict or set(body) != keys:
            raise ValueError()
        action, revision, key, actor = (body[k] for k in ("action", "expectedRevision", "commandKey", "actor"))
        if (type(action) is not str or action not in {*ACTIONS, "manual_message", "resolve_delivery_evidence"} or type(revision) is not int or
                not 0 <= revision < 2**31-1 or type(key) is not str or not re.fullmatch(r"[a-f0-9]{64}", key) or
                not actor_allowed(actor, capability="chat" if action in {"manual_message", "request_takeover", "release_takeover"} else "manage")):
            raise ValueError()
        if resolution and (body['decision']!='delivered' or type(body['attemptId']) is not str
                or not re.fullmatch(r'[A-Za-z0-9-]{1,128}',body['attemptId'])
                or type(body['providerMessageId']) is not int or not 0<body['providerMessageId']<2**63):raise ValueError()
        if manual and (type(body["body"]) is not str or (not body["body"].strip() and "media" not in body) or "\x00" in body["body"]
                or len(body["body"].encode()) > 4000):
            raise ValueError()
        if manual and "media" in body:
            from onlyfans_runtime.manual_media import normalize
            if body.get("catalogItemId") is not None:
                raise ValueError()
            try:
                body["media"] = normalize(body["media"])
            except Refused:
                raise ValueError()
        if manual and body.get("catalogItemId") is not None and (
                type(body["catalogItemId"]) is not int or not 0 < body["catalogItemId"] < 2**31):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return refused("invalid_request", 400, "That conversation control request is invalid.")
    try:
        if resolution:
            scope=current_scope.get()
            if scope is not None and not scope.can_manage:return refused('forbidden',403,'Management permission is required.')
            from onlyfans_runtime.delivery_resolution import resolve
            from onlyfans_runtime.delivery_resolution_jobs import available_for
            if not await asyncio.to_thread(available_for,store,tenant,account_id):return refused('resolution_unavailable',503,'Delivery reconciliation is not configured.')
            await resolve(store,tenant,account_id,fan,attempt=body['attemptId'],provider_message_id=body['providerMessageId'],
                expected_revision=revision,command_key=key,actor=actor)
            return response({'ok':True,'applied':True,'commandKey':key,'accountId':account_id,'conversationId':conversation_id,
                'action':action,'recordedRevision':revision})
        if manual:
            if manual_configured() is None:
                return refused("manual_unavailable", 503, "Manual messaging is not configured.")
            from onlyfans_runtime.text_delivery import TextAuthority
            intent = await asyncio.to_thread(TextAuthority(store).prepare, tenant, account_id, fan,
                command_key=key, expected_revision=revision, body=body["body"], origin="manual", actor=actor,
                catalog_item_id=body.get("catalogItemId"), manual_media=body.get("media"))
            return response({"ok": True, "applied": True, "commandKey": key, "accountId": account_id,
                "conversationId": conversation_id, "action": action, "recordedRevision": revision,
                "intentId": intent, "outcome": "queued"})
        recorded = await asyncio.to_thread(store.control, tenant, account_id, fan, mode=ACTIONS[action],
            expected_revision=revision, command_key=key, actor=actor, action=action,
            reason="Dashboard operator: " + action)
    except Refused as exc:
        if str(exc) in {"scope unavailable", "conversation unavailable"}:
            return refused("scope_unavailable", 404, "That conversation is not available.")
        from .refusals import public_refusal
        code, message = public_refusal(str(exc))
        current_revision = None
        holds = [code]
        try:
            from sqlalchemy.orm import Session
            from db.models import OfConversationState
            from db.onlyfans_models import OfRuntime
            with Session(store.engine) as session:
                state = session.get(OfConversationState, (account_id, fan))
                runtime = session.get(OfRuntime, account_id)
                current_revision = state.revision if state else None
                if runtime and not runtime.enabled:holds.append('account_disabled')
                if state and state.unresolved_attempt_id:holds.append('delivery_unresolved')
                if state and state.takeover_state == 'requested':holds.append('takeover_pending')
        except Exception:pass
        log.info("control_refused account=%s fan=%s action=%s expected_revision=%s current_revision=%s holds=%s command_key=%s", account_id, fan, action, revision, current_revision, sorted(set(holds)), key)
        return response({'ok':False,'code':code,'error':message,'expectedRevision':revision,'currentRevision':current_revision,'holds':sorted(set(holds))},409)
    except Exception:
        log.exception("conversation control acknowledgement unavailable")
        return refused("outcome_unconfirmed", 500, "The outcome is not confirmed. Refresh or retry the same request.")
    return response({"ok": True, "applied": True, "commandKey": key, "accountId": account_id,
                     "conversationId": conversation_id, "action": action, "recordedRevision": recorded})


CASE_KEYS = {"action", "commandKey", "actor"}


@router.post("/accounts/{account_id}/operator-cases/{case_id}")
async def operator_case_action(request: Request, account_id: str, case_id: str):
    """Acknowledge or resolve one conversation-hold case. Records the outcome only: resuming automation stays the
    audited conversation control, and a send is never made here."""
    config = configured()
    if config is None:
        return refused("controls_unavailable", 503, "Conversation controls are not configured.")
    token, tenant = config
    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return refused("unauthorized", 401, "The control credential was not accepted.")
    if request.headers.get(HEADER) != SCHEMA:
        return refused("unsupported_schema", 400, "The control contract version is required.")
    store = getattr(request.app.state, "control_store", None)
    if store is None:
        return refused("controls_unavailable", 503, "Conversation controls are not available.")
    try:
        if not ID_PATTERN.fullmatch(account_id) or not re.fullmatch(r"[0-9a-f-]{36}", case_id):
            raise ValueError()
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ValueError()
        raw = bytearray()
        async for part in request.stream():
            if len(raw) + len(part) > 8192:
                return refused("request_too_large", 413, "The control request is too large.")
            raw.extend(part)
        body = json.loads(raw, object_pairs_hook=unique_keys)
        action = body.get("action") if type(body) is dict else None
        allowed = CASE_KEYS | ({"resolution"} if action == "resolve" else {"owner"} if action == "acknowledge" and "owner" in body else set())
        if (type(body) is not dict or set(body) != allowed or action not in {"acknowledge", "resolve"}
                or type(body["commandKey"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", body["commandKey"])
                or not actor_allowed(body["actor"], capability="manage")
                or (action == "resolve" and (type(body["resolution"]) is not str or not 1 <= len(body["resolution"].strip()) <= 2000))
                or ("owner" in body and (type(body["owner"]) is not str or not 1 <= len(body["owner"]) <= 128))):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError, KeyError):
        return refused("invalid_request", 400, "That operator case request is invalid.")
    from onlyfans_runtime import operator_cases
    try:
        if action == "acknowledge":
            view = await asyncio.to_thread(operator_cases.acknowledge, store, tenant, account_id, case_id,
                                           actor=body["actor"], owner=body.get("owner"))
        else:
            view = await asyncio.to_thread(operator_cases.resolve, store, tenant, account_id, case_id,
                                           actor=body["actor"], resolution=body["resolution"])
    except Refused as exc:
        if str(exc) in {"scope unavailable", "case unavailable"}:
            return refused("scope_unavailable", 404, "That case is not available.")
        return refused("case_conflict", 409, "That case cannot take this action in its current state.")
    except Exception:
        log.exception("operator case action unavailable")
        return refused("outcome_unconfirmed", 500, "The outcome is not confirmed. Refresh and retry.")
    return response({"ok": True, "applied": True, "action": action, "case": view})


LEDGER_RETRY_KEYS = {"commandKey", "expectedConnectionRevision", "expectedScanFingerprint"}
LEDGER_RETRY_CODES = {"ledger busy": ("ledger_busy", "The ledger scan is running. Refresh before retrying."),
    "ledger not held": ("ledger_not_held", "The ledger scan is not held. Refresh the ledger status."),
    "ledger state changed": ("ledger_state_changed", "The ledger state changed. Refresh before retrying."),
    "idempotency key conflict": ("command_conflict", "That command key was already used for a different request.")}


def gateway_actor(request):
    """The trusted gateway's identity: the verified agency grant, else the service-only actor header."""
    scope = current_scope.get()
    if scope is not None:
        return scope.actor if scope.can_manage else None
    actor = request.headers.get("x-onlyfans-actor", "")
    return actor if re.fullmatch(ACTOR_PATTERN, actor) else None


@router.post("/accounts/{account_id}/ledger/retry")
async def ledger_retry(request: Request, account_id: str):
    """Make a held ledger scan due. Acknowledges queued only; never contacts the provider or reconciles."""
    config = configured()
    if config is None:
        return refused("controls_unavailable", 503, "Conversation controls are not configured.")
    token, tenant = config
    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return refused("unauthorized", 401, "The control credential was not accepted.")
    if request.headers.get(HEADER) != SCHEMA:
        return refused("unsupported_schema", 400, "The control contract version is required.")
    store = getattr(request.app.state, "control_store", None)
    if store is None:
        return refused("controls_unavailable", 503, "Conversation controls are not available.")
    if current_scope.get() is not None and not current_scope.get().can_manage:
        return refused("forbidden", 403, "That action needs management permission.")
    try:
        if not ID_PATTERN.fullmatch(account_id):
            raise ValueError()
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ValueError()
        raw = bytearray()
        async for part in request.stream():
            if len(raw) + len(part) > 4096:
                return refused("request_too_large", 413, "The control request is too large.")
            raw.extend(part)
        def nonfinite(_):
            raise ValueError("nonfinite number")
        body = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=nonfinite)
        actor = gateway_actor(request)
        if type(body) is not dict or set(body) != LEDGER_RETRY_KEYS or actor is None:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return refused("invalid_request", 400, "That ledger retry request is invalid.")
    from onlyfans_runtime.launch_ledger.dashboard import retry
    try:
        ack = await asyncio.to_thread(retry, store, tenant, account_id, command_key=body["commandKey"],
            expected_revision=body["expectedConnectionRevision"], expected_fingerprint=body["expectedScanFingerprint"], actor=actor)
    except Refused as exc:
        text = str(exc)
        if text == "scope unavailable":
            return refused("scope_unavailable", 404, "That account is not available.")
        if text.startswith("invalid"):
            return refused("invalid_request", 400, "That ledger retry request is invalid.")
        code, message = LEDGER_RETRY_CODES.get(text, ("control_conflict", "The retry could not be applied. Refresh the ledger status."))
        return refused(code, 409, message)
    except Exception:
        log.exception("ledger retry acknowledgement unavailable")
        return refused("outcome_unconfirmed", 500, "The outcome is not confirmed. Refresh or retry the same request.")
    return response({"ok": True, **ack})
