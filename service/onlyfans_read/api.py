"""The scoped FastAPI read surface and its smoke check.

GET only, by construction. There is no POST, PUT, PATCH or DELETE here, no
command method and no path that reaches a provider — and the offline suite
asserts that by walking this router's own routes rather than by reading this
comment.

Two things every handler does before anything else:

  1. `verify_bearer` — the service credential, or a refusal. There is no
     session fallback and no cookie fallback: a request from a signed-in
     operator's browser with no service token is refused here, which is what
     keeps a human's dashboard session from being usable as a service identity.
  2. `negotiate_schema_version` — the client states the contract it speaks, and
     a missing or unimplemented one is refused rather than defaulted.

Responses carry `Cache-Control: private, no-store`. This is private
conversation data on behalf of one tenant; a cached answer would outlive the
credential that fetched it and could be handed to the next caller of a shared
proxy.

Every refusal is JSON with the same shape the dashboard already classifies:
`{ok: false, state, code, error}` plus an HTTP status. The dashboard maps 401,
403 and 404 to a visible refusal, 503 to `unavailable`, and everything else to
an error — so a refusal here can never arrive at the UI as an empty list.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from . import service
from .auth import ServicePrincipal, negotiate_schema_version, service_configured, verify_bearer
from .contract import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_HEADER,
    SERVICE_PREFIX,
    SUPPORTED_SCHEMA_VERSIONS,
)
from .errors import OnlyFansReadError, Unavailable
from .repo import SqlOnlyFansReadRepo

log = logging.getLogger("onlyfans.read")

router = APIRouter(prefix=SERVICE_PREFIX, tags=["onlyfans-read"])

#: Private, never cached. Mirrors the dashboard's own header for these reads.
_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    SCHEMA_VERSION_HEADER: SCHEMA_VERSION,
}


def _refusal(exc: OnlyFansReadError) -> JSONResponse:
    # `expose=True` because every message in `errors.py` is written to be safe
    # by construction: none of them names a record, an id, a tenant or a table,
    # so there is nothing here to redact. The parameter exists for the case that
    # changes — an error type whose message could quote what it refused — and
    # the offline suite asserts the property directly (a refusal for account
    # 2001 must not contain "2001") rather than trusting the rule.
    log.info("onlyfans read refused: %s (%s)", exc.code, exc.message)
    return JSONResponse(exc.as_body(expose=True), status_code=exc.status, headers=_PRIVATE_HEADERS)


async def _authenticate(request: Request) -> ServicePrincipal:
    """The gate, in one place, so no handler can be written without it."""
    principal = verify_bearer(request.headers.get("authorization"))
    negotiate_schema_version(request.headers.get(SCHEMA_VERSION_HEADER))
    return principal


def _repo() -> SqlOnlyFansReadRepo:
    """The production repository.

    Built per request and cheap: it holds no connection and no state. It is a
    function rather than a module-level singleton so a test that wants to drive
    the service with a fake does so through `service.py` directly, without this
    module needing an injection seam that production could accidentally use.
    """
    return SqlOnlyFansReadRepo()


@router.get("/status")
async def status(request: Request) -> JSONResponse:
    """A smoke check that reads no account data.

    Exists so the gate can be verified from outside without asking for a
    conversation: a caller that gets a 200 here has proven it holds the service
    credential and speaks the schema version. It deliberately does NOT touch
    storage — a status that went to the database would be a data read wearing a
    health check's name.
    """
    try:
        principal = await _authenticate(request)
    except OnlyFansReadError as exc:
        return _refusal(exc)
    return JSONResponse(
        {
            "ok": True,
            "schemaVersion": SCHEMA_VERSION,
            "supportedSchemaVersions": list(SUPPORTED_SCHEMA_VERSIONS),
            "scope": "deployment" if principal.is_deployment_scope else "tenant",
            "serviceConfigured": service_configured(),
        },
        status_code=200,
        headers=_PRIVATE_HEADERS,
    )


@router.get("/accounts")
async def list_accounts(
    request: Request,
    creator: str | None = Query(default=None, description="Requested creator id (narrowing, never a grant)"),
    account: str | None = Query(default=None, description="Requested account id (narrowing, never a grant)"),
) -> JSONResponse:
    try:
        principal = await _authenticate(request)
        body = await service.list_accounts(
            _repo(), principal, creator_id=creator, account_id=account
        )
    except OnlyFansReadError as exc:
        return _refusal(exc)
    except Exception:  # noqa: BLE001 — an unexpected failure must not leak detail
        log.exception("onlyfans accounts read failed")
        return _refusal(
            Unavailable("read_failed", "The OnlyFans read did not succeed.")
        )
    return JSONResponse(body, status_code=200, headers=_PRIVATE_HEADERS)


@router.get("/accounts/{account_id}/conversations")
async def list_conversations(
    request: Request,
    account_id: str,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> JSONResponse:
    try:
        principal = await _authenticate(request)
        body = await service.list_conversations(
            _repo(), principal, account_id=account_id, cursor=cursor, limit=limit
        )
    except OnlyFansReadError as exc:
        return _refusal(exc)
    except Exception:  # noqa: BLE001
        log.exception("onlyfans conversation list read failed")
        return _refusal(Unavailable("read_failed", "The OnlyFans read did not succeed."))
    return JSONResponse(body, status_code=200, headers=_PRIVATE_HEADERS)


@router.get("/accounts/{account_id}/operations")
async def get_operations(request: Request, account_id: str) -> JSONResponse:
    """Authenticated metadata, with the same binding scope as conversations."""
    try:
        from .errors import Refused
        principal = await _authenticate(request)
        account_id = service._require_id(account_id, what="account")
        body = await _repo().operations(tenant_id=principal.tenant_id, account_id=account_id)
        if body is None:
            raise Refused("unknown_account")
    except OnlyFansReadError as exc:
        return _refusal(exc)
    except Exception:
        return _refusal(Unavailable("operations_unavailable", "The OnlyFans operational read did not succeed."))
    return JSONResponse({"schemaVersion": SCHEMA_VERSION, "operations": body}, status_code=200, headers=_PRIVATE_HEADERS)


@router.get("/accounts/{account_id}/workspace/{section}")
async def workspace(request: Request, account_id: str, section: str,
                    cursor: str | None = Query(default=None), limit: int = Query(default=25)):
    try:
        from .errors import Refused
        principal=await _authenticate(request)
        account_id=service._require_id(account_id,what="account")
        body=await _repo().workspace(tenant_id=principal.tenant_id,account_id=account_id,
            section=section,cursor=cursor,limit=limit)
        if body is None:raise Refused("unknown_account")
    except OnlyFansReadError as exc:
        return _refusal(exc)
    except Exception:
        return _refusal(Unavailable("workspace_unavailable","The OnlyFans workspace read did not succeed."))
    return JSONResponse({"schemaVersion":SCHEMA_VERSION,"workspace":body},headers=_PRIVATE_HEADERS)


@router.get("/accounts/{account_id}/conversations/{conversation_id}")
async def get_conversation(
    request: Request,
    account_id: str,
    conversation_id: str,
) -> JSONResponse:
    try:
        principal = await _authenticate(request)
        body = await service.get_conversation(
            _repo(), principal, account_id=account_id, conversation_id=conversation_id
        )
    except OnlyFansReadError as exc:
        return _refusal(exc)
    except Exception:  # noqa: BLE001
        log.exception("onlyfans conversation read failed")
        return _refusal(Unavailable("read_failed", "The OnlyFans read did not succeed."))
    return JSONResponse(body, status_code=200, headers=_PRIVATE_HEADERS)
