"""Engine v2 durable state that is not send authority: the account runner lease and the work board.

* ``RunnerLease``: at most one live engine process per OnlyFans account, with an expiry, so a crash
  frees the account by itself and a second process (a rolling deploy, a hand-started runner) can
  never serve the same account at the same time. It sits ON TOP of the per-fan pilot lease and the
  conversation owner token, which remain the only send authority.
* ``WorkBoard``: a per-fan read model (state + fixed reason + since when) so nothing waits
  silently. It is written by the engine and read by the dashboard; deleting it changes no send
  decision.

All writes are the ledger's short serialised transactions; nothing here is held across a model call
or a network request.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from .ledger import Ledger, Refused, _dt

WORK_STATES = ("waiting", "quiet", "preparing", "sending", "held", "blocked")


class RunnerLease:
    def __init__(self, ledger: Ledger, account_id: str, holder: str, ttl_s: float):
        self.ledger, self.account_id, self.holder, self.ttl_s = ledger, account_id, holder[:120], float(ttl_s)
        self.token = None
        self.generation = 0

    def acquire(self, now) -> int:
        """Take the account's lease; raises ``runner-lease-held`` while another owner's is live."""
        now_s = _dt(now)
        until = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=self.ttl_s))
        token = self.token or "runner-" + secrets.token_hex(16)
        with self.ledger._tx(initialize=True) as con:
            row = con.execute("SELECT owner_token,expires_at,generation FROM engine_runner_lease WHERE account_id=?",
                              (self.account_id,)).fetchone()
            if row is None:
                con.execute("INSERT INTO engine_runner_lease(account_id,owner_token,holder,generation,acquired_at,"
                            "heartbeat_at,expires_at) VALUES(?,?,?,1,?,?,?)",
                            (self.account_id, token, self.holder, now_s, now_s, until))
                gen = 1
            else:
                if row["owner_token"] != token and _dt(row["expires_at"]) > now_s:
                    raise Refused("runner-lease-held")
                gen = row["generation"] + (0 if row["owner_token"] == token else 1)
                con.execute("UPDATE engine_runner_lease SET owner_token=?,holder=?,generation=?,acquired_at=?,"
                            "heartbeat_at=?,expires_at=? WHERE account_id=?",
                            (token, self.holder, gen,
                             now_s if row["owner_token"] != token else self._acquired(con), now_s, until, self.account_id))
        self.token, self.generation = token, gen
        return gen

    def _acquired(self, con):
        return con.execute("SELECT acquired_at FROM engine_runner_lease WHERE account_id=?", (self.account_id,)).fetchone()[0]

    def renew(self, now) -> bool:
        """Extend our lease. False (and the lease is gone) when it lapsed and someone else holds it."""
        if self.token is None:
            return False
        now_s = _dt(now)
        until = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=self.ttl_s))
        with self.ledger._tx(initialize=True) as con:
            row = con.execute("SELECT owner_token,expires_at FROM engine_runner_lease WHERE account_id=?",
                              (self.account_id,)).fetchone()
            if row is None or row["owner_token"] != self.token:
                self.token = None
                return False
            # A lapsed lease that nobody took over may still be extended: the fan-level fencing decides safety.
            con.execute("UPDATE engine_runner_lease SET heartbeat_at=?,expires_at=? WHERE account_id=? AND owner_token=?",
                        (now_s, until, self.account_id, self.token))
        return True

    def release(self, now) -> None:
        if self.token is None:
            return
        now_s = _dt(now)
        with self.ledger._tx(initialize=True) as con:
            con.execute("UPDATE engine_runner_lease SET expires_at=?,heartbeat_at=? WHERE account_id=? AND owner_token=?",
                        (now_s, now_s, self.account_id, self.token))
        self.token = None

    def holder_of(self) -> dict | None:
        with self.ledger._tx(initialize=True) as con:
            row = con.execute("SELECT holder,generation,heartbeat_at,expires_at FROM engine_runner_lease WHERE account_id=?",
                              (self.account_id,)).fetchone()
        return dict(row) if row else None


class WorkBoard:
    """Per-fan work state with a fixed reason. Upserts only on change; one transaction per flush."""

    def __init__(self, ledger: Ledger, account_id: str):
        self.ledger, self.account_id = ledger, account_id
        self._known: dict[int, tuple] = {}
        self._dirty: dict[int, tuple | None] = {}

    def set(self, fan: int, state: str, reason: str | None = None, *, message_id=None, waiting_since=None) -> None:
        if state not in WORK_STATES:
            raise Refused("work-state")
        value = (state, reason, message_id, _dt(waiting_since) if waiting_since else None)
        current = self._dirty[fan] if fan in self._dirty else self._known.get(fan)
        if current == value:
            return
        self._dirty[fan] = value

    def clear(self, fan: int) -> None:
        if fan in self._known or fan in self._dirty:
            self._dirty[fan] = None

    def pending(self) -> int:
        return len(self._dirty)

    def flush(self, now) -> int:
        """Write every change in ONE transaction. Returns the number of rows touched."""
        if not self._dirty:
            return 0
        now_s = _dt(now)
        batch, self._dirty = self._dirty, {}
        try:
            with self.ledger._tx(initialize=True) as con:
                for fan, value in batch.items():
                    if value is None:
                        con.execute("DELETE FROM engine_work WHERE account_id=? AND fan_id=?", (self.account_id, fan))
                        continue
                    state, reason, mid, since = value
                    prior = con.execute("SELECT state,reason,state_since,attempts FROM engine_work WHERE account_id=? AND fan_id=?",
                                        (self.account_id, fan)).fetchone()
                    if prior is None:
                        con.execute("INSERT INTO engine_work(account_id,fan_id,state,reason,message_id,waiting_since,state_since,"
                                    "attempts,updated_at) VALUES(?,?,?,?,?,?,?,0,?)",
                                    (self.account_id, fan, state, reason, mid, since, now_s, now_s))
                    else:
                        changed = (prior["state"], prior["reason"]) != (state, reason)
                        con.execute("UPDATE engine_work SET state=?,reason=?,message_id=?,waiting_since=?,state_since=?,"
                                    "attempts=attempts+?,updated_at=? WHERE account_id=? AND fan_id=?",
                                    (state, reason, mid, since, now_s if changed else prior["state_since"],
                                     1 if changed and state in ("preparing", "sending") else 0, now_s, self.account_id, fan))
        except Exception:
            for fan, value in batch.items():          # keep the changes for the next flush; the board is best effort
                self._dirty.setdefault(fan, value)
            raise
        for fan, value in batch.items():
            if value is None:
                self._known.pop(fan, None)
            else:
                self._known[fan] = value
        return len(batch)

    def summary(self) -> dict:
        with self.ledger._tx(initialize=True) as con:
            rows = con.execute("SELECT state,reason,COUNT(*) AS n,MIN(waiting_since) AS oldest FROM engine_work "
                               "WHERE account_id=? GROUP BY state,reason", (self.account_id,)).fetchall()
        return {"rows": [dict(r) for r in rows]}
