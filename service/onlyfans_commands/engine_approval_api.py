"""Plan C operator HTTP surface (contract: of-engine-operator.v2), operator-only.

Routes under /onlyfans/control/v2/accounts/{account_id}/ :
  GET  drafts                                         live drafts, exact ordered parts
  POST drafts/{requestId}/{generation}/approve|reject|edit
  POST conversations/{conversationId}/stop|takeover|handback
  GET  unresolved                                     unresolved work, each with an owner and a next step
  POST unresolved/{id}/reconcile

Same credential as the v1 operator router (the control bearer token, constant-time compare), the schema header
X-OnlyFans-Engine-Operator-Version: of-engine-operator.v2, strict JSON (unique keys, no non-finite numbers, 8 KB cap).
The agency-scoped path is refused: this is the internal operator surface. All behaviour is in
conversation_engine/operator_v2.py; nothing here approves, hands back or sends by itself.
"""
import asyncio
import hmac
import json
import re
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from onlyfans_commands.api import unique_keys
from onlyfans_commands.settings import configured
from onlyfans_read.agency_scope import current_scope
from onlyfans_read.contract import ID_PATTERN, conversation_id_for, fan_id_from_conversation_id
from onlyfans_runtime.store import Refused
from conversation_engine import operator_v2 as v2

SCHEMA = 'of-engine-operator.v2'
HEADER = 'X-OnlyFans-Engine-Operator-Version'
MAX_BODY = 8192
router = APIRouter(prefix='/onlyfans/control/v2', tags=['onlyfans-engine-operator-v2'])


def answer(body, status=200):
    return JSONResponse({'schemaVersion': SCHEMA, **body}, status_code=status,
                        headers={'Cache-Control': 'private, no-store', HEADER: SCHEMA})


def gate(request, account_id):
    """(store, tenant, None) when the caller may proceed, else (None, None, response)."""
    config = configured()
    if config is None:
        return None, None, answer({'ok': False, 'code': 'unavailable'}, 503)
    token, tenant = config
    auth = request.headers.get('authorization', '').split()
    if len(auth) != 2 or auth[0].lower() != 'bearer' or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return None, None, answer({'ok': False, 'code': 'unauthorized'}, 401)
    if request.headers.get(HEADER) != SCHEMA:
        return None, None, answer({'ok': False, 'code': 'unsupported_schema'}, 400)
    if current_scope.get() is not None:
        return None, None, answer({'ok': False, 'code': 'operator_only'}, 403)
    store = getattr(request.app.state, 'control_store', None)
    if store is None:
        return None, None, answer({'ok': False, 'code': 'unavailable'}, 503)
    if not ID_PATTERN.fullmatch(account_id):
        return None, None, answer({'ok': False, 'code': 'invalid_request'}, 400)
    return store, tenant, None


def _nonfinite(_):
    raise ValueError('nonfinite value')


async def read_body(request):
    """The parsed JSON object, or a response (413 too large). ValueError for anything malformed."""
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise ValueError('content type')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY:
            return answer({'ok': False, 'code': 'too_large'}, 413)
        raw.extend(chunk)
    body = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=_nonfinite)
    if type(body) is not dict:
        raise ValueError('body')
    return body


def failure(exc):
    if isinstance(exc, v2.NotFound):
        return answer({'ok': False, 'code': 'not_found'}, 404)
    if isinstance(exc, v2.Conflict):
        return answer({'ok': False, 'code': 'conflict'}, 409)
    if isinstance(exc, Refused):
        return answer({'ok': False, 'code': 'conflict'}, 409)
    return answer({'ok': False, 'code': 'invalid_request'}, 400)


BAD = (v2.BadRequest, ValueError, TypeError, UnicodeError, RecursionError, KeyError)


async def _command(request, account_id, work):
    store, tenant, refusal = gate(request, account_id)
    if refusal is not None:
        return refusal
    try:
        body = await read_body(request)
        if isinstance(body, JSONResponse):
            return body
        result = await asyncio.to_thread(work, store, tenant, body)
    except (v2.NotFound, v2.Conflict, Refused) as exc:
        return failure(exc)
    except BAD:
        return answer({'ok': False, 'code': 'invalid_request'}, 400)
    return answer(result)


@router.get('/accounts/{account_id}/pilot-scope')
async def pilot_scope_view(request: Request, account_id: str):
    store, tenant, refusal = gate(request, account_id)
    if refusal is not None:
        return refusal
    try:
        from conversation_engine.pilot_scope import describe
        result = await asyncio.to_thread(describe, store, tenant, account_id)
    except (v2.NotFound, Refused) as exc:
        return failure(exc)
    return answer({'ok':True, **result})


@router.post('/accounts/{account_id}/pilot-scope/configure')
async def pilot_scope_configure(request: Request, account_id: str):
    from conversation_engine.pilot_scope import configure
    def work(store, tenant, body):
        if set(body) != {'config', 'expectedRevision', 'commandKey', 'actor', 'reason'}:
            raise v2.BadRequest('exact configuration fields required')
        return configure(store, tenant, account_id, body['config'], expected_revision=body['expectedRevision'],
                         command_key=body['commandKey'], actor=body['actor'], reason=body['reason'])
    return await _command(request, account_id, work)


@router.post('/accounts/{account_id}/pilot-scope/status')
async def pilot_scope_status(request: Request, account_id: str):
    from conversation_engine.pilot_scope import set_status
    def work(store, tenant, body):
        if set(body) != {'status', 'expectedRevision', 'commandKey', 'actor', 'reason'}:
            raise v2.BadRequest('exact status fields required')
        return set_status(store, tenant, account_id, body['status'], expected_revision=body['expectedRevision'],
                          command_key=body['commandKey'], actor=body['actor'], reason=body['reason'])
    return await _command(request, account_id, work)


@router.get('/accounts/{account_id}/drafts')
async def drafts(request: Request, account_id: str):
    store, tenant, refusal = gate(request, account_id)
    if refusal is not None:
        return refusal
    try:
        items = await asyncio.to_thread(v2.list_drafts, store, tenant, account_id)
        receipts = await asyncio.to_thread(v2.list_receipts, store, tenant, account_id)
    except (v2.NotFound, Refused) as exc:
        return failure(exc)
    return answer({'ok': True, 'accountId': account_id, 'drafts': items, 'receipts': receipts})


@router.get('/accounts/{account_id}/unresolved')
async def unresolved(request: Request, account_id: str):
    store, tenant, refusal = gate(request, account_id)
    if refusal is not None:
        return refusal
    try:
        items = await asyncio.to_thread(v2.unresolved, store, tenant, account_id)
    except (v2.NotFound, Refused) as exc:
        return failure(exc)
    return answer({'ok': True, 'accountId': account_id, 'items': items})


@router.post('/accounts/{account_id}/drafts/{request_id}/{generation}/{action}')
async def draft_command(request: Request, account_id: str, request_id: str, generation: str, action: str):
    if action not in ('approve', 'reject', 'edit') or not re.fullmatch(r'[0-9]{1,10}', generation):
        return answer({'ok': False, 'code': 'not_found'}, 404)
    return await _command(request, account_id, lambda store, tenant, body: v2.draft_command(
        store, tenant, account_id, request_id, int(generation), action, body))


@router.post('/accounts/{account_id}/conversations/{conversation_id}/{action}')
async def conversation_command(request: Request, account_id: str, conversation_id: str, action: str):
    if action not in ('stop', 'takeover', 'handback'):
        return answer({'ok': False, 'code': 'not_found'}, 404)
    fan = fan_id_from_conversation_id(conversation_id, account_id) if ID_PATTERN.fullmatch(account_id) else None
    if fan is None or not 0 < fan < 2 ** 63 or conversation_id_for(account_id, fan) != conversation_id:
        # Same answer for a missing conversation and one that belongs to another account.
        return answer({'ok': False, 'code': 'invalid_request'}, 400)
    return await _command(request, account_id, lambda store, tenant, body: v2.conversation_command(
        store, tenant, account_id, fan, action, body))


@router.post('/accounts/{account_id}/unresolved/{item_id}/reconcile')
async def reconcile(request: Request, account_id: str, item_id: str):
    return await _command(request, account_id, lambda store, tenant, body: v2.reconcile(
        store, tenant, account_id, item_id, body))
