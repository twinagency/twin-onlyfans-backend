"""Optional BFF narrowing, in addition to every route's purpose credential.

An absent scope keeps the internal service contract. A present malformed scope
never falls back. ContextVars isolate concurrent requests and copy into to_thread.
The dashboard derives this only from verified memberships and creator assignments.
"""
from contextvars import ContextVar
from dataclasses import dataclass
import json
import re
import os

HEADER = "x-onlyfans-agency-scope"
VERSION_HEADER = "X-OnlyFans-Access-Version"
VERSION = "of-agency.v1"
UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
ACTOR_PATTERN = rf"(?:op-session:[a-f0-9]{{64}}|agency-user:{UUID}:{UUID})"


@dataclass(frozen=True)
class AgencyScope:
    creator_ids: tuple[str, ...]
    actor: str
    can_manage: bool
    can_chat: bool = False
    version: str = VERSION


current_scope: ContextVar[AgencyScope | None] = ContextVar("onlyfans_agency_scope", default=None)


def parse_scope(raw):
    if raw is None:
        return None
    if len(raw) > 12288:
        raise ValueError("invalid agency scope")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError("duplicate agency scope key")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique)
    if (type(value) is not dict or set(value) not in ({"creatorIds", "actor", "canManage"}, {"creatorIds", "actor", "canManage", "canChat"})
            or ("canChat" in value and type(value["canChat"]) is not bool)
            or type(value["creatorIds"]) is not list or type(value["canManage"]) is not bool
            or type(value["actor"]) is not str or not re.fullmatch(rf"agency-user:{UUID}:{UUID}", value["actor"])
            or any(type(item) is not str or not re.fullmatch(UUID, item) for item in value["creatorIds"])):
        raise ValueError("invalid agency scope")
    return AgencyScope(tuple(sorted(set(value["creatorIds"]))), value["actor"], value["canManage"], value.get("canChat", False),
        "of-agency.v2" if "canChat" in value else VERSION)


def creator_allowed(creator_id):
    scope = current_scope.get()
    return scope is None or creator_id in scope.creator_ids


def actor_allowed(actor, *, capability="manage"):
    scope = current_scope.get()
    # Internal import tasks are named explicitly, never as fabricated users.
    # They exist only in the worker and cannot enter an HTTP agency scope.
    if (scope is None and os.environ.get("ONLYFANS_PROCESS_ROLE") == "worker"
            and type(actor) is str and re.fullmatch(r"worker-import:[a-f0-9]{64}", actor)):
        return True
    return (type(actor) is str and re.fullmatch(ACTOR_PATTERN, actor) is not None
            and (scope is None or (scope.can_manage or capability == "chat" and scope.can_chat) and actor == scope.actor))


async def lock_read_scope(session, account_id):
    """Hold the live binding while reading private data in this transaction."""
    scope = current_scope.get()
    if scope is None:
        return
    from sqlalchemy import select
    from db.models import OfCreatorBinding
    from .errors import Refused
    binding = select(OfCreatorBinding.id).where(OfCreatorBinding.account_id == account_id,
        OfCreatorBinding.channel == "onlyfans", OfCreatorBinding.revoked_at.is_(None),
        OfCreatorBinding.creator_id.in_(scope.creator_ids)).with_for_update(read=True)
    if await session.scalar(binding) is None:
        raise Refused("unknown_account")


class AgencyScopeMiddleware:
    """ASGI middleware keeps the scope for the entire streaming response."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.datastructures import Headers
        from starlette.responses import JSONResponse
        try:
            headers = Headers(scope=scope)
            if len(headers.getlist(HEADER)) > 1:
                raise ValueError("duplicate agency scope")
            grant = parse_scope(headers.get(HEADER))
            namespace = "/onlyfans/agency/v1/"
            if scope["path"].startswith(namespace):
                if grant is None:
                    raise ValueError("agency identity required")
                path = "/onlyfans/" + scope["path"][len(namespace):]
                scope = {**scope, "path": path, "raw_path": path.encode()}
            if grant is not None:
                path = scope["path"]
                allowed = path == "/onlyfans/api/v1/status" or (path == "/onlyfans/api/v1/earnings" and scope["method"] in {"GET", "HEAD"}) or bool(re.fullmatch(
                    r"/onlyfans/(?:(?:api|control|content|configuration|campaign|offer-import|content-sync)/v1|content/v2)/accounts(?:/[A-Za-z0-9._:-]{1,64}(?:/.*)?)?", path))
                sync = path.startswith("/onlyfans/content-sync/v1/")
                # content-sync/v1 lists its own operations; a draft chunk upload is the only PUT.
                chunk = sync and scope["method"] == "PUT" and bool(re.fullmatch(
                    r"/onlyfans/content-sync/v1/accounts/[A-Za-z0-9._:-]{1,64}/drafts/[0-9a-f-]{36}/files/[0-9a-f-]{36}/chunks/[0-9]{1,6}", path))
                moxy_put = scope["method"] == "PUT" and bool(re.fullmatch(
                    r"/onlyfans/configuration/v1/accounts/[A-Za-z0-9._:-]{1,64}/moxy", path))
                chat_write = grant.can_chat and scope["method"] == "POST" and bool(re.fullmatch(
                    r"/onlyfans/(?:control/v1/accounts/[A-Za-z0-9._:-]{1,64}/conversations/[A-Za-z0-9._:-]{1,64}(?:/requests/[0-9a-f-]{36})?|configuration/v1/accounts/[A-Za-z0-9._:-]{1,64}/history-memory/fans/[1-9][0-9]{0,17}/corrections)", path))
                if (not allowed
                        or scope["method"] not in ({"GET", "HEAD", "POST"} | ({"PUT"} if chunk or moxy_put else set()))
                        or (scope["method"] in {"POST", "PUT"} and not grant.can_manage and not chat_write)):
                    raise ValueError("agency action unavailable")
        except (ValueError, TypeError, RecursionError):
            return await JSONResponse({"ok": False, "code": "agency_scope_denied", "error": "That agency request is not available."},
                status_code=403, headers={"Cache-Control": "private, no-store"})(scope, receive, send)
        token = current_scope.set(grant)
        async def scoped_send(message):
            if message["type"] == "http.response.start" and grant is not None:
                message = {**message, "headers": [*message.get("headers", []), (VERSION_HEADER.lower().encode(), grant.version.encode())]}
            await send(message)
        try:
            await self.app(scope, receive, scoped_send)
        finally:
            current_scope.reset(token)
