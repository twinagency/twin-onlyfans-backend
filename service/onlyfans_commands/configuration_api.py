"""Separate, explicitly scoped creator-configuration purpose credential."""
import asyncio
import hmac
import json
import os
import re

from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse

from onlyfans_read.contract import ID_PATTERN
from onlyfans_runtime.configuration import get_configuration,update_configuration,InvalidConfiguration
from onlyfans_runtime.store import Refused
from .api import unique_keys

SCHEMA="of-configuration.v2"
HEADER="X-OnlyFans-Configuration-Version"
PREFIX="/onlyfans/configuration/v1"
router=APIRouter(prefix=PREFIX,tags=["onlyfans-configuration"])


def configured():
    token=(os.environ.get("ONLYFANS_CONFIGURATION_TOKEN") or "").strip()
    tenant=(os.environ.get("ONLYFANS_CONFIGURATION_TENANT") or "").strip()
    other=[(os.environ.get(name) or "").strip() for name in ("ONLYFANS_API_TOKEN","ONLYFANS_CONTROL_TOKEN","ONLYFANS_CAMPAIGN_TOKEN","ONLYFANS_CONTENT_TOKEN","ONLYFANS_CONNECTION_TOKEN")]
    if (os.environ.get("ONLYFANS_PROCESS_ROLE")!="api" or not token or tenant=="deployment" or
        not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}",tenant) or
        any(value and hmac.compare_digest(value.encode(),token.encode()) for value in other)):
        return None
    return token,tenant


def response(body,status=200):
    return JSONResponse({"schemaVersion":SCHEMA,**body},status_code=status,
        headers={"Cache-Control":"private, no-store",HEADER:SCHEMA})


def refuse(code,status,message):return response({"ok":False,"code":code,"error":message},status)


def authenticate(request,account_id):
    config=configured()
    if config is None:return refuse("configuration_unavailable",503,"Creator configuration editing is not configured.")
    token,tenant=config
    auth=request.headers.get("authorization","").split()
    if len(auth)!=2 or auth[0].lower()!="bearer" or not hmac.compare_digest(auth[1].encode(),token.encode()):
        return refuse("unauthorized",401,"The configuration credential was not accepted.")
    if request.headers.get(HEADER)!=SCHEMA:return refuse("unsupported_schema",400,"The configuration contract version is required.")
    if not ID_PATTERN.fullmatch(account_id):return refuse("invalid_request",400,"That account reference is invalid.")
    store=getattr(request.app.state,"control_store",None)
    if store is None:return refuse("configuration_unavailable",503,"Creator configuration editing is unavailable.")
    return store,tenant


@router.get("/accounts/{account_id}")
async def get(request:Request,account_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    try:
        data=await asyncio.to_thread(get_configuration,store,tenant,account_id)
    except Refused:return refuse("not_available",404,"That creator configuration is not available.")
    except Exception:return refuse("configuration_unavailable",503,"Creator configuration could not be read.")
    return response({"ok":True,"accountId":account_id,"configuration":data})


@router.post("/accounts/{account_id}")
async def post(request:Request,account_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    try:
        if request.headers.get("content-type","").split(";")[0].strip().lower()!="application/json":raise ValueError()
        raw=bytearray()
        async for part in request.stream():
            if len(raw)+len(part)>65536:return refuse("request_too_large",413,"The creator configuration request is too large.")
            raw.extend(part)
        body=json.loads(raw,object_pairs_hook=unique_keys)
        if type(body) is not dict or set(body)!={"patch","expectedRevision","expectedFingerprint","commandKey","actor"}:raise ValueError()
    except (ValueError,TypeError,UnicodeError,RecursionError):
        return refuse("invalid_request",400,"That creator configuration request is invalid.")
    try:
        revision,fingerprint=await asyncio.to_thread(update_configuration,store,tenant,account_id,
            patch=body["patch"],expected_revision=body["expectedRevision"],expected_fingerprint=body["expectedFingerprint"],
            command_key=body["commandKey"],actor=body["actor"])
    except InvalidConfiguration as exc:return refuse("invalid_configuration",400,str(exc))
    except Refused as exc:
        if str(exc) in {"scope unavailable","configuration unavailable"}:return refuse("not_available",404,"That creator configuration is not available.")
        return refuse("configuration_conflict",409,"Configuration changed. Refresh before saving again.")
    except Exception:return refuse("outcome_unconfirmed",500,"The save outcome is not confirmed. Refresh or retry the same request.")
    return response({"ok":True,"applied":True,"accountId":account_id,"commandKey":body["commandKey"],
        "recordedRevision":revision,"recordedFingerprint":fingerprint})
