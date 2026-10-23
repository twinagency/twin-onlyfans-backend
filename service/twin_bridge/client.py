"""Import-safe async client for Twin's draft + status HTTP routes.

Exactly two routes exist here - `POST /v1/onlyfans/replies` and
`POST /v1/onlyfans/replies/status`. Acknowledgement, cancellation and reset are
deliberately absent: this part has no delivery path, and HTTP success is never
evidence that a message was sent.

Design points the assignment pins down:

* The base URL and bearer token come from trusted configuration / dependency
  injection. They can never come from an event or a message payload.
* Remote hosts, embedded credentials, query/fragment-bearing configuration and
  invalid ports are refused before any request is built.
* Redirects and ambient proxy configuration are off, so the bearer token cannot
  be forwarded to another host.
* One HTTP attempt per explicit call. No hidden retry, no polling loop, no
  sleep, no provider fallback - a later durable worker owns scheduling.
* The body is read as a STREAM with a running byte budget, so an oversized
  response is refused before it is buffered, and the response is always closed.
* Failure CATEGORY and admission UNCERTAINTY are separate. Only a proven pre-send
  failure means the request was not admitted; a deadline, a partial write, a 5xx
  or an unreadable reply all leave uncertainty, and it survives even when the
  response failed validation.
* Diagnostics are fixed categories. Tokens, headers, fan text, raw exception
  strings and response bodies are never logged or persisted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from . import contract
from .errors import BridgeConfigError, ContractError, TwinBridgeError

# Response categories a caller can branch on, without ever seeing internals.
CATEGORY_CONFIG = "config"
CATEGORY_AUTH = "auth"
CATEGORY_FORBIDDEN = "forbidden"
CATEGORY_BAD_REQUEST = "bad-request"
CATEGORY_NOT_FOUND = "not-found"
CATEGORY_OVERLOADED = "overloaded"
CATEGORY_DEADLINE = "deadline-exceeded"
CATEGORY_STORAGE = "storage-unavailable"
CATEGORY_INTERNAL = "internal"
CATEGORY_SERVER = "server-error"
CATEGORY_MALFORMED = "malformed-response"
CATEGORY_CONNECTION = "connection-failed"
CATEGORY_UNCERTAIN = "uncertain"

# Categories that mean "Twin may already have received and admitted this".
# A category is NOT evidence either way; the flag below is.
UNCERTAIN_CATEGORIES = frozenset({
    CATEGORY_UNCERTAIN,   # timed out / protocol cut after the request was sent
    CATEGORY_DEADLINE,    # Twin's own response deadline: it kept the work
    CATEGORY_STORAGE,     # Twin received it and hit a storage failure
    CATEGORY_INTERNAL,    # Twin received it and failed internally
    CATEGORY_MALFORMED,   # unusable reply: admission cannot be ruled out
})

ROUTE_REPLIES = "/v1/onlyfans/replies"
ROUTE_STATUS = "/v1/onlyfans/replies/status"
ROUTE_ACK = "/v1/onlyfans/replies/ack"
ROUTE_CANCEL = "/v1/onlyfans/replies/cancel"

MIN_TOKEN_LENGTH = 32
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

# Twin lifecycle states and outcome values, copied from the accepted service and
# store. Kept as sets so an unknown value is rejected rather than guessed at.
STATUSES = frozenset({"queued", "in_progress", "completed", "delivered", "cancelled", "interrupted", "failed"})
OUTCOMES = frozenset({"ok", "blocked", "failed", "stale", "unsupported"})
ACTIVE_STATUSES = frozenset({"queued", "in_progress"})
FAILURE_STATUSES = frozenset({"cancelled", "interrupted", "failed"})
DELIVERABLE_OUTCOMES = frozenset({"ok", "blocked"})

# The ack/cancel decision bodies are SMALLER than a draft/status body: they carry
# only the request identity and the route's own status vocabulary, so they get
# their own validator rather than being parsed as a draft reply.
ACK_STATUSES = frozenset({"delivered", "already-delivered"})
CANCEL_STATUSES = frozenset({"cancelled", "already-cancelled"})

# Documented public error codes -> (category, the ONE status Twin pairs with it).
# See `docs/onlyfans/HTTP_API.md`. A code that arrives with a different status,
# or a status whose body is not a valid bridge envelope, is NOT a known outcome.
ERROR_CODES: dict[str, tuple[str, int]] = {
    "unauthorized": (CATEGORY_AUTH, 401),
    "forbidden": (CATEGORY_FORBIDDEN, 403),
    "bad-request": (CATEGORY_BAD_REQUEST, 400),
    "invalid-json": (CATEGORY_BAD_REQUEST, 400),
    "invalid-utf8": (CATEGORY_BAD_REQUEST, 400),
    "method-not-allowed": (CATEGORY_BAD_REQUEST, 405),
    "request-timeout": (CATEGORY_BAD_REQUEST, 408),
    "ack-rejected": (CATEGORY_BAD_REQUEST, 409),
    "cancel-rejected": (CATEGORY_BAD_REQUEST, 409),
    "oversized-body": (CATEGORY_BAD_REQUEST, 413),
    "unsupported-media-type": (CATEGORY_BAD_REQUEST, 415),
    "unsupported-encoding": (CATEGORY_BAD_REQUEST, 415),
    "not-found": (CATEGORY_NOT_FOUND, 404),
    "overloaded": (CATEGORY_OVERLOADED, 429),
    "storage-unavailable": (CATEGORY_STORAGE, 500),
    "internal": (CATEGORY_INTERNAL, 500),
    "deadline-exceeded": (CATEGORY_DEADLINE, 503),
}


class TwinClientError(TwinBridgeError):
    """A fixed, sanitised failure category. Never carries request internals.

    `uncertain` is independent of the category: it is True whenever Twin cannot
    be shown to have NOT received and admitted the request. A caller must not
    re-send a platform message on the strength of either field.
    """

    def __init__(self, category: str, detail: str = "", *, uncertain: bool | None = None) -> None:
        super().__init__(f"{category}: {detail}" if detail else category)
        self.category = category
        self.detail = detail
        self.uncertain = category in UNCERTAIN_CATEGORIES if uncertain is None else bool(uncertain)


@dataclass(frozen=True)
class TwinReplyResult:
    """A validated Twin result for one request.

    A returned result is never uncertain; uncertainty is carried by
    `TwinClientError.uncertain`. `deliverable` is only ever true for a completed,
    un-delivered, un-committed draft with non-empty text - exactly Twin's own
    `live` predicate.
    """

    request_id: str
    conversation_key: str
    status: str
    outcome: str | None
    deliverable: bool
    text: str
    epoch: int
    created_at: str
    completed_at: str | None
    delivered_at: str | None
    committed_at: str | None
    usage: dict = field(default_factory=dict)

    @property
    def uncertain(self) -> bool:
        return False


@dataclass(frozen=True)
class TwinDecisionResult:
    """A validated acknowledgement / cancellation result.

    Deliberately NOT a draft reply: these routes answer with the request
    identity and one status from a two-value vocabulary, so `ok: true` alone is
    never accepted and a full projection is not required.
    """

    request_id: str
    conversation_key: str
    status: str

    @property
    def uncertain(self) -> bool:
        return False


def validate_base_url(raw: str) -> str:
    """Return a canonical `http://127.0.0.1:<port>` base URL, or raise.

    `localhost` is deliberately NOT accepted: it can resolve through a hosts
    file or a proxy, and the adapter is defined to live on the literal loopback
    address. Credentials, query strings, fragments and paths are refused so the
    configured token can never be aimed somewhere else.
    """
    text = (raw or "").strip()
    if not text:
        raise BridgeConfigError("bridge base URL is empty")
    parsed = urlparse(text)
    if parsed.scheme != "http":
        raise BridgeConfigError("bridge base URL must use http")
    if parsed.username or parsed.password:
        raise BridgeConfigError("bridge base URL must not embed credentials")
    if parsed.query or parsed.fragment:
        raise BridgeConfigError("bridge base URL must not contain a query or fragment")
    if parsed.path not in ("", "/"):
        raise BridgeConfigError("bridge base URL must not contain a path")
    host = (parsed.hostname or "").strip()
    if host not in _LOOPBACK_HOSTS:
        raise BridgeConfigError("bridge base URL must point at loopback (127.0.0.1 or ::1)")
    try:
        port = parsed.port
    except ValueError as exc:  # malformed port
        raise BridgeConfigError("bridge base URL has an invalid port") from exc
    if port is None or not (1 <= port <= 65535):
        raise BridgeConfigError("bridge base URL must include a port between 1 and 65535")
    host_part = f"[{host}]" if ":" in host else host
    return f"http://{host_part}:{port}"


def validate_token(raw: str) -> str:
    """Twin's own rule: at least 32 characters and no whitespace."""
    token = raw or ""
    if len(token) < MIN_TOKEN_LENGTH:
        raise BridgeConfigError(f"bridge token must be at least {MIN_TOKEN_LENGTH} characters")
    if any(ch.isspace() for ch in token):
        raise BridgeConfigError("bridge token must not contain whitespace")
    return token


class TwinBridgeClient:
    """One-shot client for the draft and status routes. Import has no side effects."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        write_timeout: float = 10.0,
        pool_timeout: float = 5.0,
        max_response_bytes: int = 64 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = validate_base_url(base_url)
        self._token = validate_token(token)
        self._max_response_bytes = int(max_response_bytes)
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(
                    connect=connect_timeout,
                    read=read_timeout,
                    write=write_timeout,
                    pool=pool_timeout,
                ),
                # A redirect could re-send the bearer token to another host, and
                # trust_env would pick up a proxy from the ambient environment.
                follow_redirects=False,
                trust_env=False,
                transport=transport,
                headers={"authorization": f"Bearer {self._token}", "accept": "application/json"},
            )
            self._owns_client = True

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "TwinBridgeClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ── routes ────────────────────────────────────────────────────────

    async def request_draft(self, event: dict) -> TwinReplyResult:
        """POST one complete version-1 event to the draft route."""
        return await self._post(ROUTE_REPLIES, event)

    async def read_status(self, event: dict) -> TwinReplyResult:
        """POST one complete version-1 event to the read-only status route."""
        return await self._post(ROUTE_STATUS, event)

    async def acknowledge_delivery(self, event: dict) -> TwinDecisionResult:
        """POST the frozen event to Twin's acknowledgement route.

        Idempotent on Twin's side (`delivered` / `already-delivered`), which is
        what makes retrying the ACK - and only the ACK - safe.
        """
        return await self._decision(
            ROUTE_ACK, event, ACK_STATUSES, "acknowledgement",
        )

    async def cancel_request(self, event: dict) -> TwinDecisionResult:
        """POST the frozen event to Twin's cancellation route."""
        return await self._decision(
            ROUTE_CANCEL, event, CANCEL_STATUSES, "cancellation",
        )

    # ── internals ─────────────────────────────────────────────────────

    async def _post(self, route: str, event: dict) -> TwinReplyResult:
        envelope = await self._post_envelope(route, event)
        return _as_reply(envelope["result"])

    async def _decision(self, route: str, event: dict, allowed: frozenset[str],
                        what: str) -> TwinDecisionResult:
        """Validate one small decision body against ITS OWN schema."""
        envelope = await self._post_envelope(route, event)
        result = envelope["result"]
        if not isinstance(result, dict):
            raise TwinClientError(CATEGORY_MALFORMED, f"{what} result is not an object")
        status = _typed_str(_required(result, "status"), f"{what} status")
        if status not in allowed:
            raise TwinClientError(
                CATEGORY_MALFORMED, f"{what} has an unknown status"
            )
        self._check_identity(result, event)
        return TwinDecisionResult(
            request_id=_typed_str(_required(result, "requestId"), "requestId"),
            conversation_key=_typed_str(_required(result, "conversationKey"), "conversationKey"),
            status=status,
        )

    async def _post_envelope(self, route: str, event: dict) -> dict:
        try:
            payload = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise TwinClientError(CATEGORY_CONFIG, "event is not serializable") from exc
        if len(payload) > contract.MAX_TOTAL_BYTES:
            raise TwinClientError(CATEGORY_CONFIG, "event exceeds the wire size limit")

        try:
            # A STREAMING request: the body is not buffered before the size guard
            # runs, and the response is closed on every exit path (including the
            # raises below) by the context manager.
            async with self._client.stream(
                "POST", route, content=payload,
                headers={"content-type": "application/json; charset=utf-8"},
            ) as response:
                if 300 <= response.status_code < 400:
                    # Redirects are off, so this is a refusal to follow one, not a
                    # result. Following it could re-send the bearer token elsewhere.
                    raise TwinClientError(CATEGORY_MALFORMED, "bridge returned a redirect")
                body = await self._read_bounded(response)
                content_type = response.headers.get("content-type", "")
                if response.status_code != 200:
                    self._raise_for_error(body, content_type, response.status_code)
                envelope = _parse_success_envelope(body, content_type)
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as exc:
            # The ONLY proof that Twin did not admit the request: a connection or
            # pool refusal that happened before any byte could be submitted.
            raise TwinClientError(CATEGORY_CONNECTION, "could not reach the bridge", uncertain=False) from exc
        except (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.WriteError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.LocalProtocolError,
            httpx.CloseError,
        ) as exc:
            # Past the point where Twin may have received the request: a failed or
            # partial write does NOT establish non-admission, and neither does a
            # failure to close a response we already read - the work may be done.
            raise TwinClientError(CATEGORY_UNCERTAIN, "no usable response after the request was sent", uncertain=True) from exc
        except httpx.HTTPError as exc:
            # Any OTHER transport failure inside the request/response lifecycle is
            # not evidence of non-admission either, so it must not be reported as
            # a proven refusal.
            raise TwinClientError(
                CATEGORY_UNCERTAIN, "transport failure after the request was sent", uncertain=True
            ) from exc

        self._check_identity(envelope["result"], event)
        return envelope

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        """Read the body with a running byte budget; always leaves it closed.

        `aiter_raw` yields the network chunks, so the guard stops at the chunk
        that crosses the limit instead of after the whole body is buffered. Non
        identity content encodings are refused by Twin anyway, so raw == decoded.
        """
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_response_bytes:
                    raise TwinClientError(CATEGORY_MALFORMED, "response is larger than the client accepts")
            except ValueError as exc:
                raise TwinClientError(CATEGORY_MALFORMED, "response declares an invalid length") from exc
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_raw():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise TwinClientError(CATEGORY_MALFORMED, "response is larger than the client accepts")
            chunks.append(chunk)
        return b"".join(chunks)

    def _raise_for_error(self, body: bytes, content_type: str, status: int) -> None:
        """Map a non-200 to a documented outcome, or refuse to guess."""
        envelope = _decode_json_object(body, content_type)
        try:
            contract.check_schema_version(envelope.get("schemaVersion"))
        except ContractError as exc:
            raise TwinClientError(
                CATEGORY_MALFORMED, "error response has an unsupported schema version"
            ) from exc
        if envelope.get("ok") is not False:
            raise TwinClientError(CATEGORY_MALFORMED, "error response is not marked as an error")
        error = envelope.get("error")
        if not isinstance(error, dict):
            raise TwinClientError(CATEGORY_MALFORMED, "error response has no error object")
        code = error.get("code")
        if not isinstance(code, str):
            raise TwinClientError(CATEGORY_MALFORMED, "error response has no error code")
        entry = ERROR_CODES.get(code)
        if entry is None:
            # An unknown code is not a documented bridge outcome.
            raise TwinClientError(CATEGORY_MALFORMED, "error response has an unknown error code")
        category, required_status = entry
        if status != required_status:
            # The status/code pair contradicts the documented mapping.
            raise TwinClientError(CATEGORY_MALFORMED, "error response status does not match its code")
        raise TwinClientError(category, "bridge rejected the request")

    def _check_identity(self, result: dict, event: dict) -> None:
        """The result must belong to the identity we asked about."""
        try:
            expected_conv = contract.conversation_key(
                event["channel"], event["accountId"], event["creatorSlug"],
                event["conversationId"], event["fanId"],
            )
            expected_req = contract.message_key(
                event["channel"], event["accountId"], event["fanId"], event["messageId"],
            )
        except (KeyError, TypeError) as exc:
            raise TwinClientError(CATEGORY_CONFIG, "event is missing identity fields") from exc
        if result.get("conversationKey") != expected_conv:
            raise TwinClientError(CATEGORY_MALFORMED, "response conversation does not match the request")
        if result.get("requestId") != expected_req:
            raise TwinClientError(CATEGORY_MALFORMED, "response request does not match the request")


def _decode_json_object(body: bytes, content_type: str) -> dict:
    """Decode a JSON object body, or raise a fixed malformed-response failure."""
    if "json" not in (content_type or "").lower():
        raise TwinClientError(CATEGORY_MALFORMED, "response is not JSON")
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TwinClientError(CATEGORY_MALFORMED, "response is not valid UTF-8") from exc
    try:
        envelope = json.loads(decoded)
    except ValueError as exc:
        raise TwinClientError(CATEGORY_MALFORMED, "response is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise TwinClientError(CATEGORY_MALFORMED, "response envelope is not an object")
    return envelope


def _parse_success_envelope(body: bytes, content_type: str) -> dict:
    envelope = _decode_json_object(body, content_type)
    try:
        contract.check_schema_version(envelope.get("schemaVersion"))
    except ContractError as exc:
        raise TwinClientError(CATEGORY_MALFORMED, "unexpected wire schema version") from exc
    if envelope.get("ok") is not True:
        # Twin reported a refusal inside a 2xx envelope; that is not a draft and
        # it is certainly not delivery.
        raise TwinClientError(CATEGORY_MALFORMED, "response was not ok")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise TwinClientError(CATEGORY_MALFORMED, "response has no result object")
    return envelope


def _required(result: dict, key: str):
    """The value of a REQUIRED projection key, or a fixed malformed-response failure.

    `result.get(key)` cannot be used for Twin's required fields: a key the caller
    never sent would then be indistinguishable from one Twin explicitly projected
    as null. Dropping `deliveredAt` or `committedAt` would silently look like
    "no delivery evidence", turning an unverifiable reply into a deliverable
    draft. Presence and nullability are therefore checked separately.
    """
    if key not in result:
        raise TwinClientError(CATEGORY_MALFORMED, f"response has no {key} field")
    return result[key]


def _typed_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TwinClientError(CATEGORY_MALFORMED, f"response {field_name} is not a string")
    return value


def _stamp_or_none(result: dict, key: str) -> str | None:
    value = _required(result, key)
    if value is None:
        return None
    if not contract.is_canonical_timestamp(value):
        raise TwinClientError(CATEGORY_MALFORMED, f"response {key} is not a valid timestamp")
    return value


def _usage(raw: object) -> dict:
    """Validate the usage projection: every key present, then correctly typed."""
    if not isinstance(raw, dict):
        raise TwinClientError(CATEGORY_MALFORMED, "response usage is not an object")
    for key in ("provider", "tokensIn", "tokensOut", "cost", "ms", "attempts"):
        if key not in raw:
            _reject(f"response usage has no {key} field")
    try:
        return {
            # A provider LABEL, not an identifier: Twin keeps it verbatim.
            "provider": contract.validate_provider_label(raw.get("provider"), "usage provider"),
            "tokensIn": contract.strict_int(raw.get("tokensIn"), "usage tokensIn", minimum=0),
            "tokensOut": contract.strict_int(raw.get("tokensOut"), "usage tokensOut", minimum=0),
            "cost": contract.strict_number(raw.get("cost"), "usage cost", minimum=0),
            "ms": contract.strict_number(raw.get("ms"), "usage ms", minimum=0),
            "attempts": contract.strict_int(raw.get("attempts"), "usage attempts", minimum=0),
        }
    except ContractError as exc:
        raise TwinClientError(CATEGORY_MALFORMED, "response usage is malformed") from exc


def _reject(why: str) -> None:
    raise TwinClientError(CATEGORY_MALFORMED, why)


def _as_reply(result: dict) -> TwinReplyResult:
    """Validate the full version-1 projection against Twin's own invariants.

    Every key Twin's `projectReply` always emits must be PRESENT, and its type is
    checked BEFORE membership or hashing, so a list/object/null in a field
    produces a fixed sanitised failure instead of a raw Python exception. An
    explicit null stays legal wherever the lifecycle permits it; an absent key
    never does.
    """
    status = _typed_str(_required(result, "status"), "status")
    if status not in STATUSES:
        _reject("response has an unknown lifecycle status")

    outcome_raw = _required(result, "outcome")
    outcome = None if outcome_raw is None else _typed_str(outcome_raw, "outcome")
    if outcome is not None and outcome not in OUTCOMES:
        _reject("response has an unknown outcome")

    deliverable = _required(result, "deliverable")
    if not isinstance(deliverable, bool):
        _reject("response is missing a deliverable flag")

    text = _typed_str(_required(result, "text"), "text")

    try:
        epoch = contract.strict_int(_required(result, "epoch"), "epoch", minimum=0)
    except ContractError as exc:
        raise TwinClientError(CATEGORY_MALFORMED, "response epoch is not a valid integer") from exc

    created_at = _stamp_or_none(result, "createdAt")
    if created_at is None:
        _reject("response has no creation timestamp")
    completed_at = _stamp_or_none(result, "completedAt")
    delivered_at = _stamp_or_none(result, "deliveredAt")
    committed_at = _stamp_or_none(result, "committedAt")
    usage = _usage(_required(result, "usage"))

    # Deliverable is exactly Twin's own `live` predicate: completed, no delivery
    # evidence, and non-empty text. A non-null delivery/commit stamp or a
    # non-deliverable outcome can therefore never be handed out as a draft.
    if deliverable:
        if status != "completed":
            _reject("only a completed draft may be deliverable")
        if not text:
            _reject("a deliverable draft must carry text")
        if outcome not in DELIVERABLE_OUTCOMES:
            _reject("a deliverable draft must have a deliverable outcome")
        if delivered_at is not None or committed_at is not None:
            _reject("a deliverable draft must not carry delivery evidence")
    elif text:
        _reject("an undeliverable result must not carry draft text")

    if delivered_at is not None and status != "delivered":
        _reject("only a delivered request may carry delivery evidence")
    if committed_at is not None and delivered_at is None:
        _reject("a commit cannot precede delivery")

    if status == "delivered":
        if delivered_at is None:
            _reject("a delivered request must carry delivery evidence")
        if outcome not in DELIVERABLE_OUTCOMES:
            _reject("a delivered request must have a deliverable outcome")
    if status in FAILURE_STATUSES:
        if delivered_at is not None or committed_at is not None:
            _reject("a failed request must not carry delivery evidence")
        if status == "failed" and outcome is None:
            _reject("a failed request must record an outcome")

    if status in ACTIVE_STATUSES:
        if completed_at is not None:
            _reject("an unfinished request must not have a completion timestamp")
        if outcome is not None:
            _reject("an unfinished request must not have an outcome")
    else:
        if completed_at is None:
            _reject("a finished request must have a completion timestamp")
    if status in ("completed", "delivered") and outcome not in DELIVERABLE_OUTCOMES:
        _reject("a completed request must have a deliverable outcome")

    return TwinReplyResult(
        request_id=_typed_str(_required(result, "requestId"), "requestId"),
        conversation_key=_typed_str(_required(result, "conversationKey"), "conversationKey"),
        status=status,
        outcome=outcome,
        deliverable=deliverable,
        text=text,
        epoch=epoch,
        created_at=created_at,
        completed_at=completed_at,
        delivered_at=delivered_at,
        committed_at=committed_at,
        usage=usage,
    )
