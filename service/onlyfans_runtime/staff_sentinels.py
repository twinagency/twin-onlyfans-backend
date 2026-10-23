"""Names of the two system rows that tell FASTT who sent an outbound message.

FASTT's human-reply detection (``ai_chatter._is_named_chatter_send``) needs an
outbound row to carry an employee id: the "Automation" sentinel marks the AI,
any other id marks a named human. The live ``employees`` table is empty, so both
are missing until an operator creates them explicitly
(``python -m native_pilot.attribution seed``). Neither is a person: both are
inactive, belong to no user and no chatter, and never appear in a picker.

This module only reads. With the rows absent every caller behaves exactly as it
did before this module existed (the column stays NULL).
"""
from __future__ import annotations

AUTOMATION_NAME = "Automation"       # FASTT's own sentinel (its migration 0011)
HUMAN_NAME = "Twin staff"            # every human-typed send made through Twin


def _unique_id(session, name):
    from sqlalchemy import select
    from db.models import Employee
    ids = session.scalars(select(Employee.id).where(
        Employee.display_name == name, Employee.is_active.is_(False),
        Employee.user_id.is_(None), Employee.chatter_id.is_(None)).limit(2)).all()
    return ids[0] if len(ids) == 1 else None


def human_employee_id(session):
    """The Twin-staff sentinel id, or None when it has not been created (or is ambiguous)."""
    return _unique_id(session, HUMAN_NAME)
