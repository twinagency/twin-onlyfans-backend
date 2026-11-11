"""Which accounts sell through approved commercial terms (Step 3 predicate).

THE STABLE SEAM: the Step 2 scope contract (0134, `scope_contract.py`) is the owner of "this account is commercial". Until it
lands on this branch the predicate below is derived from evidence this step owns: the account is not the legacy pilot (its
singleton scope keeps the old bounded, whole-catalogue semantics) and it has at least one live approved terms revision. At merge,
replace the body with `scope_contract.load(s, runtime)[2] == 'commercial'` and keep the signature; every Step 3 consumer calls only
this function.
"""
from sqlalchemy import select

from db.onlyfans_catalogue_terms_models import OfCatalogueTerms
from onlyfans_runtime.store import Refused


def commercial_scope(s, runtime):
    from .pilot_scope import current
    try:
        pilot, _ = current(s, runtime)
    except Refused:
        return False            # a damaged legacy pilot row is never reinterpreted as commercial
    if pilot is not None:
        return False
    return s.scalar(select(OfCatalogueTerms.item_id).where(OfCatalogueTerms.account_id == runtime.account_id,
                                                           OfCatalogueTerms.status == 'approved').limit(1)) is not None
