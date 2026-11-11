"""The Step 3 commercial predicate follows the Step 2 scope contract (integration check)."""
from sqlalchemy.orm import Session

from conversation_engine.commercial_scope import commercial_scope
from test_commercial_preparation_pg import TENANT
from test_commercial_scope_promote_pg import prepared, promote
from test_onlyfans_foundation_pg import database  # noqa: F401


def _flag(store, account):
    with Session(store.engine) as s:
        return commercial_scope(s, store._runtime(s, TENANT, account, lock=False))


def test_prepared_account_without_terms_is_not_commercial_until_promoted(database):
    store, account, _ = prepared(database)
    try:
        assert _flag(store, account) is False
        promote(store, account)
        assert _flag(store, account) is True      # promoted by the scope contract, no terms row needed
    finally:
        store.close()


def test_promoting_one_account_does_not_make_another_commercial(database):
    store, first, _ = prepared(database, 1)
    _, second, _ = prepared(database, 2)
    try:
        promote(store, first)
        assert _flag(store, first) is True
        assert _flag(store, second) is False       # same database, different account: unaffected
    finally:
        store.close()
