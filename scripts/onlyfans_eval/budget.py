"""Private evaluation spend journal, shared by both routes and process restarts.

This is local test bookkeeping, not delivery or PostgreSQL authority. A crashed
or ambiguous call keeps its reservation and blocks further spending. Never
delete that reservation to retry without reconciling provider billing first.
"""
from contextlib import contextmanager
from decimal import Decimal
import json
import os
from pathlib import Path
import uuid


class BudgetRefused(RuntimeError):
    pass


class Budget:
    def __init__(self, path, *, cap, model, price_in, price_out):
        self.path = Path(path)
        self.spec = {"cap": str(Decimal(str(cap))), "model": model,
                     "price_in": str(Decimal(str(price_in))), "price_out": str(Decimal(str(price_out)))}

    @contextmanager
    def _locked(self):
        lock = self.path.with_name(self.path.name + ".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise BudgetRefused("evaluation budget is locked; do not remove a live or unreconciled lock") from None
        try:
            os.close(fd)
            state = json.loads(self.path.read_text()) if self.path.exists() else {**self.spec, "spent": "0", "pending": None}
            if any(state.get(k) != v for k, v in self.spec.items()):
                raise BudgetRefused("evaluation budget model, prices or cap differ from the existing journal")
            yield state
            temp = self.path.with_name(self.path.name + ".tmp")
            with temp.open("w", encoding="utf-8") as f:
                json.dump(state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        finally:
            lock.unlink()

    def reserve(self, amount):
        amount = Decimal(str(amount))
        with self._locked() as state:
            if state["pending"]:
                raise BudgetRefused("evaluation call has unresolved cost; reconcile provider billing before continuing")
            if amount <= 0 or Decimal(state["spent"]) + amount > Decimal(self.spec["cap"]):
                raise BudgetRefused("remaining evaluation budget cannot cover the next call")
            ident = uuid.uuid4().hex
            state["pending"] = {"id": ident, "reserved": str(amount)}
        return ident

    def settle(self, ident, cost):
        cost = Decimal(str(cost))
        with self._locked() as state:
            pending = state["pending"]
            if not pending or pending["id"] != ident:
                raise BudgetRefused("evaluation reservation does not match")
            if not cost.is_finite() or cost < 0 or cost > Decimal(pending["reserved"]):
                raise BudgetRefused("provider cost exceeds its reservation or is invalid; reservation retained")
            state["spent"] = str(Decimal(state["spent"]) + cost)
            state["pending"] = None
