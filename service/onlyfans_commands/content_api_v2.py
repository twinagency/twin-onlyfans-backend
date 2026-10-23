"""Explicit of-content.v2 contract: pricing mode, import provenance and overrides.

The v1 endpoint keeps its exact shape. Same content credential, tenant, account
lock, idempotency and audit; only the response fields and fixed-item rules differ.
"""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from onlyfans_runtime.content_management import get_content_v2, update_content, InvalidContent
from onlyfans_runtime.store import Refused
from . import content_api
from .api import unique_keys

SCHEMA = "of-content.v2"
HEADER = content_api.HEADER
PREFIX = "/onlyfans/content/v2"
router = APIRouter(prefix=PREFIX, tags=["onlyfans-content"])


def response(body, status=200):
    return JSONResponse({"schemaVersion": SCHEMA, **body}, status_code=status,
        headers={"Cache-Control": "private, no-store", HEADER: SCHEMA})


def refuse(code, status, message):
    return response({"ok": False, "code": code, "error": message}, status)


def authenticate(request, account_id, item_id):
    context = content_api.authenticate_version(request, account_id, item_id, SCHEMA)
    if len(context) == 3:
        return refuse(*context)
    return context


@router.get("/accounts/{account_id}/items/{item_id}")
async def get(request: Request, account_id: str, item_id: str):
    context = authenticate(request, account_id, item_id)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    try:
        data = await asyncio.to_thread(get_content_v2, store, tenant, account_id, item_id)
    except Refused:
        return refuse("not_available", 404, "That content item is not available.")
    except Exception:
        return refuse("content_unavailable", 503, "Content could not be read.")
    return response({"ok": True, "accountId": account_id, "itemId": item_id, "item": data})


@router.post("/accounts/{account_id}/items/{item_id}")
async def post(request: Request, account_id: str, item_id: str):
    context = authenticate(request, account_id, item_id)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    keys = {"patch", "expectedRevision", "expectedFingerprint", "commandKey", "actor", "acknowledgeOverride"}
    try:
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ValueError()
        raw = bytearray()
        async for part in request.stream():
            if len(raw) + len(part) > 65536:
                return refuse("request_too_large", 413, "The content request is too large.")
            raw.extend(part)
        body = json.loads(raw, object_pairs_hook=unique_keys,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        if type(body) is not dict or set(body) != keys or type(body["acknowledgeOverride"]) is not bool:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return refuse("invalid_request", 400, "That content request is invalid.")
    try:
        revision, fingerprint = await asyncio.to_thread(update_content, store, tenant, account_id, item_id,
            patch=body["patch"], expected_revision=body["expectedRevision"], expected_fingerprint=body["expectedFingerprint"],
            command_key=body["commandKey"], actor=body["actor"], version=2, acknowledge_override=body["acknowledgeOverride"])
    except InvalidContent as exc:
        return refuse("invalid_content", 400, str(exc))
    except Refused as exc:
        if str(exc) in {"scope unavailable", "content unavailable"}:
            return refuse("not_available", 404, "That content item is not available.")
        return refuse("content_conflict", 409, "Content changed. Refresh before saving again.")
    except Exception:
        return refuse("outcome_unconfirmed", 500, "The save outcome is not confirmed. Refresh or retry the same request.")
    return response({"ok": True, "applied": True, "accountId": account_id, "itemId": item_id,
        "commandKey": body["commandKey"], "recordedRevision": revision, "recordedFingerprint": fingerprint})
