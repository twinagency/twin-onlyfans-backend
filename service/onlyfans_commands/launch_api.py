"""Launch routes (history recovery and memory, proactive engagement) under the configuration purpose.

Same credential, tenant, version header and agency rules as personality: reads narrow to the creator grant,
every POST needs canManage and an allowed actor. Nothing here contacts the provider or sends; engagement
settings default off and a send still needs the worker flag, saved settings and the common delivery authority.
"""
import asyncio
import json
import re
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from onlyfans_read.agency_scope import actor_allowed
from onlyfans_runtime.launch_engagement.contracts import Policy, load_object
from onlyfans_runtime.launch_engagement.models import Action, Settings
from onlyfans_runtime.launch_engagement.service import EngagementService
from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory
from onlyfans_runtime.launch_history_memory.recovery import HistoryRecovery
from onlyfans_runtime.store import Refused
from onlyfans_runtime.launch_pagination import decode, encode, query_bounds
from .api import unique_keys
from .configuration_api import authenticate, refuse, response

PREFIX = "/onlyfans/configuration/v1/accounts/{account_id}"
engagement_router = APIRouter(prefix=PREFIX + "/engagement", tags=["onlyfans-engagement"])
history_router = APIRouter(prefix=PREFIX + "/history-memory", tags=["onlyfans-history-memory"])
MAX_BODY = 16384
NOT_FOUND = {"scope unavailable", "engagement action unavailable"}


def _z(value):
    return value.isoformat() + "Z" if value is not None else None


def action_view(row):
    """Labels stay distinct: scheduled, queued, accepted. A cancel request is never shown as unsent."""
    return {"actionId": row.action_id, "kind": row.kind, "status": row.status, "reason": row.reason,
            "fanId": str(row.fan_id), "dueAt": _z(row.due_at), "expiresAt": _z(row.expires_at),
            "intentId": row.intent_id, "cancelRequested": row.cancel_requested_at is not None}
    # "accepted" means the transport accepted the message. Purchases come only from the ledger, never from here.


async def _body(request, required, optional=(), *, capability="manage"):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise Refused("invalid JSON required")
    raw = bytearray()
    async for part in request.stream():
        if len(raw) + len(part) > MAX_BODY:
            raise Refused("invalid request too large")
        raw.extend(part)
    def nonfinite(_):
        raise ValueError("nonfinite JSON")
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys, parse_constant=nonfinite)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Refused("invalid JSON") from None
    if type(body) is not dict or not set(required) <= set(body) <= set(required) | set(optional):
        raise Refused("invalid fields")
    if not actor_allowed(body.get("actor"), capability=capability):
        raise Refused("invalid managing actor")
    return body


def _fan(value):
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]{0,17}", value) is None:
        raise Refused("invalid fan reference")
    return int(value)


async def _run(request, account_id, work, *, required=None, optional=(), capability="manage"):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    store, tenant = context
    try:
        body = await _body(request, required, optional, capability=capability) if required is not None else None
        result = await asyncio.to_thread(work, store, tenant, body)
    except Refused as exc:
        text = str(exc)
        if text in NOT_FOUND:
            return refuse("not_available", 404, "That resource is not available.")
        if "invalid" in text:
            return refuse("invalid_request", 400, "That request needs correction.")
        if "permission" in text:
            return refuse("forbidden", 403, "That action needs management permission.")
        return refuse("conflict", 409, "The state changed or the command was reused. Refresh before continuing.")
    except Exception:
        return refuse("outcome_unconfirmed", 500, "The outcome is not confirmed. Refresh or retry the same request.")
    return response({"ok": True, "accountId": account_id, **result})


# ── engagement ──────────────────────────────────────────────────────────

@engagement_router.get("")
async def engagement_settings(request: Request, account_id: str):
    def work(store, tenant, _):
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account_id)
            row = s.get(Settings, account_id)
            current = row is not None and row.binding_id == runtime.binding_id
            policy = Policy.parse(load_object(row.settings_json)) if current else Policy()
            return {"engagementVersion": "of-engagement.v1", "revision": row.revision if row is not None else 0,
                    "bindingCurrent": current or row is None, "settings": json.loads(policy.json())}
    return await _run(request, account_id, work)


@engagement_router.post("/settings")
async def engagement_configure(request: Request, account_id: str):
    def work(store, tenant, body):
        revision = EngagementService(store).configure(tenant, account_id, expected_revision=body["expectedRevision"],
            command_key=body["commandKey"], actor=body["actor"], settings=body["settings"])
        return {"engagementVersion": "of-engagement.v1", "revision": revision, "commandKey": body["commandKey"]}
    return await _run(request, account_id, work, required=("expectedRevision", "commandKey", "actor", "settings"))


@engagement_router.get("/actions")
async def engagement_actions(request: Request, account_id: str):
    def work(store, tenant, _):
        cursor, limit = query_bounds(request.query_params)
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account_id)
            position = decode(cursor, "engagement-actions", account_id, runtime.binding_id)
            query = select(Action).where(Action.account_id == account_id, Action.binding_id == runtime.binding_id)
            if position is not None:
                if (type(position) is not list or len(position) != 2 or type(position[0]) is not str
                        or type(position[1]) is not str or re.fullmatch(r"[a-f0-9-]{36}", position[1]) is None):
                    raise Refused("invalid action cursor")
                try:
                    stamp = datetime.fromisoformat(position[0])
                except ValueError:
                    raise Refused("invalid action cursor") from None
                if stamp.tzinfo is not None:
                    raise Refused("invalid action cursor")
                query = query.where(or_(Action.created_at < stamp,
                    and_(Action.created_at == stamp, Action.action_id > position[1])))
            rows = s.scalars(query.order_by(Action.created_at.desc(), Action.action_id).limit(limit + 1)).all()
            page = rows[:limit]
            return {"engagementVersion": "of-engagement.v1", "actions": [action_view(row) for row in page],
                "nextCursor": encode("engagement-actions", account_id, runtime.binding_id,
                    [page[-1].created_at.isoformat(), page[-1].action_id]) if len(rows) > limit else None}
    return await _run(request, account_id, work)


@engagement_router.get("/actions/{action_id}")
async def engagement_action(request: Request, account_id: str, action_id: str):
    # Read-only projection of the stored row; the worker (or POST refresh) keeps it current.
    def work(store, tenant, _):
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account_id)
            row = s.get(Action, (account_id, action_id)) if re.fullmatch(r"[a-f0-9-]{36}", action_id) else None
            if row is None or row.binding_id != runtime.binding_id:
                raise Refused("engagement action unavailable")
            return {"engagementVersion": "of-engagement.v1", "action": action_view(row)}
    return await _run(request, account_id, work)


@engagement_router.post("/actions/{action_id}/refresh")
async def engagement_refresh(request: Request, account_id: str, action_id: str):
    def work(store, tenant, body):
        EngagementService(store).detail(tenant, account_id, action_id)
        return engagement_action_sync(store, tenant, account_id, action_id)
    return await _run(request, account_id, work, required=("actor",))


def engagement_action_sync(store, tenant, account_id, action_id):
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account_id)
        row = s.get(Action, (account_id, action_id))
        if row is None or row.binding_id != runtime.binding_id:
            raise Refused("engagement action unavailable")
        return {"engagementVersion": "of-engagement.v1", "action": action_view(row)}


@engagement_router.post("/actions/{action_id}/cancel")
async def engagement_cancel(request: Request, account_id: str, action_id: str):
    def work(store, tenant, body):
        EngagementService(store).cancel(tenant, account_id, action_id, actor=body["actor"],
                                        reason=body.get("reason", "operator_cancelled"))
        return engagement_action_sync(store, tenant, account_id, action_id)
    return await _run(request, account_id, work, required=("actor",), optional=("reason",))


# ── history recovery and memory ─────────────────────────────────────────

@history_router.get("/fans")
async def history_fans(request: Request, account_id: str):
    def work(store, tenant, _):
        cursor, limit = query_bounds(request.query_params)
        return HistoricalMemory(store).list_fans(tenant, account_id, cursor=cursor, limit=limit)
    return await _run(request, account_id, work)

@history_router.get("/progress")
async def history_progress(request: Request, account_id: str):
    return await _run(request, account_id, lambda store, tenant, _: {
        "progress": HistoryRecovery(store).progress(tenant, account_id)})


@history_router.get("/fans/{fan_id}/progress")
async def history_fan_progress(request: Request, account_id: str, fan_id: str):
    def work(store, tenant, _):
        fan = _fan(fan_id)
        return {"recovery": HistoryRecovery(store).progress(tenant, account_id, fan_id=fan),
                "memory": HistoricalMemory(store).progress(tenant, account_id, fan)}
    return await _run(request, account_id, work)


@history_router.get("/fans/{fan_id}/memory")
async def history_memory(request: Request, account_id: str, fan_id: str):
    def work(store, tenant, _):
        if set(request.query_params) - {"query"} or len(request.query_params.getlist("query")) > 1:
            raise Refused("invalid query")
        return {"memory": HistoricalMemory(store).read(tenant, account_id, _fan(fan_id),
                                                       query=request.query_params.get("query", ""))}
    return await _run(request, account_id, work)


@history_router.post("/fans/{fan_id}/corrections")
async def history_correct(request: Request, account_id: str, fan_id: str):
    def work(store, tenant, body):
        result = HistoricalMemory(store).correct(tenant, account_id, _fan(fan_id), field=body["field"],
            operation=body["operation"], value=body["value"], expected_version=body["expectedVersion"],
            command_key=body["commandKey"], actor=body["actor"], reason=body["reason"])
        return {"correction": result, "commandKey": body["commandKey"]}
    return await _run(request, account_id, work, required=("field", "operation", "value", "expectedVersion",
                                                           "commandKey", "actor", "reason"), capability="chat")


@history_router.post("/backfill")
async def history_backfill(request: Request, account_id: str):
    def work(store, tenant, body):
        HistoryRecovery(store).request_account(tenant, account_id)  # resumes; never resets a cursor
        return {"requested": "account"}
    return await _run(request, account_id, work, required=("actor",))


@history_router.post("/fans/{fan_id}/backfill")
async def history_fan_backfill(request: Request, account_id: str, fan_id: str):
    def work(store, tenant, body):
        HistoryRecovery(store).request_fan(tenant, account_id, _fan(fan_id))
        return {"requested": "fan"}
    return await _run(request, account_id, work, required=("actor",))

