"""Private of-content-sync.v1 endpoints. HTTP only enqueues and reads work.

Provider I/O (history refresh, vault reads, uploads) belongs to the worker.
Same content-purpose credential and tenant as offer import. Saving a draft
never sends a fan message, publishes a post or changes outgoing authority.
"""
import asyncio
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from onlyfans_runtime import content_sync as sync
from onlyfans_runtime.content_sync import InvalidSync
from onlyfans_runtime.store import Refused
from . import content_api
from .api import unique_keys

SCHEMA = "of-content-sync.v1"
HEADER = "X-OnlyFans-Content-Sync-Version"
PREFIX = "/onlyfans/content-sync/v1"
DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024
router = APIRouter(prefix=PREFIX, tags=["onlyfans-content-sync"])


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
        return refuse("sync_unavailable", 503, "Content sync is not configured.")
    token, tenant = config
    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return refuse("unauthorized", 401, "The content credential was not accepted.")
    if request.headers.get(HEADER) != SCHEMA:
        return refuse("unsupported_schema", 400, "The content sync contract version is required.")
    if not ID_PATTERN.fullmatch(account_id):
        return refuse("invalid_sync_request", 400, "That account reference is invalid.")
    store = getattr(request.app.state, "control_store", None)
    if store is None:
        return refuse("sync_unavailable", 503, "Content sync is unavailable.")
    return store, tenant


async def body(request, keys):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise InvalidSync("Use an explicit JSON request.")
    raw = bytearray()
    async for part in request.stream():
        if len(raw) + len(part) > 131072:
            raise InvalidSync("The request is too large.")
        raw.extend(part)
    try:
        value = json.loads(raw, object_pairs_hook=unique_keys,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict or set(value) != keys:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidSync("That sync request is invalid.") from None


def failure(exc):
    if isinstance(exc, Refused):
        text = str(exc)
        if "connection" in text and "unavailable" in text:
            return refuse("connection_unavailable", 409, "Verify the selected account connection first.")
        if "storage" in text:
            return refuse("sync_unavailable", 503, "Upload storage is unavailable. Try again shortly.")
        if text == "scope unavailable" or text.endswith(" unavailable"):
            return refuse("not_available", 404, "Those sync records are not available.")
        if "idempotency" in text:
            return refuse("idempotency_conflict", 409, "That command was already used with different details.")
        if "revision conflict" in text:
            return refuse("revision_conflict", 409, "This changed since you loaded it. Refresh and review again.")
        return refuse("sync_conflict", 409, "Sync state changed. Refresh before trying again.")
    if isinstance(exc, ImportError):
        return refuse("sync_unavailable", 503, "Media sync is not available in this release.")
    if isinstance(exc, ValueError):
        # Includes the media module's own InvalidSync, which also subclasses ValueError.
        return refuse("invalid_sync_request", 400, str(exc) if len(str(exc)) < 240 else "That sync request is invalid.")
    return refuse("outcome_unconfirmed", 500, "The outcome is not confirmed. Check the command before retrying.")


async def run(request, account_id, call):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    try:
        return await call(store, tenant)
    except Exception as exc:
        return failure(exc)


def media():
    from onlyfans_runtime import content_sync_media
    return content_sync_media


def page_params(request, extra=()):
    params = request.query_params
    if not set(params) <= {"after", "limit", *extra} or any(len(params.getlist(k)) != 1 for k in params):
        raise InvalidSync("That page request is invalid.")
    raw = params.get("limit", "25")
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 2:
        raise InvalidSync("That page size is invalid.")
    return params, int(raw)


@router.get("/accounts/{account_id}/status")
async def status(request: Request, account_id: str):
    async def call(store, tenant):
        if request.query_params:
            raise InvalidSync("Status takes no parameters.")
        return response({"ok": True, **await asyncio.to_thread(sync.get_status, store, tenant, account_id)})
    return await run(request, account_id, call)


@router.post("/accounts/{account_id}/settings")
async def settings(request: Request, account_id: str):
    async def call(store, tenant):
        value = await body(request, {"enabled", "expectedRevision", "commandKey", "actor"})
        result = await asyncio.to_thread(sync.set_settings, store, tenant, account_id, enabled=value["enabled"],
            expected_revision=value["expectedRevision"], command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "accountId": account_id, **result})
    return await run(request, account_id, call)


@router.post("/accounts/{account_id}/sync")
async def sync_now(request: Request, account_id: str):
    async def call(store, tenant):
        value = await body(request, {"kind", "commandKey", "actor"})
        result = await asyncio.to_thread(sync.request_sync_now, store, tenant, account_id, kind=value["kind"],
            command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "accountId": account_id, **result})
    return await run(request, account_id, call)


@router.get("/accounts/{account_id}/operations")
async def operations(request: Request, account_id: str):
    async def call(store, tenant):
        params, limit = page_params(request)
        page = await asyncio.to_thread(sync.list_operations, store, tenant, account_id, after=params.get("after"), limit=limit)
        return response({"ok": True, "accountId": account_id, **page})
    return await run(request, account_id, call)


@router.get("/accounts/{account_id}/operations/{operation_id}")
async def operation(request: Request, account_id: str, operation_id: str):
    async def call(store, tenant):
        return response({"ok": True, "accountId": account_id,
            **await asyncio.to_thread(sync.get_operation, store, tenant, account_id, operation_id)})
    return await run(request, account_id, call)


@router.post("/accounts/{account_id}/operations/{operation_id}/retry")
async def retry(request: Request, account_id: str, operation_id: str):
    async def call(store, tenant):
        value = await body(request, {"commandKey", "actor"})
        result = await asyncio.to_thread(sync.retry_operation, store, tenant, account_id, operation_id,
            command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "accountId": account_id, **result})
    return await run(request, account_id, call)


@router.get("/accounts/{account_id}/conflicts")
async def conflicts(request: Request, account_id: str):
    async def call(store, tenant):
        params = request.query_params
        if not set(params) <= {"status"} or any(len(params.getlist(k)) != 1 for k in params):
            raise InvalidSync("That conflict request is invalid.")
        page = await asyncio.to_thread(sync.list_conflicts, store, tenant, account_id, status=params.get("status", "open"))
        return response({"ok": True, "accountId": account_id, **page})
    return await run(request, account_id, call)


@router.post("/accounts/{account_id}/conflicts/{conflict_id}/resolve")
async def resolve(request: Request, account_id: str, conflict_id: str):
    async def call(store, tenant):
        value = await body(request, {"resolution", "expectedRevision", "expectedItemFingerprint", "commandKey", "actor"})
        result = await asyncio.to_thread(sync.resolve_conflict, store, tenant, account_id, conflict_id,
            resolution=value["resolution"], expected_revision=value["expectedRevision"],
            expected_item_fingerprint=value["expectedItemFingerprint"], command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "accountId": account_id, **result})
    return await run(request, account_id, call)


@router.post("/accounts/{account_id}/drafts")
async def save_draft(request: Request, account_id: str):
    async def call(store, tenant):
        value = await body(request, {"commandKey", "actor", "label", "description", "priceCents", "files", "previewFileIds"})
        draft = await asyncio.to_thread(media().save_draft, store, tenant, account_id, value=value, actor=value["actor"])
        return response({"ok": True, "accountId": account_id, "draft": draft})
    return await run(request, account_id, call)


@router.put("/accounts/{account_id}/drafts/{draft_id}/files/{file_id}/chunks/{index}")
async def chunk(request: Request, account_id: str, draft_id: str, file_id: str, index: str):
    async def call(store, tenant):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/octet-stream":
            raise InvalidSync("Send the chunk as application/octet-stream.")
        if not re.fullmatch(r"[0-9]{1,6}", index):
            raise InvalidSync("That chunk number is invalid.")
        limit = getattr(media(), "CHUNK_BYTES", DEFAULT_CHUNK_BYTES)
        raw = bytearray()
        async for part in request.stream():
            if len(raw) + len(part) > limit:
                raise InvalidSync("That upload chunk is too large.")
            raw.extend(part)
        result = await asyncio.to_thread(media().put_chunk, store, tenant, account_id, draft_id, file_id, int(index), bytes(raw))
        return response({"ok": True, "accountId": account_id, **result})
    return await run(request, account_id, call)


@router.post("/accounts/{account_id}/drafts/{draft_id}/commit")
async def commit(request: Request, account_id: str, draft_id: str):
    async def call(store, tenant):
        value = await body(request, {"commandKey", "actor"})
        draft = await asyncio.to_thread(media().commit_draft, store, tenant, account_id, draft_id,
            command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "accountId": account_id, "draft": draft})
    return await run(request, account_id, call)


@router.get("/accounts/{account_id}/drafts")
async def drafts(request: Request, account_id: str):
    async def call(store, tenant):
        params, limit = page_params(request)
        page = await asyncio.to_thread(media().list_drafts, store, tenant, account_id, after=params.get("after"), limit=limit)
        return response({"ok": True, "accountId": account_id, **page})
    return await run(request, account_id, call)


@router.get("/accounts/{account_id}/drafts/{draft_id}")
async def draft(request: Request, account_id: str, draft_id: str):
    async def call(store, tenant):
        return response({"ok": True, "accountId": account_id,
            "draft": await asyncio.to_thread(media().get_draft, store, tenant, account_id, draft_id)})
    return await run(request, account_id, call)


@router.get("/accounts/{account_id}/commands/{command_key}")
async def command(request: Request, account_id: str, command_key: str):
    async def call(store, tenant):
        value = await asyncio.to_thread(sync.get_command, store, tenant, account_id, command_key)
        return response({"ok": True, "accountId": account_id, "command": value})
    return await run(request, account_id, call)
