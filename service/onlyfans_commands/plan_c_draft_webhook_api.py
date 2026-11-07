"""Default-off HTTP ingress for the explicitly scoped Plan C draft-only worker.

Only the HMAC-authenticated raw webhook is accepted. The existing worker
persists canonical evidence before planning; this route has no send endpoint.
"""
import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fans_api_adapter.webhooks import verify_signature
from onlyfans_runtime.store import Refused

router = APIRouter()
MAX_BODY = 1_000_000


def _answer(status, code):
    return JSONResponse({'ok': status == 202, 'code': code}, status_code=status,
                        headers={'Cache-Control': 'private, no-store'})


@router.post('/onlyfans/webhooks/plan-c-draft')
async def draft_webhook(request: Request):
    pilot = getattr(request.app.state, 'plan_c_draft', None)
    if pilot is None:
        return _answer(404, 'unavailable')
    signatures = request.headers.getlist('signature')
    keys = request.headers.getlist('x-ofapi-idempotency-key')
    if len(signatures) != 1 or len(keys) != 1:
        return _answer(401, 'unauthorized')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY:
            return _answer(413, 'too_large')
        raw.extend(chunk)
    raw = bytes(raw)
    if not verify_signature(raw, signatures[0], pilot.secret):
        return _answer(401, 'unauthorized')
    try:
        await asyncio.to_thread(pilot.handle, raw, signatures[0], keys[0])
    except Refused:
        return _answer(409, 'scope_or_state_refused')
    except Exception:
        # Evidence already committed by intake stays durable. A failed response
        # allows an authenticated redelivery to reuse the same event key.
        return _answer(503, 'unavailable')
    return _answer(202, 'accepted')
