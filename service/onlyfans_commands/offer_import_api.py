"""Private of-offer-import.v1 endpoints. HTTP only enqueues and reads work.

Provider reads belong to the worker. Draft amounts, media and previews come
from the selected observation; no client-supplied price is ever accepted.
"""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from onlyfans_runtime.offer_import import (InvalidImport, cancel_job, create_drafts, get_candidate,
    get_command, get_job, list_candidates, request_job, withdraw_draft)
from onlyfans_runtime.store import Refused
from . import content_api
from .api import unique_keys

SCHEMA = "of-offer-import.v1"
HEADER = "X-OnlyFans-Offer-Import-Version"
PREFIX = "/onlyfans/offer-import/v1"
router = APIRouter(prefix=PREFIX, tags=["onlyfans-offer-import"])


def response(body, status=200):
    return JSONResponse({"schemaVersion": SCHEMA, **body}, status_code=status,
        headers={"Cache-Control": "private, no-store", HEADER: SCHEMA})


def refuse(code, status, message):
    return response({"ok": False, "code": code, "error": message}, status)


def authenticate(request, account_id):
    """Reuse the content-purpose credential and tenant under this contract's header."""
    import hmac
    from onlyfans_read.contract import ID_PATTERN
    config = content_api.configured()
    if config is None:
        return refuse("import_unavailable", 503, "Offer import is not configured.")
    token, tenant = config
    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return refuse("unauthorized", 401, "The content credential was not accepted.")
    if request.headers.get(HEADER) != SCHEMA:
        return refuse("unsupported_schema", 400, "The offer import contract version is required.")
    if not ID_PATTERN.fullmatch(account_id):
        return refuse("invalid_request", 400, "That account reference is invalid.")
    store = getattr(request.app.state, "control_store", None)
    if store is None:
        return refuse("import_unavailable", 503, "Offer import is unavailable.")
    return store, tenant


async def body(request, keys):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise InvalidImport("Use an explicit JSON request.")
    raw = bytearray()
    async for part in request.stream():
        if len(raw) + len(part) > 131072:
            raise InvalidImport("The request is too large.")
        raw.extend(part)
    try:
        value = json.loads(raw, object_pairs_hook=unique_keys,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict or set(value) != keys:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidImport("That import request is invalid.") from None


def failure(exc):
    if isinstance(exc, InvalidImport):
        return refuse("invalid_import_request", 400, str(exc))
    if isinstance(exc, Refused):
        text = str(exc)
        if text in {"scope unavailable", "import job unavailable", "import candidate unavailable",
                    "import source unavailable", "import draft unavailable"}:
            return refuse("not_available", 404, "Those import records are not available.")
        if text == "verified connection unavailable":
            return refuse("connection_unavailable", 409, "Verify the selected account connection first.")
        if text == "import job already active":
            return refuse("job_active", 409, "A job of this kind is already running.")
        return refuse("import_conflict", 409, "Import state changed. Refresh before trying again.")
    return refuse("outcome_unconfirmed", 500, "The outcome is not confirmed. Check the command before retrying.")


async def run(request, account_id, call):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    try:
        return await call(store, tenant)
    except (InvalidImport, Refused) as exc:
        return failure(exc)
    except Exception as exc:
        return failure(exc)


@router.post("/accounts/{account_id}/jobs")
async def create_job(request: Request, account_id: str):
    async def call(store, tenant):
        value = await body(request, {"mode", "sources", "commandKey", "actor"})
        job = await asyncio.to_thread(request_job, store, tenant, account_id, mode=value["mode"],
            sources=value["sources"], command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "accountId": account_id, "job": job})
    return await run(request, account_id, call)


@router.get("/accounts/{account_id}/jobs/{job_id}")
async def read_job(request: Request, account_id: str, job_id: str):
    async def call(store, tenant):
        return response({"ok": True, "accountId": account_id, "job": await asyncio.to_thread(get_job, store, tenant, account_id, job_id)})
    return await run(request, account_id, call)


@router.get("/accounts/{account_id}/jobs/{job_id}/candidates")
async def read_candidates(request: Request, account_id: str, job_id: str):
    async def call(store, tenant):
        params = request.query_params
        if not set(params) <= {"after", "limit"} or any(len(params.getlist(k)) != 1 for k in params):
            raise InvalidImport("That candidate page request is invalid.")
        raw = params.get("limit", "25")
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 2:
            raise InvalidImport("That page size is invalid.")
        page = await asyncio.to_thread(list_candidates, store, tenant, account_id, job_id,
            after=params.get("after"), limit=int(raw))
        return response({"ok": True, "accountId": account_id, **page})
    return await run(request, account_id, call)


@router.get("/accounts/{account_id}/jobs/{job_id}/candidates/{candidate_id}")
async def read_candidate(request: Request, account_id: str, job_id: str, candidate_id: str):
    async def call(store, tenant):
        value = await asyncio.to_thread(get_candidate, store, tenant, account_id, job_id, candidate_id)
        return response({"ok": True, "accountId": account_id, "candidate": value})
    return await run(request, account_id, call)


@router.post("/accounts/{account_id}/jobs/{job_id}/cancel")
async def cancel(request: Request, account_id: str, job_id: str):
    async def call(store, tenant):
        value = await body(request, {"expectedRevision", "actor"})
        job = await asyncio.to_thread(cancel_job, store, tenant, account_id, job_id,
            expected_revision=value["expectedRevision"], actor=value["actor"])
        return response({"ok": True, "accountId": account_id, "job": job})
    return await run(request, account_id, call)


@router.post("/accounts/{account_id}/drafts")
async def drafts(request: Request, account_id: str):
    async def call(store, tenant):
        value = await body(request, {"drafts", "actor"})
        results = await asyncio.to_thread(create_drafts, store, tenant, account_id,
            drafts=value["drafts"], actor=value["actor"])
        # Per-item outcomes: a partial batch is never reported as all created.
        return response({"ok": True, "accountId": account_id, "results": results,
            "created": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results)})
    return await run(request, account_id, call)


@router.post("/accounts/{account_id}/drafts/{item_id}/withdraw")
async def withdraw(request: Request, account_id: str, item_id: str):
    async def call(store, tenant):
        value = await body(request, {"commandKey", "actor"})
        if not item_id.isascii() or not item_id.isdecimal() or len(item_id) > 10:
            raise InvalidImport("That item reference is invalid.")
        result = await asyncio.to_thread(withdraw_draft, store, tenant, account_id, int(item_id),
            command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "accountId": account_id, **result})
    return await run(request, account_id, call)


@router.get("/accounts/{account_id}/commands/{command_key}")
async def command(request: Request, account_id: str, command_key: str):
    async def call(store, tenant):
        value = await asyncio.to_thread(get_command, store, tenant, account_id, command_key)
        return response({"ok": True, "accountId": account_id, "command": value})
    return await run(request, account_id, call)
