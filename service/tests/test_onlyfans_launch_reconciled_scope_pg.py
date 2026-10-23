"""Cross-binding and exact-generation proofs for reconciled launch contracts."""
from datetime import timedelta

from sqlalchemy.orm import Session

from test_onlyfans_launch_ledger_api_pg import h, database  # noqa: F401
from test_onlyfans_launch_engagement_pg import rig, schema_template  # noqa: F401
from test_onlyfans_launch_engagement_events_pg import plant, row
from test_onlyfans_launch_ledger_recovery_pg import frozen_scan
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN
from db.models import OfCreatorBinding, Transaction
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerEvidence, OfLedgerScan
from onlyfans_commands.launch_control_api import read_router
from onlyfans_runtime.launch_engagement import events
from onlyfans_runtime.launch_engagement.models import Action
from onlyfans_runtime.store import utcnow


def old_binding(s, account):
    now = utcnow()
    binding = OfCreatorBinding(id="retired-" + account, tenant_id="former-tenant", creator_id="former-creator",
        creator_name="Former creator", account_id=account, handle="former", channel="onlyfans",
        created_at=now - timedelta(days=10), revoked_at=now - timedelta(days=1))
    s.add(binding)
    s.flush()
    return binding


def test_flat_and_rich_ledger_exclude_predecessor_binding_metadata_and_counts(h):
    frozen_scan(h.engine, h.account)
    h.client.app.include_router(read_router)
    with Session(h.engine) as s, s.begin():
        retired = old_binding(s, h.account)
        scan = s.get(OfLedgerScan, h.account)
        scan.binding_id = retired.id
        old = retired.created_at
        s.add(OfLedgerEvidence(account_id=h.account, evidence_hash="e" * 64, provider_transaction_id="former-tx",
            fan_id=101, outcome="conflict", observation_json="{}", recorded_at=old))
        s.add(Transaction(account_id=h.account, fan_id=101, kind="ppv_message", amount_cents=100,
            currency="USD", occurred_at=old, ingested_at=old, provider_transaction_id="former-tx",
            status="cleared", source="ledger"))
    before = h.scan()
    flat = h.read()
    assert flat.status_code == 200
    body = flat.json()
    assert (body["status"], body["scanFingerprint"], body["windowStart"], body["pageOffset"], body["lastError"]) == (
        "not_started", None, None, None, None)
    assert body["conflictCount"] == body["unlinkedPaidCount"] == 0
    from test_onlyfans_launch_ledger_api_pg import AGENCY, READ, headers
    rich = h.client.get(f"{AGENCY}/api/v1/accounts/{h.account}/ledger/evidence", headers={**headers(), **READ})
    assert rich.status_code == 200
    ledger = rich.json()["ledger"]
    assert ledger["status"] == "not_started" and ledger["evidence"] == []
    assert ledger["conflictingEvidence"] == ledger["unresolvedTransactions"] == 0
    assert h.scan() == before and h.calls == []


def test_full_scan_cas_refuses_changed_lease_identity_without_scheduling(h):
    frozen_scan(h.engine, h.account)
    old = utcnow() - timedelta(minutes=1)
    h.push_out(lease_token="11111111-1111-4111-8111-111111111111", lease_until=old)
    body = h.read().json()
    before = h.scan()
    with Session(h.engine) as s, s.begin():
        s.get(OfLedgerScan, h.account).lease_token = "22222222-2222-4222-8222-222222222222"
    response = h.retry({"commandKey": "d" * 64, "expectedConnectionRevision": body["connectionRevision"],
        "expectedScanFingerprint": body["scanFingerprint"]})
    assert response.status_code == 409 and response.json()["code"] == "ledger_state_changed"
    after = h.scan()
    assert h.counts()[0] == 0
    assert {k: v for k, v in after.items() if k != "lease_token"} == {
        k: v for k, v in before.items() if k != "lease_token"}
    assert after["next_poll_at"] > utcnow() and h.calls == []


def test_generation_rotation_invalidates_retry_cas_without_resetting_window(h):
    frozen_scan(h.engine, h.account)
    body, before = h.read().json(), h.scan()
    with Session(h.engine) as s, s.begin():
        s.get(OfProviderConnection, h.account).revision += 1
    response = h.retry({"commandKey": "f" * 64, "expectedConnectionRevision": body["connectionRevision"],
        "expectedScanFingerprint": body["scanFingerprint"]})
    assert response.status_code == 409 and response.json()["code"] == "ledger_state_changed"
    assert h.scan() == before and h.counts()[0] == 0 and h.calls == []


def test_event_hooks_do_not_touch_retired_binding_actions(rig):
    _, engine, item, now = rig
    with Session(engine) as s, s.begin():
        retired = old_binding(s, ACCOUNT)
        retired_id = retired.id
    action = plant(engine, ACCOUNT, FAN, item, anchor=999, binding_id=retired_id)
    with Session(engine) as s, s.begin():
        assert events.cancel_for_fan(s, ACCOUNT, FAN, "reply", now=now) == 0
        assert events.cancel_for_item(s, ACCOUNT, item, now=now) == 0
    retained = row(engine, ACCOUNT, action)
    assert retained.status == "scheduled" and retained.cancel_requested_at is None
