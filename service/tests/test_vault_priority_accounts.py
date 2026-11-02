"""ONLYFANS_VAULT_PRIORITY_ACCOUNTS only reorders which pending vault job is picked next."""
import os

from sqlalchemy.dialects import postgresql

from onlyfans_runtime import vault_management as vm


def _sql(expr):
    return str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_unset_keeps_order(monkeypatch):
    monkeypatch.delenv("ONLYFANS_VAULT_PRIORITY_ACCOUNTS", raising=False)
    assert vm.vault_priority_accounts() == ()
    assert "IS NULL" in _sql(vm._priority_order())


def test_valid_accounts_parsed_and_invalid_dropped(monkeypatch):
    monkeypatch.setenv("ONLYFANS_VAULT_PRIORITY_ACCOUNTS", " of-312988353,of-63665785 ,bad id;drop,,")
    assert vm.vault_priority_accounts() == ("of-312988353", "of-63665785")
    sql = _sql(vm._priority_order())
    assert "'of-312988353'" in sql and "'of-63665785'" in sql and "bad" not in sql
