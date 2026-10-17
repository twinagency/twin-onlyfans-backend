"""Service-to-service auth for the OnlyFans read API.

THE ONE CREDENTIAL IS A BEARER TOKEN, AND IT IS NOT A COOKIE. The dashboard
calls this API from its own server, with `Authorization: Bearer
<ONLYFANS_API_TOKEN>` and no cookies. So this module reads exactly one header
and never consults a cookie, a query parameter or a provider session — a
request that carries a signed-in operator's cookies but no service token is
refused, which is what stops an operator's browser session from becoming a
service credential. The offline suite asserts that this module cannot read
cookies at all (by inspecting its own source), because "we remembered not to"
is not a property a test can hold onto.

FAIL CLOSED, NEVER FIXTURES. With no token configured, every route answers 503
`unavailable`. That is a deliberate choice over any richer behaviour: a
deployment that has not been given a credential must not serve private
conversation data to anyone, and must not look like a deployment that has no
data instead. The dashboard renders `unavailable` as "there is nothing to read
from", which is exactly the truth.

TENANT SCOPE, AND WHY BLANK IS NOT A SCOPE. `ONLYFANS_API_TENANT` names the
tenant this credential acts as, and it has exactly three treatments:

  • a `users.id` — the credential sees only the creators and accounts bound to
    that agency;
  • the literal `deployment` — the deployment operator's own credential, which
    sees every binding in this installation. Widening to "everything" is
    therefore something an operator WRITES DOWN, not something that happens by
    leaving a variable empty;
  • unset or blank — **refused**, 503 `unavailable`. A token without a tenant is
    a deployment that has not decided what its credential is for, and the one
    reading of that state which must never be possible is "so it may read
    everything". The credential is unusable until the decision is made, which is
    recoverable by configuration and not by accident.

Either way the *binding authority* still decides, per account, whether the
answer exists (service.py). The tenant is a narrowing, never a grant: a token
scoped to one agency cannot be widened by anything the caller sends.

The token is compared with `hmac.compare_digest`, and a wrong token and a
missing one produce the same 401 body — a service that says "that token is not
one of ours" is a service that answers questions about its own credentials.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from .contract import SCHEMA_VERSION_HEADER, SUPPORTED_SCHEMA_VERSIONS
from .errors import BadRequest, Unauthorized, Unavailable

# Both names are the dashboard's own. `ONLYFANS_API_BASE_URL` is read by the
# dashboard, not here, and is named in this docstring only so the pair is
# greppable from either side.
TOKEN_ENV = "ONLYFANS_API_TOKEN"
TENANT_ENV = "ONLYFANS_API_TENANT"

# The explicit spelling of "no tenant narrowing".
TENANT_DEPLOYMENT = "deployment"


@dataclass(frozen=True)
class ServicePrincipal:
    """Who this request is, as far as this service is concerned.

    `tenant_id is None` means the deployment scope. It is not "unknown" — the
    two are different decisions and only `None` is ever reachable, because an
    absent token is refused before this object is built.
    """

    tenant_id: str | None
    label: str

    @property
    def is_deployment_scope(self) -> bool:
        return self.tenant_id is None


def declared_token() -> str:
    """The raw credential value, stripped. `""` when the deployment has none."""
    return (os.environ.get(TOKEN_ENV) or "").strip()


def declared_tenant() -> str:
    """The raw tenant value, stripped. `""` when the deployment did not decide."""
    return (os.environ.get(TENANT_ENV) or "").strip()


def configured_token() -> str | None:
    """The token this service will accept, or None when it has none."""
    return declared_token() or None


def resolve_tenant(raw: str | None) -> str | None:
    """THE tenant rule. Pure, so a test can drive every branch without env vars.

      • blank or unset — `Unavailable`. Not "deployment", and not "no
        narrowing": undecided. See the module docstring for why that one
        reading is the dangerous one.
      • the literal `deployment` — `None`, the deployment-wide scope.
      • anything else — itself: a `users.id`, i.e. an agency narrowing.

    `""` is the absence of a decision, never a tenant id, so it is refused
    rather than passed down as a scope nothing could be bound to (which would
    look exactly like a legitimate empty installation).
    """
    value = (raw or "").strip()
    if not value:
        raise Unavailable(
            "tenant_not_configured",
            "This relay has an OnlyFans service token but no ONLYFANS_API_TENANT, so it "
            "does not know which tenant that credential may read for. Nothing is served "
            "until that is decided.",
        )
    if value == TENANT_DEPLOYMENT:
        return None
    return value


def configured_tenant() -> str | None:
    """The narrowing the configured credential applies, or None for deployment.

    Raises exactly what a request would raise for a blank tenant, so this cannot
    answer a question the gate refuses.
    """
    return resolve_tenant(declared_tenant())


def tenant_configured() -> bool:
    """Whether the configured credential carries an explicit tenant decision."""
    return bool(declared_tenant())


def service_configured() -> bool:
    return configured_token() is not None


def verify_bearer(authorization: str | None) -> ServicePrincipal:
    """Verify `Authorization: Bearer <token>` or refuse.

    Raises `Unavailable` when this deployment has no token (fail closed),
    `Unauthorized` for anything else that is not the token.
    """
    expected = configured_token()
    if expected is None:
        raise Unavailable(
            "service_token_not_configured",
            "This relay has no OnlyFans service token configured, so no OnlyFans data "
            "can be read from it. Nothing is served in its place.",
        )

    header = (authorization or "").strip()
    if not header:
        raise Unauthorized()
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        # `Basic`, a bare token, a cookie-shaped value — all the same refusal.
        raise Unauthorized()
    presented = parts[1].strip()
    if not presented or not hmac.compare_digest(presented, expected):
        raise Unauthorized()

    # The tenant is resolved AFTER the token is proven, and that order is
    # deliberate: a wrong token stays a 401, so a misconfigured deployment does
    # not describe its own configuration to whoever happened to ask. A caller
    # that holds the token gets the honest 503.
    tenant = resolve_tenant(declared_tenant())
    return ServicePrincipal(
        tenant_id=tenant,
        label="deployment" if tenant is None else f"tenant:{tenant}",
    )


def negotiate_schema_version(header_value: str | None) -> str:
    """Require the client's schema version, and require that we speak it.

    A missing header is a `BadRequest` rather than a default, for the same
    reason the dashboard refuses an answer with no `schemaVersion`: a default
    silently defines what the caller meant, and the whole point of the version
    is that neither side guesses.
    """
    presented = (header_value or "").strip()
    if not presented:
        raise BadRequest(
            "missing_schema_version",
            f"{SCHEMA_VERSION_HEADER} is required on every OnlyFans read.",
        )
    if presented not in SUPPORTED_SCHEMA_VERSIONS:
        raise BadRequest(
            "unsupported_schema_version",
            "This relay does not implement that OnlyFans schema version.",
        )
    return presented
