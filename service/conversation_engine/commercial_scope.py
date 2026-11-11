"""Which accounts sell through approved commercial terms (Step 3 predicate, reconciled with the Step 2 scope contract).

An account is commercial when its OWN scope row claims `commercial.v1` (promoted by Step 2, `scope_contract.py`), or, for a prepared
account that is not yet promoted but already carries a live approved terms revision, when that evidence exists. The legacy pilot
(scope_id 1) is never commercial: its singleton scope keeps the old bounded, whole-catalogue semantics. A damaged scope row is never
reinterpreted as commercial. Both routes make the stricter commercial gates apply, never fewer.
"""
from sqlalchemy import select

from db.onlyfans_catalogue_terms_models import OfCatalogueTerms
from onlyfans_runtime.store import Refused


def commercial_scope(s, runtime):
    from . import scope_contract as sc
    from .pilot_scope import current
    row = sc.row_for(s, runtime)
    if row is not None and row.scope_id != 1:
        try:
            if sc.claim(row) == sc.COMMERCIAL:
                return True     # promoted by the Step 2 contract
        except Refused:
            return False
    try:
        pilot, _ = current(s, runtime)
    except Refused:
        return False            # a damaged legacy pilot row is never reinterpreted as commercial
    if pilot is not None:
        return False
    return s.scalar(select(OfCatalogueTerms.item_id).where(OfCatalogueTerms.account_id == runtime.account_id,
                                                           OfCatalogueTerms.status == 'approved').limit(1)) is not None


TERMS_MODE = 'commercial.v1'


def terms_mode_view(s, runtime):
    """{'terms_mode': 'commercial.v1'} for commercial accounts, {} otherwise (legacy snapshots gain no key and keep their digest)."""
    return {'terms_mode': TERMS_MODE} if commercial_scope(s, runtime) else {}
