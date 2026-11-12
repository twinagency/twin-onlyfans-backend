"""R1/R2: durable provider retry timing and visible, classified preparation failures.

Real PostgreSQL, mocked wire only. Time is moved by editing the persisted due time,
so the tests prove the due time is durable rather than an in-process timer.
"""
import json
import time
from datetime import timedelta
from email.utils import formatdate
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database, seed
from test_engine_real_worker_pg import answer
from test_real_transport_pg import _item
from test_commercial_preparation_pg import setup, factory, identity, page, config, TENANT, ACTOR
from conversation_engine import preparation as prep
from db.onlyfans_engine_models import OfEnginePilotScope
from db.onlyfans_vault_models import OfVaultSyncJob
from db.schema import check_schema, HEAD, _config
from onlyfans_runtime.store import Store, Refused, utcnow
from fans_api_adapter.protocol import parse_retry_after


class Wire:
    """Scripted provider: identity always as configured, vault pages from a script."""
    def __init__(self, cfg, *script):
        self.cfg, self.script, self.calls, self.pages = cfg, list(script), [], []

    def __call__(self, req):
        self.calls.append(req.url.path)
        if req.url.path == "/api/accounts":
            return identity(self.cfg)
        self.pages.append(str(req.url))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return step()


def limited(headers=None, status=429):
    return lambda: answer(status, {"error": "synthetic"}, headers or {})


def ok_page(*ids, more=False):
    return lambda: page([_item(i) for i in ids], more)


def run(store, account, wire):
    return prep.prepare_one(store, TENANT, account, client_factory=factory(wire))


def job_of(database, account):
    with Session(database[1]) as s:
        job = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account))
        s.expunge(job)
        return job


def make_due(database, account):
    with Session(database[1]) as s, s.begin():
        job = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account))
        job.retry_not_before = utcnow() - timedelta(seconds=1)


def make_record_due(database, account):
    # identity-phase failures keep the authoritative due time on the account record
    with Session(database[1]) as s, s.begin():
        row = s.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.account_id == account))
        record = json.loads(row.last_attempt_json)
        record["retryNotBefore"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.last_attempt_json = json.dumps(record)


def seconds_until(moment):
    return (moment - utcnow()).total_seconds()


def used(store, account):
    state = prep.view(store, TENANT, account)
    return state["providerRequestsUsed"], state["providerCreditsReserved"]


# ---- R1: Retry-After parsing -------------------------------------------------

def test_retry_after_parser_accepts_seconds_and_http_dates_and_clamps():
    assert parse_retry_after("120") == 120
    assert parse_retry_after(" 7 ") == 7
    assert parse_retry_after("0") == 0
    assert parse_retry_after("999999") == 3600
    assert parse_retry_after("-5") == 0
    future = parse_retry_after(formatdate(time.time() + 90, usegmt=True))
    assert 85 <= future <= 91
    assert parse_retry_after(formatdate(time.time() - 500, usegmt=True)) == 0
    assert parse_retry_after(formatdate(time.time() + 10**7, usegmt=True)) == 3600
    for junk in (None, "", "soon", "12.5", "1e3", "Mon, 99 Foo 2026", b"120", 120, "9" * 200):
        assert parse_retry_after(junk) is None


# ---- R1: durable due time gates every provider I/O ---------------------------

def test_retry_after_blocks_identity_page_and_reservation_until_due(database):
    store, account, cfg = setup(database)
    wire = Wire(cfg, limited({"Retry-After": "120", "x-ofapi-trace-id": "trace-429"}))
    try:
        first = run(store, account, wire)
        assert first["state"] == "partial" and first["vault"]["error"] == "vault_read_failed"
        assert len(wire.calls) == 2 and used(store, account) == (2, 4)
        job = job_of(database, account)
        assert job.status == "pending" and job.retry_attempts == 1 and job.page_offset == 0
        assert 115 <= seconds_until(job.retry_not_before) <= 121
        for _ in range(3):
            again = run(store, account, wire)
            assert again["state"] == "delayed" and again["cause"] == "provider_rate_limited"
        assert len(wire.calls) == 2, "no identity or page read before the persisted due time"
        assert used(store, account) == (2, 4), "no reservation before the due time"
        shown = prep.view(store, TENANT, account)
        assert shown["lastAttempt"]["outcome"] == "transient" and shown["lastAttempt"]["retryNotBefore"]
        assert shown["vault"]["retryAttempts"] == 1 and shown["vault"]["retryNotBefore"]
        wire.script[:] = [ok_page(501)]
        make_due(database, account)
        done = run(store, account, wire)
        assert done["state"] == "complete" and len(wire.pages) == 2 and len(wire.calls) == 4
        job = job_of(database, account)
        assert job.retry_attempts == 0 and job.retry_not_before is None
        assert prep.view(store, TENANT, account)["lastAttempt"]["outcome"] == "complete"
    finally:
        store.close()


def test_http_date_retry_after_sets_due_time(database):
    store, account, cfg = setup(database)
    stamp = formatdate(time.time() + 600, usegmt=True)
    wire = Wire(cfg, limited({"Retry-After": stamp}, 503))
    try:
        run(store, account, wire)
        assert 590 <= seconds_until(job_of(database, account).retry_not_before) <= 601
        assert run(store, account, wire)["state"] == "delayed" and len(wire.calls) == 2
    finally:
        store.close()


def test_missing_header_uses_bounded_exponential_backoff_from_thirty_seconds(database):
    store, account, cfg = setup(database)
    wire = Wire(cfg, limited(status=503))
    try:
        seen = []
        for _ in range(4):
            run(store, account, wire)
            job = job_of(database, account)
            seen.append((job.retry_attempts, round(seconds_until(job.retry_not_before) / 10) * 10))
            make_due(database, account)
        assert seen == [(1, 30), (2, 60), (3, 120), (4, 240)]
    finally:
        store.close()


def test_retry_after_is_clamped_to_one_hour(database):
    store, account, cfg = setup(database)
    wire = Wire(cfg, limited({"Retry-After": "86400"}))
    try:
        run(store, account, wire)
        assert 3590 <= seconds_until(job_of(database, account).retry_not_before) <= 3601
    finally:
        store.close()


def test_five_transient_failures_exhaust_the_job_and_stop_polling(database):
    store, account, cfg = setup(database, requests=40, credits=80)
    wire = Wire(cfg, limited(status=503))
    try:
        for i in range(5):
            result = run(store, account, wire)
            if i < 4:
                assert result["vault"]["error"] == "vault_read_failed"
                make_due(database, account)
        assert result["vault"]["error"] == "vault_retry_exhausted"
        job = job_of(database, account)
        assert (job.status, job.last_error, job.retry_attempts) == ("failed", "vault_retry_exhausted", 5)
        spent, calls = used(store, account), len(wire.calls)
        shown = prep.view(store, TENANT, account)
        assert shown["lastAttempt"]["cause"] == "vault_retry_exhausted" and shown["lastAttempt"]["retryNotBefore"] is None
        assert shown["lastAttempt"]["exhausted"] is True and shown["failure"]["cause"] == "vault_retry_exhausted"
        for _ in range(3):
            assert run(store, account, wire)["state"] == "held"
        assert len(wire.calls) == calls and used(store, account) == spent
        # Same semantics as any failed import: only an explicit new sync command resumes it.
        from onlyfans_runtime.vault_management import request_sync
        queued = request_sync(store, TENANT, account, expected_connection_revision=1, command_key="e" * 64, actor=ACTOR)
        wire.script[:] = [ok_page(501)]
        done = run(store, account, wire)
        assert done["state"] == "complete" and done["vault"]["jobId"] == queued["jobId"]
    finally:
        store.close()


def test_restart_sees_the_persisted_due_time(database):
    store, account, cfg = setup(database)
    wire = Wire(cfg, limited({"Retry-After": "300"}))
    run(store, account, wire)
    store.close()
    store = Store(database[0])
    try:
        assert run(store, account, wire)["state"] == "delayed"
        assert len(wire.calls) == 2
        assert prep.view(store, TENANT, account)["lastAttempt"]["retryNotBefore"]
    finally:
        store.close()


def test_stop_and_resume_preserve_cursor_due_time_and_cumulative_reservations(database):
    store, account, cfg = setup(database)
    wire = Wire(cfg, ok_page(*range(900, 876, -1), more=True), limited({"Retry-After": "200"}))
    try:
        assert run(store, account, wire)["state"] == "partial"
        assert run(store, account, wire)["vault"]["error"] == "vault_read_failed"
        before = used(store, account)
        job = job_of(database, account)
        assert before == (4, 8) and job.page_offset == 24 and job.pages_read == 1
        prep.stop(store, TENANT, account, expected_revision=1, command_key="b" * 64, actor=ACTOR, reason="Pause")
        assert run(store, account, wire)["state"] == "stopped"
        prep.configure(store, TENANT, account, cfg, expected_revision=2, command_key="c" * 64, actor=ACTOR, reason="Resume")
        after = job_of(database, account)
        assert used(store, account) == before and after.job_id == job.job_id and after.page_offset == 24
        assert after.retry_not_before == job.retry_not_before and after.retry_attempts == 1
        assert run(store, account, wire)["state"] == "delayed", "resume must not skip an unexpired provider due time"
        wire.script[:] = [ok_page(500)]
        make_due(database, account)
        assert run(store, account, wire)["state"] == "complete"
        assert len(wire.pages) == 3 and "offset=24" in wire.pages[1] and wire.pages[2] == wire.pages[1]
        assert used(store, account) == (6, 12)
    finally:
        store.close()


# ---- R2: classified, persisted, visible failures -----------------------------

def test_identity_401_is_persisted_and_not_polled_until_a_new_operator_command(database):
    store, account, cfg = setup(database)
    calls = []

    def wire(req):
        calls.append(req.url.path)
        return answer(401, {"error": "synthetic_invalid_token", "detail": "Bearer synthetic-preparation"}, {"x-ofapi-trace-id": "t-401"})
    try:
        with pytest.raises(Refused):
            run(store, account, wire)
        shown = prep.view(store, TENANT, account)
        attempt = shown["lastAttempt"]
        assert attempt["outcome"] == "identity_failed" and attempt["cause"] == "provider_auth_rejected"
        assert attempt["retryNotBefore"] is None and attempt["attempts"] == 1 and attempt["operatorAction"]
        assert shown["failure"]["outcome"] == "identity_failed" and shown["connected"] is False
        dumped = json.dumps(shown)
        assert "synthetic-preparation" not in dumped and "synthetic_invalid_token" not in dumped
        assert used(store, account) == (1, 2)
        for _ in range(4):
            held = run(store, account, lambda req: pytest.fail("held preparation must not touch the provider"))
            assert held["state"] == "held" and held["cause"] == "provider_auth_rejected"
        assert used(store, account) == (1, 2) and len(calls) == 1
        # An unchanged-config command from the operator is the explicit new command.
        prep.configure(store, TENANT, account, cfg, expected_revision=1, command_key="d" * 64, actor=ACTOR, reason="Credential replaced")
        assert prep.view(store, TENANT, account)["lastAttempt"] is None
        with pytest.raises(Refused):
            run(store, account, wire)
        assert len(calls) == 2 and used(store, account) == (2, 4)
    finally:
        store.close()


def test_identity_failure_after_successful_pages_is_visible_and_keeps_cursor(database):
    store, account, cfg = setup(database)
    wire = Wire(cfg, ok_page(*range(900, 876, -1), more=True))
    try:
        assert run(store, account, wire)["state"] == "partial"
        assert prep.view(store, TENANT, account)["lastAttempt"]["outcome"] == "partial"
        with pytest.raises(Refused):
            run(store, account, lambda req: answer(403, {"error": "forbidden"}))
        shown = prep.view(store, TENANT, account)
        assert shown["lastAttempt"]["cause"] == "provider_forbidden" and shown["vault"]["pagesRead"] == 1
        assert job_of(database, account).page_offset == 24
    finally:
        store.close()


def test_failure_classes_are_distinct_and_persisted(database, tmp_path):
    outcomes = {}

    def attempt(label, wire_for, i, **limits):
        store, account, cfg = setup(database, i, **limits)
        try:
            try:
                run(store, account, wire_for(cfg))
            except Refused:
                pass
            shown = prep.view(store, TENANT, account)
            outcomes[label] = (shown["lastAttempt"]["outcome"], shown["lastAttempt"]["cause"],
                               shown["lastAttempt"]["retryNotBefore"] is not None)
            return store, account, cfg
        finally:
            store.close()
    attempt("mismatch", lambda cfg: (lambda req: answer(200, [{"id": cfg["provider_account"], "onlyfans_id": "999", "is_authenticated": True}])), 1)
    attempt("account_missing", lambda cfg: (lambda req: identity(config(9))), 11)
    attempt("unauthenticated", lambda cfg: (lambda req: answer(200, [{"id": cfg["provider_account"], "onlyfans_id": cfg["creator_id"], "is_authenticated": False}])), 2)
    attempt("transient_identity", lambda cfg: (lambda req: answer(503, {})), 3)
    attempt("unreachable", lambda cfg: (lambda req: (_ for _ in ()).throw(TimeoutError("synthetic"))), 4)
    attempt("identity_schema", lambda cfg: (lambda req: answer(200, {"not": "a list"})), 5)
    attempt("vault_schema", lambda cfg: Wire(cfg, lambda: answer(200, {"data": {"list": "bad", "hasMore": False}})), 6)
    attempt("vault_auth", lambda cfg: Wire(cfg, limited(status=403)), 7)
    attempt("vault_transient", lambda cfg: Wire(cfg, limited(status=500)), 8)
    attempt("bad_items", lambda cfg: Wire(cfg, lambda: page([{"id": "x"}])), 10)
    assert outcomes["mismatch"] == ("identity_failed", "identity_mismatch", False)
    assert outcomes["account_missing"] == ("identity_failed", "account_not_found", False)
    assert outcomes["unauthenticated"] == ("identity_failed", "account_not_authenticated", False)
    assert outcomes["transient_identity"] == ("transient", "provider_unavailable", True)
    assert outcomes["unreachable"] == ("transient", "provider_unreachable", True)
    assert outcomes["identity_schema"] == ("schema", "provider_response_invalid", False)
    assert outcomes["vault_schema"] == ("schema", "provider_response_invalid", False)
    assert outcomes["vault_auth"] == ("identity_failed", "provider_forbidden", False)
    assert outcomes["vault_transient"] == ("transient", "provider_unavailable", True)
    assert outcomes["bad_items"][0] == "schema"


def test_missing_credential_and_exhausted_budget_are_their_own_outcomes(database, tmp_path):
    from fans_api_adapter import real_transport
    url, engine = database
    try:
        # Budget: two requests allowed, identity + one page uses them while pages remain.
        small_store, small, small_cfg = setup(database, 2, requests=2, credits=4, per=2)
        try:
            wire = Wire(small_cfg, ok_page(*range(900, 876, -1), more=True))
            assert run(small_store, small, wire)["state"] == "partial"
            spent, calls = used(small_store, small), len(wire.calls)
            result = run(small_store, small, wire)
            assert result["state"] == "held" and result["cause"] == "provider_budget_exhausted"
            shown = prep.view(small_store, TENANT, small)
            assert shown["lastAttempt"]["outcome"] == "budget_exhausted" and shown["failure"]["operatorAction"]
            assert used(small_store, small) == spent and len(wire.calls) == calls
        finally:
            small_store.close()
        # Credential: a production-mode account whose approved credential file is absent.
        from test_onlyfans_foundation_pg import seed
        from onlyfans_runtime.text_delivery import TextAuthority
        from db.onlyfans_models import OfRuntime
        prod = seed(url, engine, account="prep-prod", tenant=TENANT)
        with Session(engine) as s, s.begin():
            s.get(OfRuntime, "prep-prod").enabled = False
        TextAuthority(prod).configure(TENANT, "prep-prod", expected_revision=0, enabled=False, media_enabled=False,
            limit_units=0, throttle_seconds=1, transport_kind="fansapi", actor=ACTOR, reason="Preparation only")
        prod_cfg = config(5)
        prod_cfg["qualification"]["mode"] = "production"
        prep.configure(prod, TENANT, "prep-prod", prod_cfg, expected_revision=0, command_key="a" * 64, actor=ACTOR, reason="Production read ceiling")
        registry = tmp_path / "registry.json"
        registry.write_text(json.dumps([{"tenant": TENANT, "account": "prep-prod", "provider_account": prod_cfg["provider_account"],
            "creator_id": prod_cfg["creator_id"], "env_file": str(tmp_path / "missing-credential"), "limits": prod_cfg["limits"]}]))
        registry.chmod(0o600)
        env = {"TWIN_FANSAPI_PREPARATION_REAL": "1", "TWIN_FANSAPI_PREPARATION_REGISTRY": str(registry)}
        first = prep.run_preparation_once(prod, TENANT, env=env)
        assert first == [{"accountId": "prep-prod", "state": "held", "cause": "credential_unavailable"}]
        shown = prep.view(prod, TENANT, "prep-prod")
        assert shown["lastAttempt"]["outcome"] == "credential_missing" and shown["lastAttempt"]["operatorAction"]
        assert shown["providerRequestsUsed"] == 0
        # Held: the loop must not even look for the credential again until commanded.
        boom = lambda *a, **k: pytest.fail("held preparation must not reload credentials")
        orig = real_transport.load_real_config
        real_transport.load_real_config = boom
        try:
            again = prep.run_preparation_once(prod, TENANT, env=env)
        finally:
            real_transport.load_real_config = orig
        assert again[0]["state"] == "held" and again[0]["cause"] == "credential_unavailable"
        prod.close()
    finally:
        pass


def test_one_held_or_delayed_account_does_not_block_another(database):
    a_store, a, a_cfg = setup(database, 1)
    b_store, b, b_cfg = setup(database, 2)
    c_store, c, c_cfg = setup(database, 3)
    try:
        with pytest.raises(Refused):
            run(a_store, a, lambda req: answer(401, {}))
        assert run(b_store, b, Wire(b_cfg, limited({"Retry-After": "600"})))["vault"]["error"] == "vault_read_failed"
        assert run(a_store, a, lambda req: pytest.fail("held"))["state"] == "held"
        assert run(b_store, b, lambda req: pytest.fail("delayed"))["state"] == "delayed"
        assert run(c_store, c, Wire(c_cfg, ok_page(501)))["state"] == "complete"
        assert prep.view(c_store, TENANT, c)["lastAttempt"]["outcome"] == "complete"
        assert prep.view(a_store, TENANT, a)["lastAttempt"]["outcome"] == "identity_failed"
        assert prep.view(b_store, TENANT, b)["lastAttempt"]["outcome"] == "transient"
    finally:
        a_store.close(); b_store.close(); c_store.close()


def test_transient_identity_failure_also_waits_and_exhausts(database):
    store, account, cfg = setup(database)
    wire = lambda req: answer(429, {}, {"Retry-After": "90"})
    try:
        with pytest.raises(Refused):
            run(store, account, wire)
        attempt = prep.view(store, TENANT, account)["lastAttempt"]
        assert attempt["outcome"] == "transient" and attempt["attempts"] == 1
        assert run(store, account, lambda req: pytest.fail("delayed"))["state"] == "delayed"
        assert used(store, account) == (1, 2)
        for expected in (2, 3, 4, 5):
            make_record_due(database, account)
            with pytest.raises(Refused):
                run(store, account, wire)
            assert prep.view(store, TENANT, account)["lastAttempt"]["attempts"] == expected
        exhausted = prep.view(store, TENANT, account)["lastAttempt"]
        assert exhausted["cause"] == "vault_retry_exhausted" and exhausted["exhausted"] and exhausted["retryNotBefore"] is None
        assert run(store, account, lambda req: pytest.fail("exhausted"))["state"] == "held"
    finally:
        store.close()


def test_stop_during_inflight_failure_does_not_overwrite_new_authority(database):
    store, account, cfg = setup(database)

    def wire(req):
        if req.url.path == "/api/accounts":
            prep.stop(store, TENANT, account, expected_revision=1, command_key="b" * 64, actor=ACTOR, reason="Stop in flight")
        return answer(401, {})
    try:
        with pytest.raises(Refused):
            run(store, account, wire)
        shown = prep.view(store, TENANT, account)
        assert shown["status"] == "stopped" and shown["lastAttempt"] is None
    finally:
        store.close()


# ---- schema ------------------------------------------------------------------

def test_attempt_column_does_not_weaken_the_scope_guard_and_0133_is_in_the_chain(database):
    store, account, cfg = setup(database)
    try:
        # Integration head is 0137_model_retry_due_repair (0136 -> 0137); 0133 must stay an ancestor.
        assert HEAD == "0137_model_retry_due_repair"
        from alembic.script import ScriptDirectory
        with database[1].connect() as c:
            assert "0133_preparation_retry_state" in {r.revision for r in ScriptDirectory.from_config(_config(c)).iterate_revisions(HEAD, "base")}
        with database[1].connect() as c:
            check_schema(c)
        prep.PreparationRequestGuard(store, TENANT, account, 1)("GET", "https://api.fansapi.com/api/accounts", True)
        with database[1].begin() as c:
            c.execute(text("UPDATE of_engine_pilot_scope SET last_attempt_json='{}' WHERE account_id=:a"), {"a": account})
        for sql in ("UPDATE of_engine_pilot_scope SET provider_requests_used=0",
                    "UPDATE of_engine_pilot_scope SET revision=revision+1",
                    "UPDATE of_engine_pilot_scope SET status='active',active_since=now(),revision=revision+1"):
            with pytest.raises(Exception):
                with database[1].begin() as c:
                    c.execute(text(sql))
    finally:
        store.close()


def test_retry_state_downgrade_refuses_to_discard_evidence(database):
    from alembic import command
    from db.schema import _config
    store, account, cfg = setup(database)
    try:
        run(store, account, Wire(cfg, limited({"Retry-After": "60"})))
        with pytest.raises(RuntimeError, match="Preserve"):
            with database[1].begin() as c:
                command.downgrade(_config(c), "0132_account_preparation")
        with database[1].connect() as c:
            check_schema(c)
        assert job_of(database, account).retry_attempts == 1
    finally:
        store.close()
