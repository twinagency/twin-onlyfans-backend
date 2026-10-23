"""Personality review routes under the existing configuration authority.

Mounting this router remains an integration action. These routes read canonical
history only; they accept no arbitrary provider profile, messages or secrets.
"""
import asyncio
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from onlyfans_commands.api import unique_keys
from onlyfans_commands.configuration_api import authenticate, refuse, response
from onlyfans_read.agency_scope import actor_allowed
from onlyfans_runtime.configuration import InvalidConfiguration
from onlyfans_runtime.store import Refused
from .workflow import PersonalityWorkflow, VERSION


PREFIX = "/onlyfans/configuration/v1/accounts/{account_id}/personality"
router = APIRouter(prefix=PREFIX, tags=["onlyfans-personality"])
MAX_BODY_BYTES = 65536


def _workflow(request, store):
    reader = getattr(request.app.state, "personality_credential_status_reader", None)
    if reader is None:
        return PersonalityWorkflow(store)
    return PersonalityWorkflow(store, credential_status_reader=reader)


def _positive(value):
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]{0,9}", value) is None or int(value) >= 2**31:
        raise InvalidConfiguration("invalid cursor")
    return int(value)


def _nonfinite(value):
    raise ValueError("nonfinite JSON")


async def _body(request, extra):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise InvalidConfiguration("JSON required")
    raw = bytearray()
    async for part in request.stream():
        if len(raw) + len(part) > MAX_BODY_BYTES:
            return refuse("request_too_large", 413, "The personality review request is too large.")
        raw.extend(part)
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys, parse_constant=_nonfinite)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidConfiguration("invalid JSON") from None
    if type(body) is not dict or set(body) != {"expectedRevision", "commandKey", "actor", *extra}:
        raise InvalidConfiguration("invalid fields")
    if (type(body["expectedRevision"]) is not int or not 0 <= body["expectedRevision"] < 2**31-1
            or type(body["commandKey"]) is not str or re.fullmatch(r"[a-f0-9]{64}", body["commandKey"]) is None
            or not actor_allowed(body["actor"])):
        raise InvalidConfiguration("invalid managing actor or command")
    return body


async def _run(request, account_id, operation, *, extra=(), revision=None):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    try:
        kwargs = {}
        body = None
        if operation == "history":
            if set(request.query_params) - {"before"} or len(request.query_params.getlist("before")) > 1:
                raise InvalidConfiguration("invalid cursor")
            kwargs["before"] = _positive(request.query_params["before"]) if "before" in request.query_params else None
        elif request.query_params:
            raise InvalidConfiguration("unexpected query")
        if revision is not None:
            kwargs["revision"] = _positive(revision)
        if request.method == "POST":
            body = await _body(request, extra)
            if isinstance(body, JSONResponse):
                return body
            kwargs = {"expected_revision": body["expectedRevision"], "command_key": body["commandKey"],
                      "actor": body["actor"], **{key: body[key] for key in extra}}
        workflow = _workflow(request, store)
        result = await asyncio.to_thread(getattr(workflow, operation), tenant, account_id, **kwargs)
    except InvalidConfiguration:
        return refuse("invalid_configuration", 400, "The personality review request needs correction.")
    except Refused as exc:
        if str(exc) in {"scope unavailable", "configuration unavailable", "personality unavailable", "personality source scope unavailable"}:
            return refuse("not_available", 404, "That personality review is not available.")
        return refuse("personality_conflict", 409, "The personality or configuration changed. Refresh before continuing.")
    except Exception:
        return refuse("outcome_unconfirmed", 500, "The operation outcome is not confirmed. Refresh or retry the same request.")
    payload = {"ok": True, "accountId": account_id, "personalityVersion": VERSION, "personality": result}
    if body is not None:
        payload["commandKey"] = body["commandKey"]
    return response(payload)


@router.get("")
async def get(request: Request, account_id: str):
    return await _run(request, account_id, "get")


@router.get("/history")
async def history(request: Request, account_id: str):
    return await _run(request, account_id, "history")


@router.get("/revisions/{revision}")
async def get_revision(request: Request, account_id: str, revision: str):
    return await _run(request, account_id, "get", revision=revision)


@router.post("/import")
async def import_existing(request: Request, account_id: str):
    return await _run(request, account_id, "import_existing")


@router.post("/review")
async def edit(request: Request, account_id: str):
    return await _run(request, account_id, "edit", extra=("review",))


@router.post("/approve")
async def approve(request: Request, account_id: str):
    return await _run(request, account_id, "approve", extra=("acknowledgements",))
