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

SCHEMA="of-configuration.v3"
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
        data=await asyncio.to_thread(get_configuration,store,tenant,account_id,include_activation=request.query_params.get("activation")=="1")
    except Refused:return refuse("not_available",404,"That creator configuration is not available.")
    except Exception:return refuse("configuration_unavailable",503,"Creator configuration could not be read.")
    # Older deployed dashboards validate an exact v3 response shape. Negotiate
    # this additive field, as activation already does, before exposing it.
    if request.query_params.get("contextual") != "1":
        data.pop("fasttContextualSalesMode", None)
    if request.query_params.get("safety") != "1":
        data.pop("conversationSafetyMode", None)
    if request.query_params.get("replyPolicy") != "1":
        data.pop("nativeReplyPacingEnabled", None)
        data.pop("nativeReplyFormatEnabled", None)
    if request.query_params.get("replyTiming") != "1":
        data.pop("nativeReplyDelayMaxSeconds", None)
    if request.query_params.get("commercial") != "1":
        from onlyfans_runtime.commercial_controls import view
        for field in view({}):
            data.pop(field, None)
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


@router.get("/accounts/{account_id}/moxy")
async def moxy_view(request:Request,account_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    from moxy_replacement.controls import view
    try: return response({'ok':True,'accountId':account_id,'moxy':await asyncio.to_thread(view,store,tenant,account_id)})
    except Refused:return refuse('not_available',404,'Moxy configuration is unavailable.')
    except Exception:return refuse('configuration_unavailable',503,'Moxy configuration could not be read.')


@router.put("/accounts/{account_id}/moxy")
async def moxy_configure(request:Request,account_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    from moxy_replacement.runtime import MoxyService
    from moxy_replacement.configuration import InvalidConfiguration
    from onlyfans_read.agency_scope import actor_allowed
    try:
        if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':raise ValueError()
        raw=bytearray()
        async for part in request.stream():
            if len(raw)+len(part)>131072:return refuse('request_too_large',413,'The Moxy configuration request is too large.')
            raw.extend(part)
        body=json.loads(raw,object_pairs_hook=unique_keys)
        if type(body) is not dict or set(body)!={'expectedRevision','mode','settings','actor'}:raise ValueError()
        if not actor_allowed(body['actor']):return refuse('forbidden',403,'Moxy management permission is required.')
        if type(body['expectedRevision']) is not int or body['expectedRevision']<0:raise ValueError()
    except (ValueError,TypeError,UnicodeError,RecursionError):
        return refuse('invalid_request',400,'That Moxy configuration request is invalid.')
    try:
        result=await asyncio.to_thread(MoxyService(store).configure,tenant,account_id,
            expected_revision=body['expectedRevision'],mode=body['mode'],settings=body['settings'],actor=body['actor'])
        return response({'ok':True,'accountId':account_id,'moxy':result})
    except Refused as exc:
        if str(exc)=='scope unavailable':return refuse('not_available',404,'Moxy configuration is unavailable.')
        return refuse('configuration_refused',409,str(exc))
    except (InvalidConfiguration,ValueError,KeyError,TypeError):
        return refuse('invalid_configuration',400,'Review the source-backed Moxy configuration.')
    except Exception:return refuse('outcome_unconfirmed',500,'Refresh before retrying this Moxy change.')


@router.get("/accounts/{account_id}/preparation")
async def preparation_read(request:Request,account_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    from conversation_engine.preparation import view
    try:data=await asyncio.to_thread(view,store,tenant,account_id)
    except Refused:return refuse("not_available",404,"That preparation account is unavailable.")
    except Exception:return refuse("preparation_unavailable",503,"Preparation status could not be read.")
    return response({"ok":True,"accountId":account_id,"preparation":data})

@router.post("/accounts/{account_id}/preparation")
async def preparation_write(request:Request,account_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    from conversation_engine.preparation import configure,stop
    try:
        if request.headers.get("content-type","").split(";")[0].strip().lower()!="application/json":raise ValueError()
        raw=bytearray()
        async for part in request.stream():
            if len(raw)+len(part)>16384:return refuse("request_too_large",413,"Preparation request is too large.")
            raw.extend(part)
        body=json.loads(raw,object_pairs_hook=unique_keys)
        common={"action","expectedRevision","commandKey","actor","reason"}
        if type(body) is not dict or body.get("action") not in ("configure","stop") or set(body)!=(common|({"config"} if body["action"]=="configure" else set())):raise ValueError()
    except (ValueError,TypeError,UnicodeError,RecursionError):
        return refuse("invalid_request",400,"Invalid preparation request.")
    try:
        options={k:body[k] for k in ("actor","reason")}
        options.update(expected_revision=body["expectedRevision"],command_key=body["commandKey"])
        data=await asyncio.to_thread(configure,store,tenant,account_id,body["config"],**options) if body["action"]=="configure" else await asyncio.to_thread(stop,store,tenant,account_id,**options)
    except Refused as exc:
        if str(exc)=="scope unavailable":return refuse("not_available",404,"That preparation account is unavailable.")
        return refuse("preparation_conflict",409,"Preparation was refused. Refresh and check the disabled connector, identity and budget.")
    except Exception:return refuse("outcome_unconfirmed",500,"The outcome is unconfirmed. Retry the same preparation request.")
    return response({"ok":True,"accountId":account_id,"commandKey":body["commandKey"],"preparation":data})


@router.get("/accounts/{account_id}/commercial")
async def commercial_read(request:Request,account_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    from conversation_engine.commercial_lifecycle import view
    try:data=await asyncio.to_thread(view,store,tenant,account_id)
    except Refused:return refuse("not_available",404,"That commercial scope is unavailable.")
    except Exception:return refuse("commercial_unavailable",503,"Commercial scope status could not be read.")
    return response({"ok":True,"accountId":account_id,"commercial":data})

@router.post("/accounts/{account_id}/commercial")
async def commercial_write(request:Request,account_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    from conversation_engine.commercial_lifecycle import promote,stop
    try:
        if request.headers.get("content-type","").split(";")[0].strip().lower()!="application/json":raise ValueError()
        raw=bytearray()
        async for part in request.stream():
            if len(raw)+len(part)>16384:return refuse("request_too_large",413,"Commercial scope request is too large.")
            raw.extend(part)
        body=json.loads(raw,object_pairs_hook=unique_keys)
        common={"action","expectedRevision","commandKey","actor","reason"}
        if type(body) is not dict or body.get("action") not in ("promote","stop") or set(body)!=(common|({"config"} if body["action"]=="promote" else set())):raise ValueError()
    except (ValueError,TypeError,UnicodeError,RecursionError):
        return refuse("invalid_request",400,"Invalid commercial scope request.")
    try:
        options={k:body[k] for k in ("actor","reason")}
        options.update(expected_revision=body["expectedRevision"],command_key=body["commandKey"])
        data=await asyncio.to_thread(promote,store,tenant,account_id,body["config"],**options) if body["action"]=="promote" else await asyncio.to_thread(stop,store,tenant,account_id,**options)
    except Refused as exc:
        if str(exc)=="scope unavailable":return refuse("not_available",404,"That commercial scope is unavailable.")
        return refuse("commercial_conflict",409,"The commercial scope command was refused. Refresh and check the prepared connection, disabled runtime and limits.")
    except Exception:return refuse("outcome_unconfirmed",500,"The outcome is unconfirmed. Retry the same commercial scope request.")
    return response({"ok":True,"accountId":account_id,"commandKey":body["commandKey"],"commercial":data})
