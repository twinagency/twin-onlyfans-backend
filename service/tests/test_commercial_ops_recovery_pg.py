"""Q15 operations on a production-SHAPED, populated, OFFLINE fixture (synthetic rows only; no production data, no provider, no model).

Composition checked here, on one PostgreSQL database that holds: accepted AND unknown text sends, purchase evidence, an emergency-stopped
fleet (release-package fixture), a promoted-and-launched commercial scope with its own unresolved/accepted sends, and the managed-proxy fence.

* backup/restore: pg_dump (PostgreSQL 16 client) -> encrypted archive -> pg_restore into a NEW empty database; every public row equal, the
  restored database quarantined (no release permitted, no authority resurrected), a non-empty target refused;
* guarded rollback: the previous compatible release (R0 baseline) refuses this schema, and the downgrade path refuses to discard evidence,
  leaving the database byte-for-byte unchanged (the positive downgrade on evidence-free historical states is in evidence/q15 harness);
* emergency stop composition: stops sending and cancels only QUEUED work; accepted and unknown sends and purchases are retained untouched;
* receive-only composition: the intake receiver runs on the live database and refuses to start on a quarantined restore.
"""
import importlib.util
import os
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEnginePilotScope
from db.onlyfans_models import OfRuntime
from db.schema import HEAD, SchemaMismatch, _config, check_schema
from onlyfans_runtime import operations
from onlyfans_runtime.launch_operations import account_state, control_account
from onlyfans_runtime.store import utcnow
from test_commercial_preparation_pg import ACTOR, TENANT
from test_commercial_scope_lifecycle_pg import activate, launchable, scope_state
from test_commercial_scope_promote_pg import key
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_recovery_pg import KEY, empty_database, pg_bin, rows  # noqa: F401
from test_onlyfans_release_package_pg import populated, proxy_rows
from test_plan_c_draft_app_pg import signed
from test_engine_real_worker_pg import inbound
from test_plan_c_explicit_intake_pg import environment
from test_plan_c_text_no_offers_pg import text_no_offers
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401  (fixture)
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401  (fixture used by stopped_text)
from test_engine_real_worker_pg import world  # noqa: F401  (fixture used by stopped_text)
from test_onlyfans_text_delivery_pg import ACCOUNT
from db.onlyfans_engine_models import OfFansWebhook
from onlyfans_runtime.store import Refused
from fastapi.testclient import TestClient
from model_transport.pilot_supervised import IntakeOnlyPilot

# The PREVIOUS (live) release reader baked at /opt/r0-baseline: build-candidate.sh BASELINE_COMMIT 659ea29, whose schema HEAD is
# 0129_reconnect_receipt (it predates the 0129b forward repair, so it is behind this lineage and must refuse this schema).
ROLLBACK_READER = Path("/opt/r0-baseline/service")
ROLLBACK_READER_HEAD = "0129_reconnect_receipt"


@pytest.fixture(autouse=True)
def fixture_only(monkeypatch):
    monkeypatch.setenv("TWIN_PLAN_C_FIXTURE_ONLY", "1")


def add_sends(engine, store, account, queued=True):
    """Unknown and accepted sends (and one queued intent) on the commercial account, written after launch."""
    now = utcnow()
    base = dict(account_id=account, fan_id=101, command_key=None, actor=ACTOR, origin="manual", command_hash="1" * 64, payload_hash="2" * 64,
                control_revision=0, input_revision=0, policy_revision=0, created_at=now, updated_at=now, expires_at=now + timedelta(hours=1))
    with Session(engine) as s, s.begin():
        runtime = store._runtime(s, TENANT, account)
        ids = {}
        for n, (status, extra) in enumerate((("unknown", {"deadline": now + timedelta(hours=1)}), ("accepted", {"provider_message_id": 880001}),
                                              ("queued", {}) if queued else ("not_accepted", {}))):
            ids[status] = str(uuid.uuid4())
            s.add(OfTextIntent(**{**base, "intent_id": ids[status], "binding_id": runtime.binding_id, "command_key": f"{n + 1}" * 64,
                                  "body": f"synthetic {status} send", "status": status, **extra}))
        if queued:
            s.get(OfDeliveryPolicy, account).queued_count = 1
    return ids


def intent_rows(engine, account):
    with Session(engine) as s:
        return sorted((i.intent_id, i.status, i.provider_message_id, i.body) for i in
                      s.scalars(select(OfTextIntent).where(OfTextIntent.account_id == account)))


def test_populated_commercial_fleet_emergency_stop_then_backup_restore_is_row_for_row_in_quarantine(database, tmp_path, pg_bin, monkeypatch):
    url, engine = database
    store, other, broker = populated(database, monkeypatch)
    cstore, account, cfg, promoted = launchable(database, 1)
    try:
        live = activate(cstore, account, promoted["revision"], 600)
        assert scope_state(engine, account)["status"] == "active"
        ids = add_sends(engine, cstore, account)
        sends_before = intent_rows(engine, account)
        # ---- emergency stop composition: queued work is cancelled; accepted and unknown sends are never rewritten
        with Session(engine) as s:
            fingerprint = account_state(s, cstore._runtime(s, TENANT, account))["fingerprint"]
        done = control_account(cstore, TENANT, account, action="emergency_stop", command_key=key(610), actor=ACTOR, reason="Q15 emergency stop",
                               expected_fingerprint=fingerprint)
        assert done["recorded"] and done["scopeRevision"] == live["revision"] + 1
        state = scope_state(engine, account)
        assert (state["status"], state["runtime"], state["policy"], state["media"]) == ("stopped", False, False, False)
        by_status = {row[0]: row for row in intent_rows(engine, account)}
        assert by_status[ids["unknown"]][1] == "unknown" and by_status[ids["accepted"]][1] == "accepted"
        assert by_status[ids["queued"]][1] == "cancelled"
        assert sorted(r for r in sends_before if r[0] in (ids["unknown"], ids["accepted"])) == \
            sorted(by_status[k] for k in (ids["unknown"], ids["accepted"])), "accepted and unknown sends are retained untouched"
        # ---- full-scope encrypted backup of everything above (accepted/unknown sends, purchases, scopes, audits, broker fences)
        before, proxy_before = rows(engine), proxy_rows(engine)
        statuses = lambda tables: sorted(r["status"] for r in tables["of_text_intent"])  # noqa: E731
        assert {"accepted", "unknown", "cancelled"} <= set(statuses(before)) and before["of_commerce_evidence"]
        assert any("commercial.v1" in r["config_json"] for r in before["of_engine_pilot_scope"])
        archive = tmp_path / "commercial.twinof"
        metadata = operations.backup(url, archive, key=KEY, pg_bin=pg_bin, include_proxy=True)
        assert metadata["scope"] == "public+onlyfans_proxy" and metadata["schema"] == HEAD
        with empty_database() as (target, restored):
            result = operations.restore(target, archive, key=KEY, pg_bin=pg_bin)
            assert result["releasePermitted"] is False and result["quarantined"] is True
            expected = dict(before)
            expected.pop("alembic_version")
            after = rows(restored)
            quarantine_row = after.pop("alembic_version")
            assert quarantine_row and all(r["version_num"] != HEAD for r in quarantine_row), "restore never writes a releasable revision"
            assert after == expected, [name for name in expected if after.get(name) != expected[name]]
            assert proxy_rows(restored) == proxy_before
            assert statuses(after) == statuses(before)
            assert after["of_commerce_evidence"] == before["of_commerce_evidence"]
            # No authority is resurrected by restore: every runtime/policy is still disabled and the commercial scope is stopped.
            assert all(not r["enabled"] for r in after["of_runtime"])
            assert [(r["enabled"], r["media_enabled"]) for r in after["of_delivery_policy"] if r["account_id"] == account] == [(False, False)]
            assert [r["status"] for r in after["of_engine_pilot_scope"] if "commercial.v1" in r["config_json"]] == ["stopped"]
            with restored.connect() as c, pytest.raises(SchemaMismatch, match="quarantined"):
                check_schema(c)
            # A restore never overwrites: the populated source (or any non-empty target) is refused and unchanged.
            with pytest.raises(operations.RecoveryRefused, match="new empty target"):
                operations.restore(url, archive, key=KEY, pg_bin=pg_bin)
        assert rows(engine) == before and proxy_rows(engine) == proxy_before
        assert intent_rows(engine, account)
    finally:
        cstore.close(); broker.close(); store.close(); other.close()


def test_rollback_is_guarded_the_previous_release_refuses_this_schema_and_the_downgrade_discards_nothing(database, monkeypatch):
    """Old-image restart is NOT a rollback: the R0 baseline reader rejects revision 0138, and the schema downgrade REFUSES (changing
    nothing) while commercial/provider evidence exists. This is the documented guard, asserted with the evidence preserved."""
    import subprocess
    import sys
    url, engine = database
    assert ROLLBACK_READER.is_dir(), "mount the R0 baseline (659ea29) tree at /opt/r0-baseline"
    store, other, broker = populated(database, monkeypatch)
    cstore, account, cfg, promoted = launchable(database, 1)
    try:
        activate(cstore, account, promoted["revision"], 600)
        add_sends(engine, cstore, account)
        before, proxy_before = rows(engine), proxy_rows(engine)
        env = {**os.environ, "PYTHONPATH": str(ROLLBACK_READER), "R0_TEST_DB": url, "DATABASE_URL": url, "ONLYFANS_PROCESS_ROLE": "worker", "TWIN_FASTT_CONFIG_ISOLATED": "1"}
        reader = subprocess.run([sys.executable, "-c", "import os; from sqlalchemy import create_engine; from db.schema import check_schema, HEAD; "
            "print('reader head', HEAD); c=create_engine(os.environ['R0_TEST_DB']).connect(); check_schema(c)"],
            cwd="/tmp", env=env, capture_output=True, text=True, timeout=60)
        assert f"reader head {ROLLBACK_READER_HEAD}" in reader.stdout
        assert reader.returncode != 0 and "Database revision is not supported by this process" in reader.stderr
        with pytest.raises(RuntimeError) as refused:
            with engine.begin() as c:
                command.downgrade(_config(c), "0129b_model_retry_due")
        print("DOWNGRADE REFUSED:", str(refused.value))
        assert rows(engine) == before and proxy_rows(engine) == proxy_before, "a refused downgrade must change nothing"
        with engine.connect() as c:
            check_schema(c)  # still exactly the current release schema
    finally:
        cstore.close(); broker.close(); store.close(); other.close()


def test_positive_compatible_code_rollback_accepts_the_committed_schema_and_changes_nothing(database, tmp_path, monkeypatch):
    """The POSITIVE counterpart of the guarded-rollback test. /opt/compatible-rollback (e79380f) is the previous-release CODE whose reader
    accepts revision 0138, so rolling the code back never needs a schema downgrade (which would discard commercial evidence).
    On the populated, emergency-stopped commercial database: the compatible reader accepts the committed revision and its own schema
    check passes; its API serves the read projections for BOTH the commercial account and the release-package account; its mutation
    surface stays closed; and not one row (public or broker) changes. The current release still passes its own schema check afterwards."""
    import subprocess
    import sys
    import httpx
    from test_onlyfans_http_pg import port, process, wait_http
    legacy = Path(os.environ["ONLYFANS_COMPATIBLE_ROLLBACK_DIR"]).resolve()
    assert (legacy / "service/onlyfans_app.py").is_file()
    url, engine = database
    store, other, broker = populated(database, monkeypatch)
    cstore, account, cfg, promoted = launchable(database, 1)
    try:
        live = activate(cstore, account, promoted["revision"], 600)
        ids = add_sends(engine, cstore, account)
        with Session(engine) as s:
            fingerprint = account_state(s, cstore._runtime(s, TENANT, account))["fingerprint"]
        stopped = control_account(cstore, TENANT, account, action="emergency_stop", command_key=key(611), actor=ACTOR,
                                  reason="Q15 emergency stop before code rollback", expected_fingerprint=fingerprint)
        assert stopped["recorded"] and scope_state(engine, account)["status"] == "stopped"
        before, proxy_before = rows(engine), proxy_rows(engine)
        assert {"accepted", "unknown"} <= {r["status"] for r in before["of_text_intent"]} and before["of_commerce_evidence"]
        # 1. the compatible reader accepts the committed schema (the previous release refused it in the test above)
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "TMP", "TEMP"}}
        env.update(PYTHON_DOTENV_DISABLED="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONPATH=f"{legacy}{os.pathsep}{legacy / "service"}",
                   DATABASE_URL=url, R0_TEST_DB=url, ONLYFANS_PROCESS_ROLE="worker", TWIN_FASTT_CONFIG_ISOLATED="1")
        reader = subprocess.run([sys.executable, "-c", "import os; from sqlalchemy import create_engine; from db.schema import check_schema, HEAD; "
            "print('reader head', HEAD); c=create_engine(os.environ['R0_TEST_DB']).connect(); check_schema(c); print('reader accepts the schema')"],
            cwd=str(legacy), env=env, capture_output=True, text=True, timeout=60)   # its migrations import the repo-root `service` package
        assert reader.returncode == 0, reader.stderr
        assert f"reader head {HEAD}" in reader.stdout and "reader accepts the schema" in reader.stdout
        # 2. its API serves read projections for the commercial account and the release-package account; mutations stay closed
        for tenant, name in ((TENANT, account), ("tenant-a", "acc-a")):
            api_env = {**env, "ONLYFANS_PROCESS_ROLE": "api", "ONLYFANS_API_TOKEN": "synthetic-rollback-read-token", "ONLYFANS_API_TENANT": tenant}
            api_port = port()
            base = f"http://127.0.0.1:{api_port}/onlyfans/api/v1"
            headers = {"Authorization": "Bearer synthetic-rollback-read-token", "X-OnlyFans-Schema-Version": "of-preview.v2"}
            with process([sys.executable, "-m", "uvicorn", "onlyfans_app:app", "--app-dir", "service", "--host", "127.0.0.1", "--port", str(api_port)],
                         cwd=legacy, env=api_env, log=tmp_path / f"rollback-{name}.log") as child:
                wait_http(f"http://127.0.0.1:{api_port}/healthz", child)
                with httpx.Client(trust_env=False, timeout=20) as client:
                    for path in ("/ready", "/accounts", f"/accounts/{name}/operations"):
                        response = client.get(base + path, headers=headers)
                        assert response.status_code == 200, (name, path, response.text)
                    assert client.post(base + f"/accounts/{name}/campaigns", headers=headers, json={}).status_code in (400, 401, 403, 404, 405)
        # 3. nothing changed anywhere, and the current release still accepts its own schema
        assert rows(engine) == before and proxy_rows(engine) == proxy_before
        by_status = {r[0]: r for r in intent_rows(engine, account)}
        assert by_status[ids["unknown"]][1] == "unknown" and by_status[ids["accepted"]][1] == "accepted"
        with engine.connect() as c:
            check_schema(c)
    finally:
        cstore.close(); broker.close(); store.close(); other.close()


def test_emergency_stop_closes_the_wire_for_a_pilot_that_was_built_while_active(stopped_text):
    """Emergency stop AT THE WIRE: the supervised pilot is built and proven live (control: it sends one accepted text), then the
    account is emergency-stopped. The same, already-built pilot is handed a new fan message: no model call and no provider POST
    result, and the runtime stays disabled. The stop is not a restart-time property; it holds for the process that is running."""
    from model_transport.pilot_supervised import build_supervised_pilot
    from test_plan_c_text_activation_pg import start, FAN
    from test_plan_c_supervised_pg import stage
    w, env, model, seen, first = stopped_text
    assert start(w, first["revision"])["status"] == "active"
    pilot = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    stage(pilot, 33001, "hello")
    assert pilot.tick()["dispatched"] == ["accepted"] and len(w.fans.posts) == 1          # control: the live pilot does send
    with Session(w.database[1]) as s:
        fingerprint = account_state(s, w.store._runtime(s, TENANT, ACCOUNT, lock=False))["fingerprint"]
    done = control_account(w.store, TENANT, ACCOUNT, action="emergency_stop", command_key=key(33002), actor=ACTOR,
                           reason="Q15 emergency stop of a running pilot", expected_fingerprint=fingerprint)
    assert done["recorded"]
    stage(pilot, 33003, "are you there?")
    model_calls, posts = len(seen), len(w.fans.posts)
    try:
        pilot.tick()
    except Exception:  # noqa: BLE001 - a refusal is as good as a quiet tick; only a model call or a wire POST is a failure
        pass
    assert len(seen) == model_calls, "an emergency-stopped account must not reach the model"
    assert len(w.fans.posts) == posts == 1, "an emergency-stopped account must not reach the provider wire"
    with Session(w.database[1]) as s:
        assert not s.get(OfRuntime, ACCOUNT).enabled              # the runtime switch is the stopping authority at the wire


# ---------------------------------------------------------------------------------------------- receive-only composition
spec = importlib.util.spec_from_file_location("plan_c_explicit_receiver_q15", Path(__file__).resolve().parents[2] / "ops/plan-c-supervision/receive_only.py")
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


def test_receive_only_runs_on_the_live_database_and_refuses_a_quarantined_restore(stopped_text, tmp_path, pg_bin, monkeypatch):
    """Active no-offers text scope: the receiver entrypoint stores signed events and never builds a model, a sender or the active pilot;
    pointed at a quarantined restore of the same database it refuses before serving anything."""
    w, env, _, seen = text_no_offers(stopped_text)
    environment(monkeypatch, w, env)
    archive = tmp_path / "receive.twinof"
    operations.backup(w.database[0], archive, key=KEY, pg_bin=pg_bin)
    pre = after_count(w.database[1])  # webhook rows at backup time
    served = []
    unsigned, _ = signed(inbound(35001, "hello"), key="q15-unsigned")
    body, headers = signed(inbound(35002, "hello from q15"), key="q15-intake")

    def serve(app, **kw):
        assert type(app.state.pilot) is IntakeOnlyPilot
        with TestClient(app) as client:
            assert client.post("/onlyfans/webhooks/plan-c", content=unsigned).status_code == 401
            assert client.post("/onlyfans/webhooks/plan-c", content=body, headers=headers).status_code == 202
            ready = client.get("/readyz").json()
            assert ready["mode"] == "intake_only" and not ready["sendingReady"]
        served.append(True)

    guards = [patch("model_transport.pilot_supervised.build_supervised_pilot", side_effect=AssertionError("active factory called")),
              patch("model_transport.pilot_supervised.build_engine_service", side_effect=AssertionError("model constructed")),
              patch("model_transport.pilot_supervised.build_real_transport", side_effect=AssertionError("send constructed"))]
    with patch("uvicorn.run", side_effect=serve), guards[0] as active, guards[1] as model, guards[2] as send:
        receiver.main()
        active.assert_not_called(); model.assert_not_called(); send.assert_not_called()
        assert served == [True] and not seen and not w.fans.posts
        with Session(w.database[1]) as s:
            assert s.get(OfFansWebhook, (ACCOUNT, 1, "q15-intake")).raw_body == body
        # The same receiver on a quarantined restore of this database refuses to start: nothing is served, nothing is stored.
        with empty_database() as (target, restored):
            assert operations.restore(target, archive, key=KEY, pg_bin=pg_bin)["releasePermitted"] is False
            monkeypatch.setenv("DATABASE_URL", target)
            with pytest.raises((SchemaMismatch, Refused)):
                receiver.main()
            assert served == [True], "the quarantined restore must not serve"
            assert after_count(restored) == pre and after_count(w.database[1]) == pre + 1, "nothing new reached the quarantined restore"
        assert w.fans.posts == [] and not seen


def after_count(engine):
    with Session(engine) as s:
        return s.scalar(text("SELECT count(*) FROM of_fans_webhook"))
