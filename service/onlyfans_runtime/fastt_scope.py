"""Platform mode ``db:*`` discovers every live registered OnlyFans binding.

New accounts need no deployment edit. With no explicit fan selection, the runner
serves eligible fans of that account; account activation, verified credentials,
delivery limits and each conversation's takeover/pause/no-contact still apply.
The legacy worker is fenced even if discovery is temporarily unavailable.

Which fans the FASTT runner owns: a per-account operator setting under a deployment allowlist.

Two layers, and the env layer is always the upper bound:

* ``ONLYFANS_FASTT_SCOPE`` (deployment, unchanged syntax) is the ALLOWLIST. ``<account>:<fan>`` allows one
  fan, ``<account>:*`` allows any fan of the account, and the new literal ``db:<account>`` says "this account
  may be scoped from the database" (it allows any fan but owns none until the operator selects some).
  With no database value for an account the env alone decides, exactly as before (``<account>:*`` owns the
  whole account, a pair owns that fan), so a deploy that only has the old variable behaves identically.
* ``account_ai_config.ai_chatter_config_json["fastt_scope"] = {"mode": "single"|"selected"|"all",
  "fans": [int, ...]}`` is the operator's selection (``single`` = exactly one fan, ``all`` ignores ``fans``).
  No new column or migration. With a value, the SERVED set is ``selection ∩ allowlist``.

Two predicates come out of that, and they differ on purpose:

``owned`` (``fastt_owned`` / ``fastt_account_owned`` / ``_scope``): "the legacy worker must not send for this
fan". It only ever GROWS inside a process: the pure-env owners, plus every fan the database has selected
since this process started. A fan that leaves the selection stays owned, so removing a fan can never hand it
back to the legacy worker, which may still be mid-send under a stale view; only a restart of that process
forgets it, and an operator who wants a fan returned to the legacy worker restarts the worker after the
runner has stopped serving it. A ``db:`` account whose selection has never been read successfully is owned
whole (fail closed).

``serves`` (``fastt_serves`` / ``serve_scope``): "the runner may act for this fan right now", the current
selection ∩ allowlist (or the pure env value when the account has no database value). The runner and the
supervisor use it, so removing a fan stops FASTT serving it without handing it to anyone else (its queued
human messages simply wait).

``_scope()`` stays cheap and synchronous: the env is parsed per call as before, and the database is read at
most once per ``TTL_S`` by a short sync query (3 s statement timeout, one pooled connection, only the
``account_ai_config`` rows of the allowlisted accounts). A failed read keeps the last known value and retries
after the next interval; it never shrinks ownership.

Two processes (worker and runner) each refresh on their own clock, so a fan newly added to the selection
could be seen by the runner a moment before the worker. To close that window a fan that APPEARS after a
process's first successful read is served only after ``SETTLE_S`` (> two refresh intervals), while it is
owned (legacy-skipped) immediately. The supervisor applies the same wait to everything it starts from the
database, including at its own start.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from .runtime_stop import to_fastt_account_id

SCOPE_ENV = "ONLYFANS_FASTT_SCOPE"
KEY = "fastt_scope"
PLATFORM_SCOPE = "db:*"
MODES = ("single", "selected", "all")
MAX_FANS = 5000
TTL_S = 15.0
SETTLE_S = 2 * TTL_S + 5.0

log = logging.getLogger("fastt_scope")
_clock = time.monotonic       # tests replace these two
_reader = None                # callable(db account ids) -> {db account id: raw config json | None}
_discoverer = None            # tests may inject registered OnlyFans account ids


# ── strict value validation (also used by the settings route) ────────────────

def normalize(value) -> dict:
    """The canonical ``{"mode", "fans"}`` for a stored/submitted value, or ValueError with the reason."""
    if not isinstance(value, dict):
        raise ValueError("fastt_scope must be an object")
    extra = set(value) - {"mode", "fans"}
    if extra:
        raise ValueError("fastt_scope has unknown keys: " + ", ".join(sorted(map(str, extra))))
    mode = value.get("mode")
    if mode not in MODES:
        raise ValueError("fastt_scope.mode must be one of single, selected, all")
    fans = value.get("fans", [])
    if mode == "all":
        return {"mode": "all", "fans": []}
    if not isinstance(fans, list):
        raise ValueError("fastt_scope.fans must be a list")
    if len(fans) > MAX_FANS:
        raise ValueError(f"fastt_scope.fans has more than {MAX_FANS} entries")
    for fan in fans:
        if type(fan) is not int or fan <= 0:
            raise ValueError("fastt_scope.fans must be positive integers")
    if mode == "single" and len(fans) != 1:
        raise ValueError("fastt_scope mode single needs exactly one fan")
    if mode == "selected" and not fans:
        raise ValueError("fastt_scope mode selected needs at least one fan (use null to clear)")
    return {"mode": mode, "fans": sorted(set(fans))}


# ── the env allowlist ────────────────────────────────────────────────────────

def _account(raw):
    try:
        return to_fastt_account_id(raw)
    except Exception:
        return None


def parse_allowlist(raw):
    """(allowed pairs, ``<account>:*`` accounts, ``db:<account>`` accounts), FASTT-form account ids."""
    pairs, anyfan, dbonly = set(), set(), set()
    for item in (raw or "").split(","):
        item = item.strip()
        if item == PLATFORM_SCOPE:
            anyfan.update(_platform_accounts())
            continue
        if item.startswith("db:"):
            account = _account(item[3:].strip())
            if account:
                dbonly.add(account)
            continue
        account, _, fan = item.partition(":")
        account = account.strip()
        if not account:
            continue
        fastt = _account(account)
        if fastt is None:
            continue
        if fan == "*":
            anyfan.add(fastt)
        elif fan.isdecimal():
            pairs.add((fastt, int(fan)))
    return pairs, anyfan, dbonly


# ── the process-local database cache ─────────────────────────────────────────

class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {}          # fastt account -> (mode, frozenset(fans)) | None (no value), last known
        self.known = set()      # accounts read successfully at least once
        self.next_try = 0.0
        self.has_read = False
        self.ever_fans = {}     # account -> set(fan): owned for the life of this process
        self.ever_all = set()
        self.seen = {}          # (account, fan | "*") -> (first seen, in the first read)
        self.engine = None
        self.engine_url = None
        self.discovery_lock = threading.Lock()
        self.discovered = set()
        self.discovery_next = 0.0


_state = _State()


def reset_cache():
    """Forget everything learned (tests, and a clean slate in a fresh process)."""
    global _state
    old = _state
    _state = _State()
    if old.engine is not None:
        try:
            old.engine.dispose()
        except Exception:
            pass


def _db_id(account):
    return "of-" + account if account.isdecimal() else account


def platform_scope_enabled(raw=None):
    """Platform deployment: discover registered creators, retain per-creator controls."""
    if raw is None:
        raw = os.environ.get(SCOPE_ENV, "")
    return PLATFORM_SCOPE in {v.strip() for v in raw.split(",")}


def _scope_engine():
    from sqlalchemy import create_engine
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return None
    st = _state
    if st.engine is None or st.engine_url != url:
        st.engine = create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1), pool_size=1,
                                  max_overflow=0, pool_timeout=3, pool_pre_ping=True,
                                  connect_args={"options": "-c timezone=UTC -c statement_timeout=3000 -c lock_timeout=1000",
                                                "connect_timeout": 3})
        st.engine_url = url
    return st.engine


def _default_discover():
    """Only existing, live OnlyFans bindings may acquire a platform runner."""
    from sqlalchemy import text
    engine = _scope_engine()
    if engine is None:
        return set()
    with engine.connect() as con:
        return {row[0] for row in con.execute(text(
            "SELECT r.account_id FROM of_runtime r JOIN of_creator_bindings b ON b.id=r.binding_id "
            "AND b.account_id=r.account_id WHERE b.revoked_at IS NULL AND b.channel='onlyfans' "
            "AND b.tenant_id<>'deployment'"))}


def _platform_accounts():
    st, now = _state, _clock()
    if now < st.discovery_next or not st.discovery_lock.acquire(blocking=False):
        return set(st.discovered)
    try:
        if now < st.discovery_next:
            return set(st.discovered)
        st.discovery_next = now + TTL_S
        try:
            found = (_discoverer or _default_discover)()
            st.discovered = {account for raw in found if (account := _account(raw)) is not None}
        except Exception as exc:
            log.warning("platform scope discovery failed (%s); keeping registered accounts", type(exc).__name__)
        return set(st.discovered)
    finally:
        st.discovery_lock.release()


def _default_read(db_ids):
    from sqlalchemy import bindparam, text
    engine = _scope_engine()
    if engine is None:
        return {i: None for i in db_ids}
    query = text("SELECT account_id, ai_chatter_config_json FROM account_ai_config WHERE account_id IN :ids"
                 ).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as con:
        found = {row[0]: row[1] for row in con.execute(query, {"ids": list(db_ids)})}
    return {i: found.get(i) for i in db_ids}


def _value(raw):
    """(mode, fans) from a stored config blob; None when it names no fastt_scope; ValueError if malformed."""
    import json
    if raw in (None, ""):
        return None
    cfg = json.loads(raw)
    if not isinstance(cfg, dict) or cfg.get(KEY) is None:
        return None
    clean = normalize(cfg[KEY])
    return clean["mode"], frozenset(clean["fans"])


def _allowed(account, fan, pairs, anyfan, dbonly):
    return account in anyfan or account in dbonly or (account, fan) in pairs


def _db_eff(account, val, pairs, anyfan, dbonly):
    """(all?, fans) the database value grants once cut by the allowlist."""
    if val is None:
        return False, set()
    mode, fans = val
    if mode == "all":
        if account in anyfan or account in dbonly:
            return True, set()
        return False, {f for a, f in pairs if a == account}
    return False, {f for f in fans if _allowed(account, f, pairs, anyfan, dbonly)}


def _refresh(candidates, now, allow):
    st = _state
    if now < st.next_try or not st.lock.acquire(blocking=False):
        return
    try:
        if now < st.next_try:
            return
        st.next_try = now + TTL_S
        reader = _reader or _default_read
        try:
            raw = reader([_db_id(a) for a in sorted(candidates)])
        except Exception as exc:                  # keep the last known value, retry next interval
            log.warning("fastt_scope read failed (%s); keeping last known", type(exc).__name__)
            return
        first = not st.has_read
        for account in candidates:
            try:
                val = _value(raw.get(_db_id(account)))
            except Exception as exc:              # unreadable value: same as a failed read for this account
                log.warning("fastt_scope value for an account is unreadable (%s); keeping last known", type(exc).__name__)
                continue
            st.rows[account] = val
            st.known.add(account)
            all_, fans = _db_eff(account, val, *allow)
            if all_:
                st.ever_all.add(account)
                st.seen.setdefault((account, "*"), (now, first))
            for fan in fans:
                st.ever_fans.setdefault(account, set()).add(fan)
                st.seen.setdefault((account, fan), (now, first))
        st.has_read = True
    finally:
        st.lock.release()


def _compute(kind):
    """(fan pairs, whole accounts) for ``own`` | ``serve`` | ``desired`` (serve with the settle wait always)."""
    allow = parse_allowlist(os.environ.get(SCOPE_ENV))
    pairs, anyfan, dbonly = allow
    candidates = anyfan | dbonly | {a for a, _ in pairs}
    if not candidates:
        return set(), set()
    now = _clock()
    _refresh(candidates, now, allow)
    st = _state
    out_pairs, out_accounts = set(), set()

    def settled(key):
        entry = st.seen.get(key)
        if entry is None:
            return False
        seen_at, bootstrap = entry
        return (bootstrap and kind == "serve") or now - seen_at >= SETTLE_S

    with st.lock:
        for account in candidates:
            val = st.rows.get(account)
            env_pairs = {f for a, f in pairs if a == account}
            db_all, db_fans = _db_eff(account, val, *allow)
            if kind == "own":
                if (account in anyfan or db_all or account in st.ever_all
                        or (account in dbonly and account not in st.known)):
                    out_accounts.add(account)
                for fan in env_pairs | db_fans | st.ever_fans.get(account, set()):
                    out_pairs.add((account, fan))
                continue
            if val is None:                       # no database value: the env alone, exactly as before
                if account in anyfan:
                    out_accounts.add(account)
                out_pairs.update((account, f) for f in env_pairs)
                continue
            if db_all:
                if account in anyfan or settled((account, "*")):
                    out_accounts.add(account)
                else:                             # the switch to "all" is still settling: keep what already served
                    out_pairs.update((account, f) for (a, f), _ in st.seen.items()
                                     if a == account and f != "*" and settled((a, f)))
                continue
            out_pairs.update((account, f) for f in db_fans if account in anyfan or f in env_pairs or settled((account, f)))
    return out_pairs, out_accounts


def owned_scope():
    return _compute("own")


def serve_scope():
    return _compute("serve")


def desired_scope():
    """What the supervisor starts children for: the serve view, but the settle wait applies at its own start too."""
    return _compute("desired")
