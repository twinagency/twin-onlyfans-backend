"""Versioned request workflow; writes are limited to synthetic engine owners."""
import asyncio
import hmac
import json
import re
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from onlyfans_commands.api import unique_keys
from onlyfans_commands.settings import configured
from onlyfans_read.agency_scope import actor_allowed
from onlyfans_read.contract import ID_PATTERN, conversation_id_for, fan_id_from_conversation_id
from onlyfans_runtime.store import Refused
from conversation_engine.operator import execute

SCHEMA = 'of-engine-operator.v1'
HEADER = 'X-OnlyFans-Engine-Operator-Version'
router = APIRouter(prefix='/onlyfans/control/v1', tags=['onlyfans-engine-operator'])


def answer(body, status=200):
    return JSONResponse({'schemaVersion':SCHEMA, **body}, status_code=status,
                        headers={'Cache-Control':'private, no-store', HEADER:SCHEMA})


@router.post('/accounts/{account_id}/conversations/{conversation_id}/requests/{request_id}')
async def command(request: Request, account_id: str, conversation_id: str, request_id: str):
    config = configured()
    if config is None:
        return answer({'ok':False, 'code':'unavailable'}, 503)
    token, tenant = config
    auth = request.headers.get('authorization', '').split()
    if len(auth) != 2 or auth[0].lower() != 'bearer' or not hmac.compare_digest(auth[1].encode(), token.encode()):
        return answer({'ok':False, 'code':'unauthorized'}, 401)
    if request.headers.get(HEADER) != SCHEMA:
        return answer({'ok':False, 'code':'unsupported_schema'}, 400)
    store = getattr(request.app.state, 'control_store', None)
    if store is None:
        return answer({'ok':False, 'code':'unavailable'}, 503)
    try:
        fan = fan_id_from_conversation_id(conversation_id, account_id)
        if (not ID_PATTERN.fullmatch(account_id) or fan is None or not 0 < fan < 2**63
                or conversation_id_for(account_id, fan) != conversation_id
                or not re.fullmatch(r'[0-9a-f-]{36}', request_id)
                or request.headers.get('content-type','').split(';')[0].strip().lower() != 'application/json'):
            raise ValueError()
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 4096:
                return answer({'ok':False, 'code':'too_large'}, 413)
            raw.extend(chunk)
        def nonfinite(_): raise ValueError('nonfinite value')
        body = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=nonfinite)
        required = {'action','expectedRevision','commandKey','actor'}
        if type(body) is not dict or not required.issubset(body) or set(body) - (required | {'target','evidenceRef'}):
            raise ValueError()
        action = body['action']
        if not actor_allowed(body['actor'], capability='chat'):
            raise ValueError()
        result = await asyncio.to_thread(execute, store, tenant, account_id, request_id, action=action,
            expected_revision=body['expectedRevision'], command_key=body['commandKey'], actor=body['actor'],
            target=body.get('target'), evidence_ref=body.get('evidenceRef'), expected_fan_id=fan)
    except Refused:
        return answer({'ok':False, 'code':'scope_or_revision_conflict'}, 409)
    except (ValueError, TypeError, UnicodeError, RecursionError, KeyError):
        return answer({'ok':False, 'code':'invalid_request'}, 400)
    return answer({'ok':True, 'accountId':account_id, 'conversationId':conversation_id,
                   'action':action, 'commandKey':body['commandKey'], **result})
