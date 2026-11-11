"""History reads go through the existing preparation request guard with per-source sub-budgets.

Real PostgreSQL, mocked wire. The guard allowlist is extended deliberately: read-only GETs for known fans'
chat history, transactions and chargebacks, each pinned to the account, the configured start date and bounded
query shapes. A spent source refuses; the others (and the vault) keep reading.
"""
import json
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_commercial_preparation_pg import setup, TENANT, ACTOR
from history_support import BASE, START, key, spec, sources, prepared, rows
from conversation_engine import preparation as prep
from onlyfans_runtime.store import Refused


def hist():
    from fans_api_adapter import history_import
    return history_import


def models():
    from db.onlyfans_history_models import OfFansImportSource, OfFansReadMeter
    return OfFansImportSource, OfFansReadMeter


def guard(store, account):
    return prep.PreparationRequestGuard(store, TENANT, account, 1)


def urls(cfg, fan=401):
    a = cfg["provider_account"]
    return {"chats": f"{BASE}/api/{a}/chats/{fan}/messages?limit=100&order=desc",
            "transactions": f"{BASE}/api/{a}/transactions?limit=100&startDate=2016-01-01+00%3A00%3A00",
            "chargebacks": f"{BASE}/api/{a}/chargebacks?limit=100&offset=0&start_date=2016-01-01+00%3A00%3A00",
            "vault": f"{BASE}/api/{a}/media/vault?limit=24&offset=0&field=recent&sort=desc"}


def counters(database, account):
    Source, Meter = models()
    with Session(database[1]) as s:
        used = {r.source: (r.requests_used, r.credits_reserved) for r in s.scalars(select(Source).where(Source.account_id == account))}
        meter = {(r.source): (r.requests, r.credits_reserved) for r in s.scalars(select(Meter).where(Meter.account_id == account))}
    return used, meter


def test_history_reads_are_refused_until_an_explicit_import_is_configured(database):
    store, account, cfg = prepared(database, begin=False)
    try:
        for kind, url in urls(cfg).items():
            if kind == "vault":
                continue
            with pytest.raises(Refused):
                guard(store, account)("GET", url, True)
        assert prep.view(store, TENANT, account)["providerRequestsUsed"] == 0
        # the vault read the preparation worker already performed is unchanged and creates no import accounting
        guard(store, account)("GET", urls(cfg)["vault"], True)
        assert counters(database, account) == ({}, {})
    finally:
        store.close()


def test_guard_allows_only_the_declared_read_shapes_for_listed_fans(database):
    store, account, cfg = prepared(database)
    hist().enlist_fan(store, account, 401, priority=0)
    u = urls(cfg)
    a = cfg["provider_account"]
    try:
        for kind in ("chats", "transactions", "chargebacks"):
            guard(store, account)("GET", u[kind], True)
        guard(store, account)("GET", f"{BASE}/api/{a}/chats/401/messages?limit=50&order=asc&last_id=77&skip_users=all", True)
        refused = [
            ("POST", u["chats"]),
            ("GET", u["chats"].replace(a, "acct_other")),
            ("GET", u["chats"].replace("/401/", "/402/")),                       # fan has no cursor: not listed
            ("GET", u["chats"].replace("/401/", "/not-a-number/")),
            ("GET", u["chats"].replace("limit=100", "limit=101")),
            ("GET", u["chats"].replace("limit=100", "limit=0")),
            ("GET", u["chats"] + "&foo=1"),
            ("GET", u["chats"] + "&limit=5"),                                    # duplicate key
            ("GET", u["chats"].replace("order=desc", "order=sideways")),
            ("GET", u["chats"] + "&filter=pinned"),
            ("GET", u["chats"] + "&last_id=abc"),
            ("GET", f"{BASE}/api/{a}/chats/401/media?limit=100"),
            ("GET", f"{BASE}/api/{a}/chats/401"),
            ("GET", u["transactions"].replace("2016-01-01", "2010-01-01")),      # not the configured start date
            ("GET", u["transactions"].replace("limit=100", "limit=101")),
            ("GET", u["transactions"] + "&bogus=1"),
            ("GET", f"{BASE}/api/{a}/transactions/123"),
            ("GET", u["chargebacks"].replace("offset=0", "offset=1000001")),
            ("GET", u["chargebacks"].replace("2016-01-01", "2015-01-01")),
            ("GET", u["chargebacks"] + "&x=1"),
            ("GET", "https://evil.example/api/%s/transactions?limit=100" % a),
            ("GET", u["transactions"] + "#frag"),
        ]
        for method, url in refused:
            with pytest.raises(Refused):
                guard(store, account)(method, url, True)
        with pytest.raises(Refused):                                              # wire must match the qualification
            guard(store, account)("GET", u["transactions"], False)
        used, meter = counters(database, account)
        assert used["chats"] == (2, 4) and used["transactions"] == (1, 2) and used["chargebacks"] == (1, 2)
        assert prep.view(store, TENANT, account)["providerRequestsUsed"] == 4, "refusals reserve nothing"
    finally:
        store.close()


def test_a_history_get_reserves_scope_source_and_meter_counters_together(database):
    store, account, cfg = prepared(database)
    hist().enlist_fan(store, account, 401, priority=0)
    try:
        guard(store, account)("GET", urls(cfg)["chats"], True)
        shown = prep.view(store, TENANT, account)
        assert (shown["providerRequestsUsed"], shown["providerCreditsReserved"]) == (1, 2)
        used, meter = counters(database, account)
        assert used["chats"] == (1, 2) and meter["chats"] == (1, 2)
        assert used["transactions"] == (0, 0) and "transactions" not in meter
        guard(store, account)("GET", urls(cfg)["vault"], True)
        used, meter = counters(database, account)
        assert used["vault_window"] == (1, 2) and meter["vault_window"] == (1, 2)
        assert prep.view(store, TENANT, account)["providerRequestsUsed"] == 2
    finally:
        store.close()


def test_a_spent_source_refuses_while_other_sources_and_the_vault_still_read(database):
    store, account, cfg = prepared(database, chats=(1, 2), transactions=(3, 6))
    hist().enlist_fan(store, account, 401, priority=0)
    u = urls(cfg)
    try:
        guard(store, account)("GET", u["chats"], True)
        with pytest.raises(prep.BudgetExhausted):
            guard(store, account)("GET", u["chats"], True)
        for _ in range(3):
            guard(store, account)("GET", u["transactions"], True)
        with pytest.raises(prep.BudgetExhausted):
            guard(store, account)("GET", u["transactions"], True)
        guard(store, account)("GET", u["vault"], True)
        guard(store, account)("GET", u["chargebacks"], True)
        shown = prep.view(store, TENANT, account)
        assert shown["providerRequestsUsed"] == 6, "refused reads reserved nothing at either level"
        used, _ = counters(database, account)
        assert used["chats"][0] == 1 and used["transactions"][0] == 3
        # the credit sub-budget binds independently of the request sub-budget
        assert isinstance(prep.BudgetExhausted("x"), Refused)
    finally:
        store.close()


def test_credit_sub_budget_binds_even_when_requests_remain(database):
    store, account, cfg = prepared(database, transactions=(10, 3))
    try:
        guard(store, account)("GET", urls(cfg)["transactions"], True)
        with pytest.raises(prep.BudgetExhausted):
            guard(store, account)("GET", urls(cfg)["transactions"], True)       # 2 + 2 > 3
        used, _ = counters(database, account)
        assert used["transactions"] == (1, 2)
    finally:
        store.close()


def test_configure_requires_every_source_budget_and_never_defaults(database):
    from fans_api_adapter import history_import
    store, account, cfg = prepared(database, begin=False)
    base = spec()
    bad = [
        {**base, "sources": {k: v for k, v in base["sources"].items() if k != "chats"}},
        {**base, "sources": {**base["sources"], "extra": {"requests": 1, "credits": 2, "min_interval_seconds": 0}}},
        {**base, "sources": {**base["sources"], "chats": {"requests": 0, "credits": 2, "min_interval_seconds": 0}}},
        {**base, "sources": {**base["sources"], "chats": {"requests": 5, "credits": 1, "min_interval_seconds": 0}}},   # < one request
        {**base, "sources": {**base["sources"], "chats": {"requests": 5, "credits": 10}}},
        {**base, "sources": {**base["sources"], "chats": {"requests": "5", "credits": 10, "min_interval_seconds": 0}}},
        {**base, "sources": {**base["sources"], "chats": {"requests": 5, "credits": 10, "min_interval_seconds": 86401}}},
        {**base, "start_date": "yesterday"},
        {**base, "start_date": "2999-01-01 00:00:00"},
        {**base, "contract_version": "history-import.v0"},
        {**base, "webhooks": {"reference": ""}},
        {k: v for k, v in base.items() if k != "start_date"},
        {**base, "sources": sources(vault=(300, 600), chats=(100, 200))},                                              # 300+100+20+20 > 400 ceiling
    ]
    try:
        for i, candidate in enumerate(bad):
            with pytest.raises(Refused):
                history_import.configure(store, TENANT, account, candidate, expected_revision=0, command_key=key(i + 1), actor=ACTOR, reason="x")
        assert rows(database, models()[0], account) == []
        with pytest.raises(Refused):
            history_import.configure(store, TENANT, account, base, expected_revision=0, command_key="bad", actor=ACTOR, reason="x")
        with pytest.raises(Refused):
            history_import.configure(store, TENANT, account, base, expected_revision=0, command_key=key(50), actor="nobody", reason="x")
        with pytest.raises(Refused):
            history_import.configure(store, TENANT, account, base, expected_revision=0, command_key=key(51), actor=ACTOR, reason=" ")
    finally:
        store.close()


def test_configure_is_idempotent_revision_fenced_and_only_ever_raises_budgets(database):
    from fans_api_adapter import history_import
    store, account, cfg = prepared(database, begin=False)
    try:
        first = history_import.configure(store, TENANT, account, spec(), expected_revision=0, command_key=key(1), actor=ACTOR, reason="Start")
        assert first["replayed"] is False and first["revision"] == 1
        Source, _ = models()
        assert {r.source for r in rows(database, Source, account)} == {"vault_window", "chats", "transactions", "chargebacks"}
        assert {r.state for r in rows(database, Source, account)} == {"not_started"}
        again = history_import.configure(store, TENANT, account, spec(), expected_revision=0, command_key=key(1), actor=ACTOR, reason="Start")
        assert again["replayed"] is True and again["revision"] == 1
        with pytest.raises(Refused):                                               # same key, different request
            history_import.configure(store, TENANT, account, spec(chats=(61, 122)), expected_revision=0, command_key=key(1), actor=ACTOR, reason="Start")
        with pytest.raises(Refused):                                               # stale revision
            history_import.configure(store, TENANT, account, spec(chats=(61, 122)), expected_revision=0, command_key=key(2), actor=ACTOR, reason="Raise")
        raised = history_import.configure(store, TENANT, account, spec(chats=(61, 122)), expected_revision=1, command_key=key(3), actor=ACTOR, reason="Raise")
        assert raised["revision"] == 2
        assert {r.source: r.requests_budget for r in rows(database, Source, account)}["chats"] == 61
        with pytest.raises(Refused):                                               # budgets never shrink
            history_import.configure(store, TENANT, account, spec(), expected_revision=2, command_key=key(4), actor=ACTOR, reason="Lower")
        with pytest.raises(Refused):                                               # start date is part of the evidence
            history_import.configure(store, TENANT, account, spec(chats=(61, 122), start_date="2017-01-01 00:00:00"), expected_revision=2, command_key=key(5), actor=ACTOR, reason="Move")
        with pytest.raises(Refused):                                               # other tenant
            history_import.configure(store, "another-tenant", account, spec(chats=(61, 122)), expected_revision=2, command_key=key(6), actor=ACTOR, reason="x")
    finally:
        store.close()


def test_configure_cannot_cut_a_budget_below_what_is_already_spent(database):
    from fans_api_adapter import history_import
    store, account, cfg = prepared(database)
    hist().enlist_fan(store, account, 401, priority=0)
    try:
        for _ in range(3):
            guard(store, account)("GET", urls(cfg)["chats"], True)
        with pytest.raises(Refused):
            history_import.configure(store, TENANT, account, spec(chats=(2, 4)), expected_revision=1, command_key=key(9), actor=ACTOR, reason="Lower")
    finally:
        store.close()


def test_configure_refuses_without_a_current_preparation_scope_or_when_stopped(database):
    from fans_api_adapter import history_import
    store, account, cfg = setup(database, 1, requests=400, credits=800)
    try:
        prep.stop(store, TENANT, account, expected_revision=1, command_key=key(70), actor=ACTOR, reason="Stop")
        with pytest.raises(Refused):
            history_import.configure(store, TENANT, account, spec(), expected_revision=0, command_key=key(71), actor=ACTOR, reason="Start")
        with pytest.raises(Refused):                                              # an account with no preparation at all
            history_import.configure(store, TENANT, "acc-unknown", spec(), expected_revision=0, command_key=key(72), actor=ACTOR, reason="Start")
        # a stopped preparation fences every history read too
        with pytest.raises(Refused):
            guard(store, account)("GET", urls(cfg)["vault"], True)
    finally:
        store.close()


def test_two_accounts_with_identical_ids_have_separate_budgets_and_meters(database):
    a_store, a, a_cfg = prepared(database, 1)
    b_store, b, b_cfg = prepared(database, 2, chats=(1, 2))
    hist().enlist_fan(a_store, a, 401, priority=0)
    hist().enlist_fan(b_store, b, 401, priority=0)
    try:
        prep.PreparationRequestGuard(b_store, TENANT, b, 1)("GET", urls(b_cfg)["chats"], True)
        with pytest.raises(prep.BudgetExhausted):
            prep.PreparationRequestGuard(b_store, TENANT, b, 1)("GET", urls(b_cfg)["chats"], True)
        for _ in range(5):
            prep.PreparationRequestGuard(a_store, TENANT, a, 1)("GET", urls(a_cfg)["chats"], True)
        assert counters(database, a)[0]["chats"] == (5, 10) and counters(database, b)[0]["chats"] == (1, 2)
        with pytest.raises(Refused):          # one account's guard cannot read the other's provider account path
            prep.PreparationRequestGuard(a_store, TENANT, a, 1)("GET", urls(b_cfg)["chats"], True)
    finally:
        a_store.close(); b_store.close()
