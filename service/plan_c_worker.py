"""Default-off supervised pilot process. No activation, approval or policy change is performed here."""
import asyncio
from contextlib import asynccontextmanager
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from fans_api_adapter.webhooks import verify_signature
from onlyfans_runtime.store import Refused

_LOG = logging.getLogger(__name__)
_MAX_BODY = 1_000_000


def _work_counts(pilot):
    """Bounded operational counts; no message text, account IDs or credentials."""
    from sqlalchemy import select, and_
    from sqlalchemy.orm import Session
    from conversation_engine.pilot_scope import require, unresolved_webhook_digests
    from conversation_engine.pilot_ingress import _unreviewed
    from db.onlyfans_delivery_models import OfTextIntent
    from db.onlyfans_fans_intake_models import OfFansProjection
    from db.onlyfans_engine_models import OfFansConnection, OfEnginePilotWebhookDigest, OfEngineRequest
    store = getattr(pilot, 'store', None) or pilot.worker.engine.store
    with Session(store.engine) as s:
        runtime = store._runtime(s, pilot.tenant, pilot.account, lock=False)
        scope, config = require(s, runtime)
        def count(query):
            return len(s.execute(query.limit(101)).all())
        counts = {'unreviewedInbound':count(_unreviewed(runtime,scope,config)),
            'unknownSends':count(select(OfTextIntent.intent_id).where(
                OfTextIntent.account_id==pilot.account,OfTextIntent.binding_id==runtime.binding_id,
                OfTextIntent.status=='unknown')),
            'openRequests':count(select(OfEngineRequest.request_id).where(
                OfEngineRequest.account_id==pilot.account,OfEngineRequest.binding_id==runtime.binding_id,
                OfEngineRequest.status.not_in(('completed','declined','cancelled')))),
            'pendingProviderEvents':count(select(OfFansProjection.event_key).join(OfFansConnection,and_(
                OfFansProjection.account_id==OfFansConnection.account_id,OfFansProjection.epoch==OfFansConnection.epoch)).where(
                OfFansProjection.account_id==pilot.account,OfFansConnection.binding_id==runtime.binding_id,
                OfFansProjection.status.in_(('pending','held')))),
            'malformedHolds':count(unresolved_webhook_digests(runtime))}
        return {**counts,'countsCapped':any(v==101 for v in counts.values())}


def _response(status, code):
    return JSONResponse({'ok': status == 202, 'code': code}, status_code=status,
                        headers={'Cache-Control': 'private, no-store'})


def create_app(pilot, *, run_loop=True):
    progress = {'ticks':0, 'failures':0, 'consecutive_failures':0, 'last_success':None}

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        async def loop():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(pilot.tick)
                    progress['ticks'] += 1
                    progress['consecutive_failures'] = 0
                    progress['last_success'] = time.monotonic()
                except Exception:
                    progress['failures'] += 1
                    progress['consecutive_failures'] += 1
                    # Preserve evidence and continue on transient failures; do not log message content or credentials.
                    _LOG.warning('pilot worker tick refused or unavailable')
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
        task = asyncio.create_task(loop()) if pilot is not None and run_loop else None
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
    app = FastAPI(title='Supervised pilot worker', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pilot = pilot

    @app.get('/healthz')
    async def health():
        return {'ok': True, 'role': 'supervised-worker'}

    @app.get('/readyz')
    async def readiness():
        # Liveness alone does not prove that recovery/planning is progressing.
        # Return counters and coarse states, never exception text or identities.
        mode, sending, reason = 'unavailable', False, 'unavailable'
        work = None
        current = app.state.pilot
        if current is not None:
            from model_transport.pilot_supervised import IntakeOnlyPilot
            mode = 'intake_only' if isinstance(current, IntakeOnlyPilot) else 'active'
            if not run_loop:
                reason = 'loop_disabled'
            elif progress['consecutive_failures']:
                reason = 'loop_failed'
            elif progress['last_success'] is None:
                reason = 'loop_starting'
            elif time.monotonic() - progress['last_success'] > 30:
                reason = 'loop_stale'
            else:
                def check():
                    if isinstance(current, IntakeOnlyPilot):
                        from sqlalchemy.orm import Session
                        from conversation_engine.pilot_scope import require
                        with Session(current.store.engine) as s:
                            runtime = current.store._runtime(s, current.tenant, current.account, lock=False)
                            scope, _ = require(s, runtime)
                            return 'restart_required' if scope.status == 'active' else 'ready'
                    current.validate()
                    return 'ready'
                try:
                    reason = await asyncio.to_thread(check)
                    work = await asyncio.to_thread(_work_counts, current)
                    sending = reason == 'ready' and mode == 'active' and not work['malformedHolds']
                except Exception:
                    reason = 'scope_or_state_refused'
        ready = reason == 'ready'
        return JSONResponse({'ok':ready, 'mode':mode, 'sendingReady':sending, 'code':reason,
            'work':work, 'workHeld':bool(work and any(work[k] for k in (
                'unreviewedInbound','unknownSends','pendingProviderEvents','malformedHolds'))),
            'ticks':progress['ticks'], 'failures':progress['failures'],
            'consecutiveFailures':progress['consecutive_failures']}, status_code=200 if ready else 503,
            headers={'Cache-Control':'private, no-store'})

    @app.post('/onlyfans/webhooks/plan-c')
    async def webhook(request: Request):
        current = request.app.state.pilot
        if current is None:
            return _response(404, 'unavailable')
        signatures, keys = request.headers.getlist('signature'), request.headers.getlist('x-ofapi-idempotency-key')
        if len(signatures) != 1 or len(keys) != 1:
            return _response(401, 'unauthorized')
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _MAX_BODY:
                return _response(413, 'too_large')
            raw.extend(chunk)
        raw = bytes(raw)
        if not verify_signature(raw, signatures[0], current.secret):
            return _response(401, 'unauthorized')
        try:
            await asyncio.to_thread(current.handle, raw, signatures[0], keys[0], ingest_only=True)
        except Refused:
            return _response(409, 'scope_or_state_refused')
        except Exception:
            return _response(503, 'unavailable')
        return _response(202, 'accepted')
    return app


def main():
    if os.environ.get('TWIN_PLAN_C_SUPERVISED', '') in ('', '0'):
        return
    if os.environ.get('ONLYFANS_PROCESS_ROLE') != 'worker' or not os.environ.get('DATABASE_URL'):
        raise Refused('explicit worker role and database are required')
    from onlyfans_runtime.store import Store
    from model_transport.pilot_supervised import build_supervised_pilot
    import uvicorn
    store = Store(os.environ['DATABASE_URL'])
    try:
        pilot = build_supervised_pilot(store)
        value = os.environ.get('TWIN_PLAN_C_LISTEN_PORT', '8765')
        if not value.isascii() or not value.isdecimal() or not 1024 <= int(value) <= 65535:
            raise Refused('invalid worker port')
        uvicorn.run(create_app(pilot), host='127.0.0.1', port=int(value), access_log=False)
    finally:
        store.close()


if __name__ == '__main__':
    main()
