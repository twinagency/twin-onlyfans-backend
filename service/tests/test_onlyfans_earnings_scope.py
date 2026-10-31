"""Account scope for earnings: pure logic, no database."""
from types import SimpleNamespace
import pytest
from onlyfans_read.earnings import parse_scope,scope_bindings
from onlyfans_read.errors import BadRequest

B=[SimpleNamespace(account_id=i) for i in ("acc-a","acc-b","acc-c")]


def test_no_scope_keeps_every_authorized_account():
    assert [b.account_id for b in scope_bindings(B,None)]==["acc-a","acc-b","acc-c"]


def test_scope_is_intersected_with_authorized_accounts_and_unknown_ids_select_nothing():
    assert [b.account_id for b in scope_bindings(B,["acc-b","other-tenant"])]==["acc-b"]
    assert scope_bindings(B,["other-tenant"])==[]


def test_scope_parsing_is_bounded_and_deduplicated():
    assert parse_scope(None) is None
    assert parse_scope("acc-b,acc-a,acc-b")==["acc-a","acc-b"]
    for bad in ("","a b","a,,b","../x",",".join(f"a{i}" for i in range(51))):
        with pytest.raises(BadRequest):parse_scope(bad)
