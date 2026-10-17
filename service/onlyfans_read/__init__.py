"""The Twin dashboard's read-only OnlyFans API, served by this relay.

Scope of this package, in one paragraph: the Twin dashboard already has a
frontend read adapter with three operations (accounts, conversation list,
conversation detail). This package is the *service* behind them. It is
read-only by construction — there is no send path, no provider client and no
write method anywhere in it — and it refuses rather than guesses: an
unauthorized account, an unknown account, a missing credential and a missing
schema version are four different refusals, and none of them is ever an empty
success.

It exists because of the delivery lesson in the consolidation plan: the
dashboard renders projections and submits commands, and it does not own a
second writable ledger. So everything here reads the records the relay already
keeps, and the binding authority (`of_creator_bindings`) is the one place that
decides which creator and tenant an OnlyFans account belongs to.

Layout:
  • contract.py     — the schema version, the envelopes, the id and cursor rules
  • errors.py       — the refusal vocabulary (status + code + state)
  • auth.py         — service-to-service bearer auth and version negotiation
  • repo.py         — the read repository boundary (the only SQL in the package)
  • projections.py  — relay rows -> the dashboard's read models
  • service.py      — authorization + the three reads, over an injected repo
  • api.py          — the FastAPI router
"""

from __future__ import annotations

from .api import router
from .contract import SCHEMA_VERSION, SERVICE_PREFIX, is_service_path

__all__ = ["router", "SCHEMA_VERSION", "SERVICE_PREFIX", "is_service_path"]
