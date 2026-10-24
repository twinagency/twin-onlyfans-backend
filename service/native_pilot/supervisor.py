"""Runner supervisor: one isolated FASTT service process per active OnlyFans account.

``python -m native_pilot.supervisor`` runs until SIGTERM/SIGINT. Every ``FASTT_SUPERVISOR_INTERVAL_S``
seconds (default 30, bounded 5..600) it works out which accounts SHOULD have a runner and makes
reality match, by spawning plain OS child processes::

    python -m native_pilot.service --account <id> (--fan N | --all-fans) --handle <h>
        --twin-context --content-manifest <generated> --enable-send

No Docker socket and no web-app control: the dashboard only writes database state, and this process
only READS it. Each child keeps the isolation the service already relies on (its own process-local
hooks, role guard, pilot row and lease); the supervisor never imports the engine or the model, and it
never decrypts a connection credential.

An account is desired when ALL of these hold (checked read-only, in this order; the first failure is
the visible hold reason):

* ``of_runtime.enabled`` and a live (not revoked) ``of_creator_bindings`` row that the runtime points at
  (``runtime-missing`` / ``runtime-disabled`` / ``binding-revoked`` / ``binding-scope``);
* ``of_delivery_policy.enabled`` (``policy-disabled``);
* a ``verified`` ``of_provider_connection`` for the same binding, without a failing check, unexpired and
  owned by the account's OnlyFans id (``connection-*``; the sealed credential is NOT opened here, the child
  does that);
* the account is named by the deployment ownership scope ``ONLYFANS_FASTT_SCOPE`` (read through
  ``onlyfans_runtime.fastt_manual._scope``, which stays the single source of who may send):
  ``<account>:*`` runs ``--all-fans``, exactly one ``<account>:<fan>`` pair runs ``--fan <fan>``, several
  pairs for one account are held (``scope-multi-fan-unsupported``). An account that is active in the
  database but outside the scope is never started (the legacy worker keeps it);
* a handle: the operator env map ``FASTT_HANDLES=of-<owner>:<name>,...`` if it names the account, else the
  binding's stored handle, else ``handle-missing`` (``handle-invalid`` if it is not a plain handle);
* a manifest built (``native_pilot.manifest.build`` then ``verify``) with no blocker
  (``manifest:<reason>``), written atomically and privately into ``FASTT_MANIFEST_DIR``.

Per account: not running -> start; the config hash (arguments + manifest) changed -> SIGTERM, wait up to the
grace (60 s), SIGKILL only if still alive, then start the new one; a child that exits is restarted with
exponential backoff 5 s .. 300 s; 5 exits within 10 minutes hold the account as ``crash-loop`` until its
config hash changes; an account that leaves the desired set gets SIGTERM. Accounts never wait for each
other: stopping, backing off and starting are per-account states advanced in small steps. SIGTERM to the
supervisor stops every child cleanly (the service treats SIGTERM as a clean stop), then exits.

Health: ``FASTT_SUPERVISOR_STATUS`` (default ``/var/lib/fastt/supervisor-status.json``) is rewritten
atomically with one entry per account (running / held / restarting / stopping, reason, no pid), and JSON
event lines go to stdout like ``runner.emit``. Nothing is written to the database.

Run EITHER this supervisor OR a hand-written ``fastt-runner`` for an account, never both.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from twin_bridge import store

from .ledger import Refused
from .scope import AccountScope

INTERVAL_S = 30.0
POLL_S = 1.0
STOP_GRACE_S = 60.0
KILL_WAIT_S = 10.0
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
CRASH_EXITS = 5
CRASH_WINDOW_S = 600.0
MAX_ACCOUNTS = 64
HANDLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
DEFAULT_MANIFEST_DIR = "/var/lib/fastt/manifests"
DEFAULT_STATUS = "/var/lib/fastt/supervisor-status.json"


def emit(event):
    print(json.dumps(event, sort_keys=True, allow_nan=False), flush=True)


def _num_env(name, default, lo, hi):
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return min(hi, max(lo, value)) if value == value else default


@dataclass(frozen=True)
class Spec:
    """What one child should be running: exact arguments plus the manifest it will read."""
    account: str
    fan: object                 # int, or "*" for --all-fans
    args: tuple
    manifest: bytes = field(repr=False)
    hash: str


@dataclass(frozen=True)
class Hold:
    reason: str


# ── the desired set, read-only ───────────────────────────────────────────────

def read_account(db, account):
    """The database facts the decision needs for one account (no credential column is selected)."""
    with store.readonly(db) as con:
        row = con.execute(
            "SELECT r.enabled AS runtime_enabled, r.binding_id AS binding_id, b.account_id AS binding_account,"
            " b.channel AS binding_channel, b.revoked_at AS binding_revoked_at, b.handle AS handle,"
            " p.enabled AS policy_enabled, c.binding_id AS connection_binding, c.owner_id AS owner_id,"
            " c.status AS connection_status, c.verified_at AS verified_at, c.last_error AS last_error,"
            " c.expires_at AS expires_at"
            " FROM of_runtime r"
            " LEFT JOIN of_creator_bindings b ON b.id = r.binding_id"
            " LEFT JOIN of_delivery_policy p ON p.account_id = r.account_id"
            " LEFT JOIN of_provider_connection c ON c.account_id = r.account_id"
            " WHERE r.account_id = ?", (account,)).fetchone()
    return None if row is None else dict(row)


def vault_mode_enabled(db, account):
    """True only when the account's ai_chatter_config_json carries ``fastt_vault_mode: true`` (default off)."""
    try:
        with store.readonly(db) as con:
            row = con.execute("SELECT ai_chatter_config_json FROM account_ai_config WHERE account_id=?", (account,)).fetchone()
        value = json.loads(row[0]) if row and row[0] else {}
    except Exception:
        return False
    return isinstance(value, dict) and value.get("fastt_vault_mode") is True


def _naive_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def inactive_reason(row, scope: AccountScope, now):
    """None when the database says the account may run, else the fixed public reason."""
    if row is None:
        return "runtime-missing"
    if not row["runtime_enabled"]:
        return "runtime-disabled"
    if row["binding_account"] is None:
        return "binding-scope"
    if row["binding_revoked_at"] is not None:
        return "binding-revoked"
    if row["binding_account"] != scope.db_id or row["binding_channel"] != "onlyfans":
        return "binding-scope"
    if row["policy_enabled"] is None or not row["policy_enabled"]:
        return "policy-disabled"
    if row["connection_status"] is None:
        return "connection-missing"
    if row["connection_binding"] != row["binding_id"]:
        return "connection-scope"
    if row["connection_status"] != "verified" or row["verified_at"] is None:
        return "connection-not-verified"
    if row["last_error"] is not None:
        return "connection-check-failing"
    try:
        if _naive_utc(row["expires_at"]) <= now:
            return "connection-expired"
    except (TypeError, ValueError):
        return "connection-expired"
    if str(row["owner_id"]) != scope.provider_id:
        return "connection-owner-mismatch"
    return None


def read_scope():
    """(fan pairs, whole accounts), bare owner ids: exactly what the senders themselves use."""
    from onlyfans_runtime.fastt_manual import _scope
    return _scope()


def parse_handles(raw):
    out = {}
    for item in (raw or "").split(","):
        account, _, name = item.strip().partition(":")
        if not account or not name:
            continue
        try:
            out[AccountScope.parse(account).provider_id] = name.strip().lstrip("@")
        except Refused:
            continue
    return out


def _manifest_target(db, account, fan):
    """(manifest bytes, None) or (None, reason): build + verify exactly as the runner will load it."""
    from . import manifest as builder
    try:
        manifest, blockers = builder.build(db, account, fan)
        if blockers:
            return None, "manifest:" + blockers[0]["reason"]
        builder.verify(db, manifest, fan)
    except Refused as exc:
        return None, "manifest:" + exc.reason
    return json.dumps(manifest, sort_keys=True).encode("utf-8"), None


def atomic_write(path, data: bytes, mode=0o600):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── child processes ──────────────────────────────────────────────────────────

class ProcChild:
    """A real child. Its output is re-emitted line by line, tagged with the account."""

    def __init__(self, argv, account):
        self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=str(Path(__file__).resolve().parents[1]))
        self.account = account
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                data = None
            emit({"event": "child", "account": self.account, **({"data": data} if isinstance(data, dict) else {"line": line[:500]})})

    def poll(self):
        return self.proc.poll()

    def terminate(self):
        self.proc.terminate()

    def kill(self):
        self.proc.kill()


def spawn_process(argv, account):
    return ProcChild([sys.executable, "-m", "native_pilot.service", *argv], account)


# ── per-account state ────────────────────────────────────────────────────────

class Slot:
    def __init__(self, account):
        self.account = account
        self.child = None
        self.spec = None            # the spec the current/last child runs
        self.state = "held"         # held | running | restarting | stopping
        self.reason = "starting"
        self.exits = deque()
        self.next_start = 0.0
        self.stop_deadline = 0.0
        self.killed = False
        self.pending = None         # after a stop: ("start", Spec) | ("hold", reason)
        self.crash_hash = None
        self.restarts = 0
        self.started = False
        self.fan = None


class Supervisor:
    def __init__(self, db, *, manifest_dir=None, status_path=None, handles=None, spawn=spawn_process,
                 scope_reader=read_scope, account_reader=read_account, clock=time.monotonic, sleep=time.sleep,
                 wall=None, emit=emit, interval_s=None, grace_s=None, poll_s=POLL_S,
                 backoff_min_s=BACKOFF_MIN_S, backoff_max_s=BACKOFF_MAX_S, crash_exits=CRASH_EXITS,
                 crash_window_s=CRASH_WINDOW_S):
        self.db = db
        self.manifest_dir = Path(manifest_dir or os.environ.get("FASTT_MANIFEST_DIR") or DEFAULT_MANIFEST_DIR)
        self.status_path = Path(status_path or os.environ.get("FASTT_SUPERVISOR_STATUS") or DEFAULT_STATUS)
        self.handles = parse_handles(os.environ.get("FASTT_HANDLES")) if handles is None else dict(handles)
        self.spawn, self.scope_reader, self.account_reader = spawn, scope_reader, account_reader
        self.clock, self.sleep, self.emit = clock, sleep, emit
        self.wall = wall or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self.interval_s = _num_env("FASTT_SUPERVISOR_INTERVAL_S", INTERVAL_S, 5, 600) if interval_s is None else interval_s
        self.grace_s = _num_env("FASTT_SUPERVISOR_GRACE_S", STOP_GRACE_S, 5, 300) if grace_s is None else grace_s
        self.poll_s, self.backoff_min_s, self.backoff_max_s = poll_s, backoff_min_s, backoff_max_s
        self.crash_exits, self.crash_window_s = crash_exits, crash_window_s
        self.slots: dict[str, Slot] = {}
        self.next_reconcile = 0.0
        self._last_status = None

    # desired set -> targets
    def targets(self):
        """({db account id: Spec | Hold} for every account the scope names, ids whose read failed)."""
        pairs, accounts = self.scope_reader()
        bare = sorted(set(accounts) | {a for a, _ in pairs})
        out, unknown = {}, set()
        for owner in bare[:MAX_ACCOUNTS]:
            account = "of-" + owner
            if not AccountScope.valid(account):
                continue
            try:
                out[account] = self.target(account, owner, pairs, accounts)
            except Exception as exc:     # a transient read failure must not stop or restart a running child
                unknown.add(account)
                self.emit({"event": "evaluate-error", "account": account, "error": type(exc).__name__})
        for owner in bare[MAX_ACCOUNTS:]:
            out["of-" + owner] = Hold("too-many-accounts")
        return out, unknown

    def target(self, account, owner, pairs, accounts):
        scope = AccountScope.parse(account)
        row = self.account_reader(self.db, account)
        reason = inactive_reason(row, scope, self.wall())
        if reason:
            return Hold(reason)
        fans = sorted(f for a, f in pairs if a == owner)
        if owner in accounts:
            fan = "*"
        elif len(fans) == 1:
            fan = fans[0]
        else:
            return Hold("scope-multi-fan-unsupported")
        handle = self.handles.get(scope.provider_id) or (row.get("handle") or "").strip().lstrip("@")
        if not handle:
            return Hold("handle-missing")
        if not HANDLE.fullmatch(handle):
            return Hold("handle-invalid")
        scope_args = ("--account", account, *(("--all-fans",) if fan == "*" else ("--fan", str(fan))),
                      "--handle", handle, "--twin-context")
        if vault_mode_enabled(self.db, account):
            # FASTT's stock vault selling: no manifest is built or listed (see content.VaultPlan).
            manifest, args = b"", (*scope_args, "--vault-mode", "--enable-send")
        else:
            manifest, reason = _manifest_target(self.db, account, fan)
            if reason:
                return Hold(reason)
            path = self.manifest_dir / f"{account}.json"
            args = (*scope_args, "--content-manifest", str(path), "--enable-send")
        digest = hashlib.sha256(json.dumps(list(args)).encode() + b"\0" + manifest).hexdigest()
        return Spec(account, fan, args, manifest, digest)

    # transitions
    def _held(self, slot, reason):
        slot.child, slot.state, slot.reason, slot.pending = None, "held", reason, None

    def _stop(self, slot, now, pending):
        slot.pending = pending
        if slot.state == "stopping":
            return
        slot.state, slot.reason, slot.killed = "stopping", "stopping", False
        slot.stop_deadline = now + self.grace_s
        self.emit({"event": "child-stop", "account": slot.account, "why": pending[0] if pending[0] == "start" else pending[1]})
        try:
            slot.child.terminate()
        except OSError:
            pass

    def _start(self, slot, spec, now):
        try:
            if spec.manifest:
                atomic_write(self.manifest_dir / f"{spec.account}.json", spec.manifest)
        except OSError:
            self._held(slot, "manifest:write-failed")
            return
        slot.fan = spec.fan
        slot.spec = spec
        try:
            slot.child = self.spawn(list(spec.args), spec.account)
        except Exception:
            self._exited(slot, now, "spawn-failed")
            return
        if slot.started:
            slot.restarts += 1
        slot.started = True
        slot.state, slot.reason = "running", None
        self.emit({"event": "child-start", "account": spec.account, "config": spec.hash[:12],
                   "scope": "all-fans" if spec.fan == "*" else "fan"})

    def _exited(self, slot, now, code):
        slot.child = None
        while slot.exits and now - slot.exits[0] > self.crash_window_s:
            slot.exits.popleft()
        slot.exits.append(now)
        if len(slot.exits) >= self.crash_exits:
            slot.crash_hash = slot.spec.hash
            slot.state, slot.reason = "held", "crash-loop"
            self.emit({"event": "crash-loop", "account": slot.account, "exits": len(slot.exits)})
            return
        delay = min(self.backoff_max_s, self.backoff_min_s * 2 ** (len(slot.exits) - 1))
        slot.state, slot.reason, slot.next_start = "restarting", f"exit:{code}", now + delay
        self.emit({"event": "child-exit", "account": slot.account, "code": code, "restart_in_s": delay})

    def _apply(self, slot, target, now):
        if isinstance(target, Hold):
            if slot.child is not None:
                self._stop(slot, now, ("hold", target.reason))
            elif (slot.state, slot.reason) != ("held", target.reason):
                self._held(slot, target.reason)
            return
        if slot.crash_hash is not None:
            if slot.crash_hash == target.hash:
                slot.state, slot.reason = "held", "crash-loop"
                return
            slot.crash_hash = None
            slot.exits.clear()
        if slot.child is not None:
            if slot.state == "stopping":
                slot.pending = ("start", target)
            elif slot.spec.hash != target.hash:
                self._stop(slot, now, ("start", target))
            return
        if slot.state == "restarting" and slot.spec.hash == target.hash:
            return
        if slot.state == "restarting":
            slot.exits.clear()
        self._start(slot, target, now)

    def reconcile(self, now):
        try:
            targets, unknown = self.targets()
        except Exception as exc:
            self.emit({"event": "desired-set-unavailable", "error": type(exc).__name__})
            return
        for account, target in targets.items():
            self._apply(self.slots.setdefault(account, Slot(account)), target, now)
        for account, slot in list(self.slots.items()):
            if account not in targets and account not in unknown:
                if slot.child is None:
                    del self.slots[account]
                else:
                    self._apply(slot, Hold("not-in-scope"), now)

    def supervise(self, now):
        for slot in self.slots.values():
            if slot.state == "running":
                code = slot.child.poll()
                if code is not None:
                    self._exited(slot, now, code)
            elif slot.state == "stopping":
                if slot.child.poll() is not None:
                    pending, slot.pending = slot.pending, None
                    slot.child = None
                    if pending and pending[0] == "start":
                        self._start(slot, pending[1], now)
                    else:
                        self._held(slot, pending[1] if pending else "stopped")
                elif now >= slot.stop_deadline and not slot.killed:
                    slot.killed = True
                    self.emit({"event": "child-kill", "account": slot.account})
                    try:
                        slot.child.kill()
                    except OSError:
                        pass
            elif slot.state == "restarting" and now >= slot.next_start:
                self._start(slot, slot.spec, now)

    def step(self, now=None):
        now = self.clock() if now is None else now
        self.supervise(now)
        beat = now >= self.next_reconcile
        if beat:
            self.next_reconcile = now + self.interval_s
            self.reconcile(now)
            self.supervise(now)
        self.write_status(force=beat)

    # health
    def status(self):
        return {"updated_at": self.wall().isoformat(timespec="seconds") + "Z", "interval_s": self.interval_s,
                "accounts": {a: {"state": s.state, "reason": s.reason, "fan": s.fan, "restarts": s.restarts,
                                 "config": s.spec.hash[:12] if s.spec and s.state != "held" else None}
                             for a, s in sorted(self.slots.items())}}

    def write_status(self, force=True):
        """Rewritten on every change, and as a heartbeat after each reconcile."""
        body = self.status()
        key = json.dumps(body["accounts"], sort_keys=True)
        if key == self._last_status and not force:
            return
        try:
            atomic_write(self.status_path, json.dumps(body, sort_keys=True).encode("utf-8"), 0o600)
            self._last_status = key
        except OSError:
            self.emit({"event": "status-write-failed"})

    def run(self, stop: threading.Event):
        self.emit({"event": "supervisor-start", "interval_s": self.interval_s})
        try:
            while not stop.is_set():
                self.step()
                stop.wait(self.poll_s)
        finally:
            self.shutdown()

    def shutdown(self):
        """SIGTERM every child at once, wait the grace, SIGKILL stragglers, then return."""
        now = self.clock()
        for slot in self.slots.values():
            if slot.child is not None and slot.state != "stopping":
                self._stop(slot, now, ("hold", "supervisor-stopping"))
            elif slot.child is None:
                self._held(slot, "supervisor-stopping")
            else:
                slot.pending = ("hold", "supervisor-stopping")
        limit = now + self.grace_s + KILL_WAIT_S
        while any(s.child is not None for s in self.slots.values()):
            if self.clock() > limit:
                self.emit({"event": "child-unstopped",
                           "accounts": sorted(a for a, s in self.slots.items() if s.child is not None)})
                break
            self.sleep(self.poll_s)
            self.supervise(self.clock())
        self.write_status()
        self.emit({"event": "supervisor-finished"})


def main(argv=None):
    from twin_bridge.coordination import store_target
    db = store_target()
    if not db:
        emit({"event": "not-running", "reason": "pilot-store-required"})
        return 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        Supervisor(db).run(stop)
    except Exception as exc:
        emit({"event": "not-running", "reason": exc.reason if isinstance(exc, Refused) else "supervisor-error"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
