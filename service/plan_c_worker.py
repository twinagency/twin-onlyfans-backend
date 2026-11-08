"""Default-off supervised pilot process. No activation, approval or policy change is performed here."""
import asyncio
from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from fans_api_adapter.webhooks import verify_signature
from onlyfans_runtime.store import Refused

_LOG = logging.getLogger(__name__)
_MAX_BODY = 1_000_000


def _response(status, code):
    return JSONResponse({'ok': status == 202, 'code': code}, status_code=status,
                        headers={'Cache-Control': 'private, no-store'})


def create_app(pilot, *, run_loop=True):
    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        async def loop():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(pilot.tick)
                except Exception:
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
            await asyncio.to_thread(current.handle, raw, signatures[0], keys[0])
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
