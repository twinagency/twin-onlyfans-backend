"""Content-purpose vault jobs and native catalogue creation; API never reads provider."""
import asyncio
import hmac
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from onlyfans_read.contract import ID_PATTERN
from onlyfans_runtime.store import Refused
from onlyfans_runtime.vault_management import InvalidVault, create_product, list_vault, request_sync
from .api import unique_keys
from .content_api import configured, response, refuse, HEADER, SCHEMA, PREFIX

router = APIRouter(prefix=PREFIX, tags=["onlyfans-vault"])


def authenticate(request, account):
    config = configured()
    if config is None:
        return refuse("content_unavailable", 503, "Content management is not configured.")
    token, tenant = config
    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return refuse("unauthorized", 401, "The content credential was not accepted.")
    if request.headers.get(HEADER) != SCHEMA:
        return refuse("unsupported_schema", 400, "The content contract version is required.")
    if not ID_PATTERN.fullmatch(account):
        return refuse("invalid_request", 400, "That account reference is invalid.")
    store = getattr(request.app.state, "control_store", None)
    if store is None:
        return refuse("content_unavailable", 503, "Content management is unavailable.")
    return store, tenant


async def body(request, keys):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise InvalidVault("Use an explicit JSON vault request.")
    raw = bytearray()
    async for part in request.stream():
        if len(raw) + len(part) > 65536:
            return refuse("request_too_large", 413, "The vault request is too large.")
        raw.extend(part)
    try:
        value = json.loads(raw, object_pairs_hook=unique_keys,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict or set(value) != keys:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidVault("That vault request is invalid.") from None


def failure(exc):
    if isinstance(exc, InvalidVault):
        return refuse("invalid_vault_request", 400, str(exc))
    if isinstance(exc, Refused):
        if str(exc) in {"scope unavailable", "vault media unavailable", "vault folder unavailable"}:
            return refuse("not_available", 404, "Those vault records are not available.")
        if str(exc) == "verified connection unavailable":
            return refuse("vault_connection_unavailable", 409, "Verify the selected account connection before synchronizing its vault.")
        return refuse("vault_conflict", 409, "Vault or catalogue state changed. Refresh before trying again.")
    return refuse("outcome_unconfirmed", 500, "The outcome is not confirmed. Refresh or retry the same request.")


@router.get("/accounts/{account_id}/vault")
async def get(request: Request, account_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    try:
        if not set(request.query_params) <= {"afterMediaId", "limit", "q", "kind", "folderId", "includeSummary"} or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise InvalidVault("That vault page request is invalid.")
        if request.query_params.get("includeSummary", "0") not in {"0", "1"}:
            raise InvalidVault("That vault summary option is invalid.")
        raw_limit = request.query_params.get("limit", "24")
        if not raw_limit.isascii() or not raw_limit.isdecimal() or len(raw_limit) > 2:
            raise InvalidVault("That vault page size is invalid.")
        value = await asyncio.to_thread(list_vault, store, tenant, account_id,
            after_media_id=request.query_params.get("afterMediaId"), limit=int(raw_limit),
            query=request.query_params.get("q", ""), kind=request.query_params.get("kind", "all"),folder_id=request.query_params.get("folderId"),
            include_summary=request.query_params.get("includeSummary") == "1")
        return response({"ok": True, "accountId": account_id, "vault": value})
    except Exception as exc:
        return failure(exc)


@router.post("/accounts/{account_id}/vault/sync")
async def sync(request: Request, account_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    try:
        value = await body(request, {"expectedConnectionRevision", "commandKey", "actor"})
        if isinstance(value, JSONResponse):
            return value
        job = await asyncio.to_thread(request_sync, store, tenant, account_id,
            expected_connection_revision=value["expectedConnectionRevision"], command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "applied": True, "accountId": account_id, "commandKey": value["commandKey"], "sync": job})
    except Exception as exc:
        return failure(exc)


@router.get("/accounts/{account_id}/vault/previews/{media_id}")
async def preview(request: Request, account_id: str, media_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse): return context
    try:
        if request.query_params: raise InvalidVault("Preview reads do not accept URLs or query parameters.")
        from onlyfans_runtime.vault_previews import read_preview, SCHEMA as PREVIEW_SCHEMA
        data, mime = await asyncio.to_thread(read_preview, *context, account_id, media_id)
        return Response(data, media_type=mime, headers={
            "Cache-Control": "private, no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox", HEADER: SCHEMA,
            "X-OnlyFans-Preview": PREVIEW_SCHEMA, "X-OnlyFans-Account-Id": account_id,
            "X-OnlyFans-Media-Id": media_id})
    except Refused:
        return refuse("preview_unavailable", 404, "That vault preview is unavailable. Sync the vault to refresh it.")
    except Exception as exc: return failure(exc)


@router.post("/accounts/{account_id}/items")
async def create(request: Request, account_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    try:
        value = await body(request, {"product", "commandKey", "actor"})
        if isinstance(value, JSONResponse):
            return value
        acknowledgement = await asyncio.to_thread(create_product, store, tenant, account_id,
            product=value["product"], command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "applied": True, "accountId": account_id, **acknowledgement})
    except Exception as exc:
        return failure(exc)


@router.get("/accounts/{account_id}/vault/assets/{media_id}")
async def asset_get(request:Request,account_id:str,media_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    try:
        if request.query_params:raise InvalidVault("Asset metadata does not use query parameters.")
        from onlyfans_runtime.vault_editing import get_asset
        result=await asyncio.to_thread(get_asset,*context,account_id,media_id)
        return response({"ok":True,"accountId":account_id,"asset":result})
    except Exception as exc:return failure(exc)


@router.post("/accounts/{account_id}/vault/assets/{media_id}")
async def asset_edit(request:Request,account_id:str,media_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    try:
        value=await body(request,{"patch","expectedFingerprint","commandKey","actor"})
        if isinstance(value,JSONResponse):return value
        from onlyfans_runtime.vault_editing import edit_asset
        result=await asyncio.to_thread(edit_asset,*context,account_id,media_id,patch=value["patch"],
            expected_fingerprint=value["expectedFingerprint"],command_key=value["commandKey"],actor=value["actor"])
        return response({"ok":True,"applied":True,"accountId":account_id,"commandKey":value["commandKey"],**result})
    except Exception as exc:return failure(exc)


@router.get("/accounts/{account_id}/vault/folders")
@router.get("/accounts/{account_id}/vault/folders/{folder_id}")
async def folders_get(request:Request,account_id:str,folder_id:str=None):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    try:
        if request.query_params:raise InvalidVault("Folder metadata does not use query parameters.")
        from onlyfans_runtime.vault_editing import get_folders
        result=await asyncio.to_thread(get_folders,*context,account_id,folder_id)
        return response({"ok":True,"accountId":account_id,"folder" if folder_id is not None else "folders":result})
    except Exception as exc:return failure(exc)


@router.post("/accounts/{account_id}/vault/folders")
@router.post("/accounts/{account_id}/vault/folders/{folder_id}")
async def folder_edit(request:Request,account_id:str,folder_id:str=None):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    try:
        value=await body(request,{"action","name","mediaIds","expectedFingerprint","commandKey","actor"})
        if isinstance(value,JSONResponse):return value
        from onlyfans_runtime.vault_editing import edit_folder
        result=await asyncio.to_thread(edit_folder,*context,account_id,folder_id,action=value["action"],name=value["name"],media_ids=value["mediaIds"],
            expected_fingerprint=value["expectedFingerprint"],command_key=value["commandKey"],actor=value["actor"])
        return response({"ok":True,"applied":True,"accountId":account_id,"commandKey":value["commandKey"],**result})
    except Exception as exc:return failure(exc)


@router.get("/accounts/{account_id}/message-media/{fan_id}/{message_id}/{media_id}/{variant}")
async def message_media(request: Request, account_id: str, fan_id: str, message_id: str, media_id: str, variant: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse): return context
    try:
        if request.query_params: raise InvalidVault("Message media reads do not accept query parameters.")
        from onlyfans_runtime.message_media import read_or_queue, reference
        ref = reference(account_id, fan_id, message_id, media_id, variant)
        value = await asyncio.to_thread(read_or_queue, *context, ref)
        if value is None:
            return JSONResponse({"status": "preparing"}, status_code=202, headers={"Cache-Control": "private, no-store", "Retry-After": "2"})
        data, mime = value
        return Response(data, media_type=mime, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox"})
    except Refused:
        return refuse("message_media_unavailable", 404, "That message attachment is unavailable.")
    except Exception:
        return refuse("message_media_unavailable", 503, "That message attachment could not be read.")
