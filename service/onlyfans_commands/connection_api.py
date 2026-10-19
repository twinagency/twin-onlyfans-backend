"""Account connection commands accept credentials without exposing them in reads."""
import asyncio,hmac,json,os,re
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse
from onlyfans_read.contract import ID_PATTERN
from onlyfans_runtime.connections import encryption_key,get_connection,save_connection,change_connection,provision_account
from onlyfans_runtime.enrollments import submit_enrollment,get_enrollments
from onlyfans_runtime.store import Refused
from .api import unique_keys
SCHEMA='of-connections.v1'
HEADER='X-OnlyFans-Connection-Version'
router=APIRouter(prefix='/onlyfans/connection/v1',tags=['onlyfans-connections'])

def configured():
    token=(os.environ.get('ONLYFANS_CONNECTION_TOKEN') or '').strip()
    tenant=(os.environ.get('ONLYFANS_CONNECTION_TENANT') or '').strip()
    if os.environ.get('ONLYFANS_PROCESS_ROLE')!='api' or not token or tenant=='deployment' or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}',tenant):return None
    for name in ['ONLYFANS_API_TOKEN','ONLYFANS_CONTROL_TOKEN','ONLYFANS_CONFIGURATION_TOKEN','ONLYFANS_CONTENT_TOKEN','ONLYFANS_CAMPAIGN_TOKEN']:
        other=(os.environ.get(name) or '').strip()
        if other and hmac.compare_digest(token.encode(),other.encode()):return None
    try:encryption_key()
    except Refused:return None
    return token,tenant

def response(body,status=200):
    return JSONResponse({'schemaVersion':SCHEMA,**body},status_code=status,headers={'Cache-Control':'private, no-store',HEADER:SCHEMA})

def failure(exc):
    if isinstance(exc,Refused):
        scope=str(exc) in {'scope unavailable','conversation unavailable'}
        return response({'ok':False,'code':'scope_unavailable' if scope else 'connection_conflict','error':'That account is unavailable.' if scope else 'The connection command could not be applied. Refresh and check the selected account.'},404 if scope else 409)
    return response({'ok':False,'code':'outcome_unconfirmed','error':'The outcome is not confirmed. Refresh or retry the same request.'},500)

def authenticate(request,account=None):
    config=configured()
    if config is None:return response({'ok':False,'code':'connections_unavailable','error':'Account connections are not configured.'},503)
    token,tenant=config;auth=request.headers.get('authorization','').split()
    if len(auth)!=2 or auth[0].lower()!='bearer' or not hmac.compare_digest(auth[1].encode(),token.encode()):return response({'ok':False,'code':'unauthorized','error':'The connection credential was not accepted.'},401)
    if request.headers.get(HEADER)!=SCHEMA:return response({'ok':False,'code':'unsupported_schema','error':'The connection version is required.'},400)
    if account is not None and not ID_PATTERN.fullmatch(account):return response({'ok':False,'code':'invalid_request','error':'Invalid account reference.'},400)
    store=getattr(request.app.state,'control_store',None)
    if store is None:return response({'ok':False,'code':'connections_unavailable','error':'Account connections are unavailable.'},503)
    return store,tenant

async def read_json(request):
    if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':raise ValueError()
    raw=bytearray()
    async for part in request.stream():
        if len(raw)+len(part)>131072:raise ValueError()
        raw.extend(part)
    def nonfinite(_):raise ValueError()
    body=json.loads(raw,object_pairs_hook=unique_keys,parse_constant=nonfinite)
    if type(body)is not dict:raise ValueError()
    return body

@router.post('/accounts')
async def provision(request:Request):
    context=authenticate(request)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    try:
        body=await read_json(request)
        if set(body)!={'creatorId','creatorName','ownerId','handle','actor','commandKey'}:raise ValueError()
    except (ValueError,TypeError,UnicodeError,RecursionError):return response({'ok':False,'code':'invalid_request','error':'Invalid account registration.'},400)
    try:
        account=await asyncio.to_thread(provision_account,store,tenant,creator_id=body['creatorId'],creator_name=body['creatorName'],owner_id=body['ownerId'],handle=body['handle'],command_key=body['commandKey'],actor=body['actor'])
        return response({'ok':True,'applied':True,'accountId':account,'commandKey':body['commandKey'],'status':'unconfigured'})
    except Exception as exc:return failure(exc)

@router.get('/accounts/{account_id}')
async def get(request:Request,account_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    try:return response({'ok':True,'connection':await asyncio.to_thread(get_connection,store,tenant,account_id)})
    except Exception as exc:return failure(exc)

@router.post('/accounts/{account_id}')
async def command(request:Request,account_id:str):
    context=authenticate(request,account_id)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    try:
        body=await read_json(request)
        if set(body)!={'action','payload','expectedRevision','actor','commandKey'} or type(body['action'])is not str:raise ValueError()
        if body['action']not in {'save','session','verify','activate','pause'}:raise ValueError()
    except (ValueError,TypeError,UnicodeError,RecursionError):return response({'ok':False,'code':'invalid_request','error':'Invalid account connection request.'},400)
    try:
        args=dict(payload=body['payload'],expected_revision=body['expectedRevision'],command_key=body['commandKey'],actor=body['actor'])
        if body['action'] in {'save','session'}:revision=await asyncio.to_thread(save_connection,store,tenant,account_id,session_only=body['action']=='session',**args)
        else:revision=await asyncio.to_thread(change_connection,store,tenant,account_id,action=body['action'],**args)
        return response({'ok':True,'applied':True,'accountId':account_id,'commandKey':body['commandKey'],'recordedRevision':revision,'action':body['action']})
    except Exception as exc:return failure(exc)


@router.get('/enrollments')
async def enrollments(request:Request):
    context=authenticate(request)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    creator=request.query_params.get('creator','')
    try:return response({'ok':True,'enrollments':await asyncio.to_thread(get_enrollments,store,tenant,creator)})
    except Exception as exc:return failure(exc)

@router.post('/enrollments')
async def enroll(request:Request):
    context=authenticate(request)
    if isinstance(context,JSONResponse):return context
    store,tenant=context
    try:
        body=await read_json(request)
        if set(body)!={'creatorId','creatorName','handle','session','actor','commandKey'}:raise ValueError()
    except (ValueError,TypeError,UnicodeError,RecursionError):return response({'ok':False,'code':'invalid_request','error':'Invalid connection enrollment.'},400)
    try:
        result=await asyncio.to_thread(submit_enrollment,store,tenant,creator_id=body['creatorId'],creator_name=body['creatorName'],handle=body['handle'],payload=body['session'],actor=body['actor'],command_key=body['commandKey'])
        return response({'ok':True,'enrollment':result})
    except Exception as exc:return failure(exc)
