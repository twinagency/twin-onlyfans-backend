"""AI vs human attribution for FASTT on Twin's database.

FASTT distinguishes "the bot sent this" from "a named human sent this" with two
facts on each outbound ``messages`` row: ``automation_kind`` (set for every AI
send, never for a human) and ``sent_by_employee_id`` (the system "Automation"
sentinel for the AI, any other employee for a named human; NULL = unattributed,
which FASTT deliberately does NOT treat as a named human). Twin's live
``employees`` table is empty, so FASTT's own lookup fails and writes NULL.

Smallest correct mapping (no code fork of FASTT's rule):

* ``Automation``  - FASTT's own sentinel row (its migration 0011), recreated.
* ``Twin staff``  - ONE inactive, user-less system row that Twin stamps on
                    human-typed sends it projects (onlyfans_runtime.text_delivery).

Nothing creates either row implicitly. The pilot only READS them: in send mode
it refuses with ``attribution-sentinels-missing`` until an operator has run

    DATABASE_URL=... python -m native_pilot.attribution seed

which inserts exactly the missing rows (idempotent, prints what it did) and
nothing else. ``status`` is read-only.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass

from onlyfans_runtime.staff_sentinels import AUTOMATION_NAME, HUMAN_NAME

from .ledger import Refused


@dataclass(frozen=True)
class Sentinels:
    automation: int | None
    human: int | None


async def _lookup(session, name):
    from sqlalchemy import select
    from db.models import Employee
    where = [Employee.display_name == name, Employee.is_active.is_(False)]
    if name != AUTOMATION_NAME:
        # FASTT resolves "Automation" by name + inactive only (employees.py); mirror that
        # exactly so this check and FASTT can never disagree about which row it is.
        where += [Employee.user_id.is_(None), Employee.chatter_id.is_(None)]
    ids = (await session.scalars(select(Employee.id).where(*where).limit(2))).all()
    if len(ids) > 1:
        raise Refused("attribution-sentinels-ambiguous")
    return ids[0] if ids else None


async def read_sentinels() -> Sentinels:
    from db.engine import get_session
    async with get_session() as s:
        return Sentinels(await _lookup(s, AUTOMATION_NAME), await _lookup(s, HUMAN_NAME))


async def require_sentinels() -> Sentinels:
    found = await read_sentinels()
    if found.automation is None or found.human is None:
        raise Refused("attribution-sentinels-missing")
    return found


async def seed_sentinels() -> list[str]:
    """Create ONLY the missing rows. Explicit operator action; returns created names."""
    from db.engine import get_session
    from db.models import Employee
    created = []
    async with get_session() as s:
        for name in (AUTOMATION_NAME, HUMAN_NAME):
            if await _lookup(s, name) is None:
                s.add(Employee(display_name=name, color="#808080", is_active=False))
                created.append(name)
    return created


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv not in (["status"], ["seed"]):
        print("usage: python -m native_pilot.attribution status|seed", file=sys.stderr)
        return 2

    async def go():
        from db.engine import engine
        try:
            if argv == ["seed"]:
                created = await seed_sentinels()
                print(json.dumps({"created": created}))
            found = await read_sentinels()
            print(json.dumps({"automation": found.automation is not None, "human": found.human is not None}))
        finally:
            await engine.dispose()
    try:
        asyncio.run(go())
    except Refused as exc:
        print(json.dumps({"refused": exc.reason}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
