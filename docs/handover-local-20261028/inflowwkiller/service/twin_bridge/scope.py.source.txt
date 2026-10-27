"""Trusted bridge scope - what Fastt is allowed to feed Twin, and from when.

Everything here is deliberately explicit and conservative:

* Empty or missing configuration means DISABLED. There is no active-account
  fallback, no wildcard account/fan selection, and the creator is never taken
  from an incoming message or payload - only from trusted configuration.
* The activation cutoff is a REQUIRED, stable configuration value. It is never
  defaulted to "now" on startup: a silent reset would turn a replayed backlog
  into new work every time the process restarted.
* The cutoff and creator slug are copied onto every accepted input row, so a
  later configuration change cannot silently reassign existing work.

Environment (all read from the process environment, never from a message):

    TWIN_BRIDGE_ENABLED          "1"/"true"/"yes" to enable; anything else is off
    TWIN_BRIDGE_ACCOUNT_ID       the ONE Fastt account id this bridge serves
    TWIN_BRIDGE_CREATOR_SLUG     the Twin creator slug that account answers as
    TWIN_BRIDGE_FAN_ALLOWLIST    comma-separated fan ids; empty means nobody
    TWIN_BRIDGE_ACTIVATION_CUTOFF  timezone-qualified ISO-8601 (e.g.
                                  2026-10-15T00:00:00Z); required
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from .contract import parse_canonical_timestamp, validate_id
from .errors import ContractError

log = logging.getLogger("of-relay.twin_bridge.scope")

SCOPE_VERSION = 1

_TRUTHY = {"1", "true", "yes", "on"}

# Fastt stores `fans.fan_id` / `messages.fan_id` as BigInteger, so an allowlist
# entry must be usable as one. A non-numeric entry would otherwise survive
# configuration and blow up later at the reconciliation's `int()` conversion.
_DECIMAL_ID = re.compile(r"^[0-9]+$")
_INT64_MAX = 2**63 - 1


@dataclass(frozen=True)
class BridgeScope:
    """The trusted scope in force for this process."""

    enabled: bool
    account_id: str | None = None
    creator_slug: str | None = None
    fan_allowlist: frozenset[str] = field(default_factory=frozenset)
    activation_cutoff: datetime | None = None  # naive UTC
    version: int = SCOPE_VERSION
    # Why the scope is disabled, for an operator reading the startup log.
    problems: tuple[str, ...] = ()

    def covers(self, account_id: object, fan_id: object) -> bool:
        """True when this account/fan is inside the designated scope.

        Identity is compared as strings; the account must equal the single
        configured account and the fan must be explicitly allowlisted.
        """
        if not self.enabled:
            return False
        if str(account_id) != self.account_id:
            return False
        return str(fan_id) in self.fan_allowlist

    def fan_ids_for_sql(self) -> tuple[int, ...]:
        """Allowlist entries as ints, ready for a `fan_id IN (...)` filter.

        Safe by construction: `load_scope` only ever stores canonical decimal
        strings, so this cannot raise on malformed configuration.
        """
        return tuple(int(fan) for fan in sorted(self.fan_allowlist))


def _split_allowlist(raw: str | None) -> tuple[frozenset[str], list[str]]:
    """Parse and NORMALISE the allowlist. Returns (fan ids, problems).

    Each entry must be a plain decimal id that fits a BigInteger; it is stored in
    its canonical form (`str(int(entry))`) so `covers()` and the SQL filter agree
    even when the operator typed leading zeros.
    """
    out: set[str] = set()
    problems: list[str] = []
    for part in (raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        if not _DECIMAL_ID.match(entry):
            problems.append(f"TWIN_BRIDGE_FAN_ALLOWLIST entry {entry!r} is not a decimal fan id")
            continue
        value = int(entry)
        if value < 1 or value > _INT64_MAX:
            problems.append(f"TWIN_BRIDGE_FAN_ALLOWLIST entry {entry!r} is out of range")
            continue
        out.add(str(value))
    return frozenset(out), problems


def load_scope(env: Mapping[str, str] | None = None) -> BridgeScope:
    """Resolve the trusted scope from the environment. Never raises.

    A partially-configured or malformed scope resolves to disabled, with the
    reasons recorded on the result, so ordinary ingestion is never broken by a
    bridge configuration mistake.
    """
    src = os.environ if env is None else env
    if str(src.get("TWIN_BRIDGE_ENABLED", "")).strip().lower() not in _TRUTHY:
        return BridgeScope(enabled=False)

    problems: list[str] = []

    account_id = str(src.get("TWIN_BRIDGE_ACCOUNT_ID", "")).strip()
    if not account_id:
        problems.append("TWIN_BRIDGE_ACCOUNT_ID is missing")
    else:
        try:
            account_id = validate_id(account_id, "TWIN_BRIDGE_ACCOUNT_ID")
        except ContractError as e:
            problems.append(str(e))

    creator_slug = str(src.get("TWIN_BRIDGE_CREATOR_SLUG", "")).strip()
    if not creator_slug:
        problems.append("TWIN_BRIDGE_CREATOR_SLUG is missing")
    else:
        try:
            creator_slug = validate_id(creator_slug, "TWIN_BRIDGE_CREATOR_SLUG")
        except ContractError as e:
            problems.append(str(e))

    allowlist, allowlist_problems = _split_allowlist(src.get("TWIN_BRIDGE_FAN_ALLOWLIST"))
    problems.extend(allowlist_problems)
    if not allowlist:
        problems.append("TWIN_BRIDGE_FAN_ALLOWLIST is empty, so no fan is designated")

    cutoff_raw = str(src.get("TWIN_BRIDGE_ACTIVATION_CUTOFF", "")).strip()
    cutoff: datetime | None = None
    if not cutoff_raw:
        problems.append("TWIN_BRIDGE_ACTIVATION_CUTOFF is missing; it is never defaulted to now")
    else:
        try:
            cutoff = parse_canonical_timestamp(cutoff_raw, "TWIN_BRIDGE_ACTIVATION_CUTOFF")
        except ContractError as e:
            problems.append(str(e))

    if problems:
        log.warning("twin bridge scope disabled: %s", "; ".join(problems))
        return BridgeScope(enabled=False, problems=tuple(problems))

    return BridgeScope(
        enabled=True,
        account_id=account_id,
        creator_slug=creator_slug,
        fan_allowlist=allowlist,
        activation_cutoff=cutoff,
    )
