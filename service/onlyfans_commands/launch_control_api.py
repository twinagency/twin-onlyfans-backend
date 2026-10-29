"""Read-purpose launch evidence and control-purpose durable operator commands."""
import asyncio
import hmac
import re
import logging
import traceback
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from onlyfans_read.api import _authenticate, _refusal, _PRIVATE_HEADERS
from onlyfans_read.contract import ID_PATTERN, SCHEMA_VERSION, SERVICE_PREFIX
from onlyfans_read.errors import BadRequest, OnlyFansReadError, Refused as ReadRefused, Unavailable
from onlyfans_runtime.launch_operations import control_account, launch_readiness, ledger_status, resolve_ledger
from onlyfans_runtime.launch_pagination import query_bounds
from onlyfans_runtime.store import Refused
from .api import response, refused
from .launch_api import _body
from .settings import configured, HEADER, PREFIX, SCHEMA

read_router = APIRouter(prefix=SERVICE_PREFIX, tags=["onlyfans-launch-read"])
control_router = APIRouter(prefix=PREFIX, tags=["onlyfans-launch-controls"])


def _store(request):
    store = getattr(request.app.state, "control_store", None)
    if store is None:
        raise Unavailable("launch_unavailable", "Account launch evidence is unavailable.")
    return store


async def _read(request, account, operation):
    reference = uuid.uuid4().hex
    try:
        principal = await _authenticate(request)
        if not ID_PATTERN.fullmatch(account):
            raise BadRequest("invalid_account", "That account reference is invalid.")
        store = _store(request)
        if operation == "ledger":
            cursor, limit = query_bounds(request.query_params)
            value = await asyncio.to_thread(ledger_status, store, principal.tenant_id, account, cursor=cursor, limit=limit)
        elif operation == "activation":
            if request.query_params:
                raise BadRequest("invalid_query", "This read takes no query parameters.")
            from onlyfans_runtime.activation_commands import read_activation
            reader = getattr(request.app.state, "personality_credential_status_reader", None)
            value = await asyncio.to_thread(read_activation, store, principal.tenant_id, account, credential_reader=reader)
        else:
            if request.query_params:
                raise BadRequest("invalid_query", "This read takes no query parameters.")
            reader = getattr(request.app.state, "personality_credential_status_reader", None)
            value = await asyncio.to_thread(launch_readiness, store, principal.tenant_id, account, credential_reader=reader)
    except OnlyFansReadError as exc:
        return _refusal(exc)
    except Refused as exc:
        if "invalid" in str(exc):
            return _refusal(BadRequest("invalid_request", "That request needs correction."))
        return _refusal(ReadRefused("unknown_account"))
    except Exception as error:
        # Stack locations and error class identify the failure without logging
        # SQL values, provider payloads, fan text or credentials.
        frames = [(frame.filename.rsplit("/", 1)[-1], frame.name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)]
        logging.getLogger("onlyfans.launch").error("evidence failure ref=%s operation=%s class=%s stack=%s", reference, operation, type(error).__name__, frames)
        answer = _refusal(Unavailable("launch_unavailable", "Account launch evidence could not be read."))
        answer.headers["X-OnlyFans-Evidence-Reference"] = reference
        return answer
    return JSONResponse({"ok": True, "schemaVersion": SCHEMA_VERSION, "accountId": account, operation: value},
                        headers=_PRIVATE_HEADERS)


@read_router.get("/accounts/{account_id}/ledger/evidence")
async def ledger(request: Request, account_id: str):
    return await _read(request, account_id, "ledger")


@read_router.get("/accounts/{account_id}/activation")
async def activation(request: Request, account_id: str):
    return await _read(request, account_id, "activation")


@read_router.get("/accounts/{account_id}/readiness")
async def readiness(request: Request, account_id: str):
    return await _read(request, account_id, "readiness")


def _control_auth(request, account):
    config = configured()
    if config is None:
        return refused("controls_unavailable", 503, "Account controls are not configured.")
    token, tenant = config
    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return refused("unauthorized", 401, "The control credential was not accepted.")
    if request.headers.get(HEADER) != SCHEMA:
        return refused("unsupported_schema", 400, "The control contract version is required.")
    if not ID_PATTERN.fullmatch(account):
        return refused("invalid_request", 400, "That account reference is invalid.")
    if getattr(request.app.state, "control_store", None) is None:
        return refused("controls_unavailable", 503, "Account controls are unavailable.")
    return request.app.state.control_store, tenant


async def _control(request, account, operation):
    context = _control_auth(request, account)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    extra = ("action",) if operation == "account" else ("evidenceHash",) if operation == "resolution" else ()
    try:
        if request.query_params:
            raise Refused("invalid query")
        body = await _body(request, ("commandKey", "actor", "reason", "expectedFingerprint", *extra))
        arguments = dict(command_key=body["commandKey"], actor=body["actor"], reason=body["reason"],
            expected_fingerprint=body["expectedFingerprint"])
        if operation == "account":
            reader = getattr(request.app.state, "personality_credential_status_reader", None)
            value = await asyncio.to_thread(control_account, store, tenant, account,
                action=body["action"], credential_reader=reader, **arguments)
        elif operation == "resolution":
            value = await asyncio.to_thread(resolve_ledger, store, tenant, account,
                evidence_hash=body["evidenceHash"], **arguments)
    except Refused as exc:
        text = str(exc)
        if text in {"scope unavailable", "ledger evidence unavailable"}:
            return refused("not_available", 404, "That account resource is not available.")
        if "invalid" in text:
            return refused("invalid_request", 400, "That request needs correction.")
        if "permission" in text:
            return refused("forbidden", 403, "That action needs management permission.")
        return refused("control_conflict", 409, "The account state changed or prerequisites are unmet. Refresh before continuing.")
    except Exception:
        return refused("outcome_unconfirmed", 500, "The outcome is not confirmed. Read the authoritative state or retry the same command.")
    return response({"ok": True, "accountId": account, "command": value})


ACTIVATION_OPERATIONS = {
    "enable-existing": ("afterFan", "batch"),
    "future-conversations": ("enabled",),
    "operating-limits": ("limitUnits", "throttleSeconds", "mediaEnabled"),
}


async def _activation(request, account, operation):
    from onlyfans_runtime import activation_commands as commands
    context = _control_auth(request, account)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    extra = ACTIVATION_OPERATIONS[operation]
    try:
        if request.query_params:
            raise Refused("invalid query")
        optional = extra if operation == "enable-existing" else ()
        body = await _body(request, ("commandKey", "actor", "reason", "expectedFingerprint", *(() if optional else extra)), optional=optional)
        common = dict(command_key=body["commandKey"], actor=body["actor"], reason=body["reason"],
                      expected_fingerprint=body["expectedFingerprint"])
        reader = getattr(request.app.state, "personality_credential_status_reader", None)
        if operation == "enable-existing":
            after = body.get("afterFan", "0")
            if type(after) is not str or not re.fullmatch(r"0|[1-9][0-9]{0,17}", after):
                raise Refused("invalid fan cursor")
            value = await asyncio.to_thread(commands.enable_existing, store, tenant, account,
                after_fan=int(after), batch=body.get("batch", commands.DEFAULT_BATCH),
                credential_reader=reader, **common)
        elif operation == "future-conversations":
            value = await asyncio.to_thread(commands.set_future_conversations, store, tenant, account,
                enabled=body["enabled"], credential_reader=reader, **common)
        else:
            value = await asyncio.to_thread(commands.set_operating_limits, store, tenant, account,
                limit_units=body["limitUnits"], throttle_seconds=body["throttleSeconds"],
                media_enabled=body["mediaEnabled"], **common)
    except commands.Blocked as exc:
        return response({"ok": False, "code": "prerequisites_unmet", "blockers": exc.blockers,
                         "error": "The account is not ready for this change. See the status checks."}, 409)
    except Refused as exc:
        text = str(exc)
        if text in {"scope unavailable", "ledger evidence unavailable"}:
            return refused("not_available", 404, "That account resource is not available.")
        if "invalid" in text:
            return refused("invalid_request", 400, "That request needs correction.")
        if "permission" in text:
            return refused("forbidden", 403, "That action needs management permission.")
        if "used allowance" in text:
            return refused("allowance_below_used", 409, "The total allowance cannot be lower than what was already used.")
        return refused("control_conflict", 409, "The account state changed or prerequisites are unmet. Refresh before continuing.")
    except Exception:
        return refused("outcome_unconfirmed", 500, "The outcome is not confirmed. Read the authoritative state or retry the same command.")
    return response({"ok": True, "accountId": account, "command": value})


async def _model(request, account, operation):
    """This account's OWN model credential (save / read-only check) and its explicit activation. Never returns the key."""
    from onlyfans_runtime import connections, activation_commands as commands
    context = _control_auth(request, account)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    try:
        if request.query_params:
            raise Refused("invalid query")
        if operation == "model-check":
            body = await _body(request, ("actor",))
            value = await asyncio.to_thread(connections.check_model_credential, store, tenant, account)
        elif operation == "activate":
            body = await _body(request, ("commandKey", "actor", "expectedRevision", "limitUnits", "throttleSeconds", "mediaEnabled"))
            reader = getattr(request.app.state, "personality_credential_status_reader", None)
            value = await asyncio.to_thread(commands.activate_account, store, tenant, account, limit_units=body["limitUnits"],
                throttle_seconds=body["throttleSeconds"], media_enabled=body["mediaEnabled"], expected_revision=body["expectedRevision"],
                command_key=body["commandKey"], actor=body["actor"], credential_reader=reader)
        else:
            body = await _body(request, ("commandKey", "actor", "expectedRevision", "modelId", "apiKey"))
            if type(body["modelId"]) is not str or type(body["apiKey"]) is not str:
                raise Refused("invalid model credential")
            revision = await asyncio.to_thread(connections.set_model_credential, store, tenant, account,
                model={"modelId": body["modelId"], "apiKey": body["apiKey"]}, expected_revision=body["expectedRevision"],
                command_key=body["commandKey"], actor=body["actor"])
            value = {"recorded": True, "revision": revision, "commandKey": body["commandKey"]}
    except commands.Blocked as exc:
        return response({"ok": False, "code": "prerequisites_unmet", "blockers": exc.blockers,
                         "error": "The account is not ready for this change. See the status checks."}, 409)
    except Refused as exc:
        text = str(exc)
        if text == "scope unavailable":
            return refused("not_available", 404, "That account resource is not available.")
        if "invalid" in text:
            return refused("invalid_request", 400, "That request needs correction.")
        if "permission" in text:
            return refused("forbidden", 403, "That action needs management permission.")
        return refused("control_conflict", 409, "The connection changed or the credential was not accepted. Refresh before continuing.")
    except Exception:
        return refused("outcome_unconfirmed", 500, "The outcome is not confirmed. Read the authoritative state or retry the same command.")
    return response({"ok": True, "accountId": account, "command": value})


@control_router.post("/accounts/{account_id}/activation/{operation}")
async def activation_command(request: Request, account_id: str, operation: str):
    if operation in {"model-credential", "model-check", "activate"}:
        return await _model(request, account_id, operation)
    if operation not in ACTIVATION_OPERATIONS:
        return refused("not_found", 404, "Unknown activation operation.")
    return await _activation(request, account_id, operation)


@control_router.post("/accounts/{account_id}/control")
async def account_control(request: Request, account_id: str):
    return await _control(request, account_id, "account")


@control_router.post("/accounts/{account_id}/ledger/resolve")
async def ledger_resolve(request: Request, account_id: str):
    return await _control(request, account_id, "resolution")
