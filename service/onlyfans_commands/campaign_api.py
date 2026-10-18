"""Explicitly scoped campaign-purpose credential and strict bounded JSON."""
import asyncio
import hmac
import json
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from onlyfans_read.contract import ID_PATTERN
from onlyfans_runtime.campaigns import CampaignService, InvalidCampaign, identity
from onlyfans_runtime.store import Refused
from .api import unique_keys

SCHEMA = "of-campaigns.v1"
HEADER = "X-OnlyFans-Campaign-Version"
PREFIX = "/onlyfans/campaign/v1"
router = APIRouter(prefix=PREFIX, tags=["onlyfans-campaigns"])
MAX_BODY = 131072


def configured():
    token = (os.environ.get("ONLYFANS_CAMPAIGN_TOKEN") or "").strip()
    tenant = (os.environ.get("ONLYFANS_CAMPAIGN_TENANT") or "").strip()
    others = [(os.environ.get(name) or "").strip() for name in
        ("ONLYFANS_API_TOKEN", "ONLYFANS_CONTROL_TOKEN", "ONLYFANS_CONFIGURATION_TOKEN",
         "ONLYFANS_CONTENT_TOKEN", "ONLYFANS_CONNECTION_TOKEN")]
    if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "api" or not token or tenant == "deployment"
            or not ID_PATTERN.fullmatch(tenant)
            or any(other and hmac.compare_digest(token.encode(), other.encode()) for other in others)):
        return None
    return token, tenant


def response(body, status=200):
    return JSONResponse({"schemaVersion": SCHEMA, **body}, status_code=status,
        headers={"Cache-Control": "private, no-store", HEADER: SCHEMA})


def refuse(code, status, message):
    return response({"ok": False, "code": code, "error": message}, status)


def authenticate(request, account_id):
    config = configured()
    if config is None:
        return refuse("campaigns_unavailable", 503, "Campaigns are not configured.")
    token, tenant = config
    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return refuse("unauthorized", 401, "The campaign credential was not accepted.")
    if request.headers.get(HEADER) != SCHEMA:
        return refuse("unsupported_schema", 400, "The campaign contract version is required.")
    if not ID_PATTERN.fullmatch(account_id):
        return refuse("invalid_request", 400, "That account reference is invalid.")
    store = getattr(request.app.state, "control_store", None)
    if store is None:
        return refuse("campaigns_unavailable", 503, "Campaigns are unavailable.")
    return CampaignService(store), tenant


async def read_json(request):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise InvalidCampaign("JSON required")
    raw = bytearray()
    async for part in request.stream():
        if len(raw) + len(part) > MAX_BODY:
            raise InvalidCampaign("campaign request too large")
        raw.extend(part)
    try:
        def nonfinite(_):
            raise ValueError("non-finite JSON")
        body = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=nonfinite)
        if type(body) is not dict:
            raise ValueError()
        return body
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidCampaign("invalid JSON") from None


def failure(exc, *, read=False):
    if isinstance(exc, InvalidCampaign):
        return refuse("invalid_request", 400, "That campaign request is invalid.")
    if isinstance(exc, Refused):
        if str(exc) in {"scope unavailable", "campaign unavailable", "campaign audience unavailable"}:
            return refuse("scope_unavailable", 404, "That campaign or audience is not available.")
        return refuse("campaign_conflict", 409, "The campaign could not be applied. Refresh before trying again.")
    return refuse("campaigns_unavailable" if read else "outcome_unconfirmed", 503 if read else 500,
        "Campaigns could not be read." if read else "The outcome is not confirmed. Refresh or retry the same request.")


@router.get("/accounts/{account_id}/campaigns")
async def list_campaigns(request: Request, account_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    service, tenant = context
    try:
        result = await asyncio.to_thread(service.list, tenant, account_id)
    except Exception as exc:
        return failure(exc, read=True)
    return response({"ok": True, "accountId": account_id, **result})


@router.get("/accounts/{account_id}/campaigns/{campaign_id}")
async def get_campaign(request: Request, account_id: str, campaign_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    service, tenant = context
    try:
        result = await asyncio.to_thread(service.detail, tenant, account_id, campaign_id)
    except Exception as exc:
        return failure(exc, read=True)
    return response({"ok": True, "accountId": account_id, **result})


@router.post("/accounts/{account_id}/campaigns")
async def create_campaign(request: Request, account_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    service, tenant = context
    try:
        body = await read_json(request)
        required = {"commandKey", "actor", "name", "body", "audience", "scheduledAt"}
        if set(body) not in (required, required | {"catalogItemId"}):
            raise InvalidCampaign("invalid campaign fields")
        if type(body["actor"]) is not str or not re.fullmatch(r"op-session:[a-f0-9]{64}", body["actor"]):
            raise InvalidCampaign("invalid campaign actor")
        result = await asyncio.to_thread(service.create, tenant, account_id,
            command_key=body["commandKey"], actor=body["actor"], name=body["name"], body=body["body"],
            audience=body["audience"], scheduled_at=body["scheduledAt"], catalog_item_id=body.get("catalogItemId"))
    except Exception as exc:
        return failure(exc)
    return response({"ok": True, "accountId": account_id, "commandKey": body["commandKey"], **result})


@router.post("/accounts/{account_id}/campaigns/{campaign_id}/actions")
async def campaign_action(request: Request, account_id: str, campaign_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    service, tenant = context
    try:
        body = await read_json(request)
        if (set(body) != {"commandKey", "actor", "expectedRevision", "action"}
                or not identity(campaign_id) or type(body["action"]) is not str):
            raise InvalidCampaign("invalid campaign action")
        if type(body["actor"]) is not str or not re.fullmatch(r"op-session:[a-f0-9]{64}", body["actor"]):
            raise InvalidCampaign("invalid campaign actor")
        result = await asyncio.to_thread(service.action, tenant, account_id, campaign_id,
            command_key=body["commandKey"], actor=body["actor"], expected_revision=body["expectedRevision"], action=body["action"])
    except Exception as exc:
        return failure(exc)
    return response({"ok": True, "accountId": account_id, "commandKey": body["commandKey"], "action": body["action"], **result})
