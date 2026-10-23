"""Scoped API process from the existing backend, without legacy runtime imports.

Run with ONLYFANS_PROCESS_ROLE=api and an explicit DATABASE_URL. Schema
changes belong to `python -m service.db.schema`, never to this lifespan.
"""
import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

if os.environ.get("ONLYFANS_PROCESS_ROLE") != "api":
    raise RuntimeError("onlyfans_app requires ONLYFANS_PROCESS_ROLE=api")

from db.engine import engine, init_db
from db.schema import check_schema
from onlyfans_read.api import router, _authenticate, _refusal, _PRIVATE_HEADERS
from onlyfans_read.errors import OnlyFansReadError, Unavailable
from onlyfans_commands.api import router as control_router
from onlyfans_commands.settings import configured as control_configured
from onlyfans_commands.configuration_api import router as configuration_router,configured as configuration_configured
from onlyfans_commands.campaign_api import router as campaign_router,configured as campaign_configured
from onlyfans_commands.content_api import router as content_router,configured as content_configured
from onlyfans_commands.content_api_v2 import router as content_v2_router
from onlyfans_commands.offer_import_api import router as offer_import_router
from onlyfans_commands.connection_api import router as connection_router,configured as connection_configured
from onlyfans_commands.vault_api import router as vault_router
from onlyfans_commands.vault_media_api import router as vault_media_router


@asynccontextmanager
async def lifespan(app):
    try:
        await init_db()
        app.state.control_store = None
        if any(value() is not None for value in (control_configured,configuration_configured,campaign_configured,content_configured,connection_configured)):
            from onlyfans_runtime.store import Store
            app.state.control_store = await asyncio.to_thread(Store, os.environ["DATABASE_URL"])
        yield
    finally:
        if getattr(app.state, "control_store", None) is not None:
            await asyncio.to_thread(app.state.control_store.close)
            app.state.control_store = None
        await engine.dispose()


app = FastAPI(title="Twin OnlyFans read service", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
from onlyfans_read.agency_scope import AgencyScopeMiddleware
app.add_middleware(AgencyScopeMiddleware)
app.include_router(router)
app.include_router(control_router)
app.include_router(configuration_router)
app.include_router(campaign_router)
app.include_router(content_router)
app.include_router(content_v2_router)
app.include_router(offer_import_router)
app.include_router(connection_router)
app.include_router(vault_router)
app.include_router(vault_media_router)


@app.middleware("http")
async def private_responses(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, X-OnlyFans-Schema-Version, X-OnlyFans-Control-Version, X-OnlyFans-Configuration-Version, X-OnlyFans-Campaign-Version, X-OnlyFans-Content-Version, X-OnlyFans-Connection-Version, X-OnlyFans-Offer-Import-Version"
    return response


@app.get("/healthz")
async def health():
    return {"ok": True, "role": "api"}


@app.get("/onlyfans/api/v1/ready")
async def ready(request: Request):
    try:
        await _authenticate(request)
        async with engine.connect() as connection:
            await connection.run_sync(check_schema)
    except OnlyFansReadError as exc:
        return _refusal(exc)
    except Exception:
        return _refusal(Unavailable("schema_unavailable", "The OnlyFans database is not ready."))
    return JSONResponse({"ok": True, "role": "api", "schemaReady": True}, headers=_PRIVATE_HEADERS)
