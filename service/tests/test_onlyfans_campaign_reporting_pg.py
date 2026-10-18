"""Campaign observations and exact purchase attribution, owned PostgreSQL only."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_campaign_media_pg import paid
from test_onlyfans_campaigns_pg import create
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT, FAN
from test_onlyfans_ledger_pg import parsed, transaction, record
from db.models import Fan, Message, Transaction
from db.onlyfans_models import OfRuntime
from db.onlyfans_ledger_models import OfLedgerScan
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport


def sent(database):
    store, authority, item, service = paid(database)
    campaign = create(service, catalog_item_id=item)
    identity = service.queue_due(TENANT, ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, SyntheticTextTransport(ACCOUNT)) == "accepted"
    return store, service, campaign["campaignId"]


def fresh(engine, *, account=ACCOUNT):
    now = utcnow()
    with Session(engine) as s, s.begin():
        runtime = s.get(OfRuntime, account)
        runtime.last_sync_at, runtime.recovery_needed = now, False
        scan = s.get(OfLedgerScan, account)
        if scan is None:
            scan = OfLedgerScan(account_id=account, binding_id=runtime.binding_id, owner_id=7001,
                connection_revision=1, window_start=now-timedelta(days=90), window_end=now,
                page_offset=0, status="idle", next_poll_at=now+timedelta(minutes=5), updated_at=now)
            s.add(scan)
        scan.last_complete_at = scan.last_full_at = now


def details(service, campaign):
    return service.detail(TENANT, ACCOUNT, campaign)["campaign"]["reporting"]


def test_missing_or_stale_coverage_is_not_fabricated_as_zero(database):
    store, service, campaign = sent(database)
    result = details(service, campaign)
    assert result["replies"] is None and result["revenueCents"] is None
    assert result["revenueStatus"] == "ledger_unavailable"
    fresh(database[1])
    result = details(service, campaign)
    assert (result["replies"], result["purchases"], result["revenueCents"], result["currency"]) == (0, 0, 0, "USD")
    with Session(database[1]) as s, s.begin():
        s.get(OfLedgerScan, ACCOUNT).last_complete_at = utcnow()-timedelta(minutes=16)
        s.get(OfRuntime, ACCOUNT).recovery_needed = True
    result = details(service, campaign)
    assert result["replies"] is None and result["revenueCents"] is None
    store.close()


def test_response_window_counts_a_recipient_once_and_excludes_other_fans(database, monkeypatch):
    store, service, campaign = sent(database)
    now = utcnow()
    with Session(database[1]) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=999)); s.flush()
        for mid, fan in [(9010, FAN), (9011, FAN), (9012, 999)]:
            s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=mid, direction="in",
                body="Synthetic response", created_at=now, media_count=0))
    fresh(database[1])
    assert details(service, campaign)["replies"] == 1
    with Session(database[1]) as s, s.begin():
        for mid in [9010, 9011]: s.get(Message, (ACCOUNT, FAN, mid)).created_at = now+timedelta(hours=25)
        s.get(OfRuntime, ACCOUNT).last_sync_at = now+timedelta(hours=25)
    # A future/invalid sync timestamp cannot claim complete history.
    assert details(service, campaign)["replies"] is None
    import onlyfans_runtime.campaign_reporting as reporting
    later = now+timedelta(hours=26)
    monkeypatch.setattr(reporting, "utcnow", lambda: later)
    with Session(database[1]) as s, s.begin():
        s.get(OfRuntime, ACCOUNT).last_sync_at = later
    assert details(service, campaign)["replies"] == 0, "a reply after 24 hours does not count"
    store.close()


def test_only_exact_native_evidence_credits_campaign_and_refund_removes_revenue(database, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    store, service, campaign = sent(database)
    event = parsed(transaction())
    event.update(fan_id=FAN, message_id=9001, occurred_at=utcnow().isoformat())
    assert record(store, ACCOUNT, event) == "applied"
    # Same fan's unrelated tip must not become campaign revenue.
    other = {**event, "provider_transaction_id":"unrelated-tip", "message_id":None, "kind":"tip", "amount_cents":5000}
    assert record(store, ACCOUNT, other) == "applied"
    fresh(database[1])
    result = details(service, campaign)
    assert (result["purchases"], result["refundedPurchases"], result["revenueCents"]) == (1, 0, 1000)
    assert result["revenueStatus"] == "available" and result["ledgerAsOf"]
    assert record(store, ACCOUNT, {**event, "status":"refunded"}) == "applied"
    fresh(database[1])
    result = details(service, campaign)
    assert (result["purchases"], result["refundedPurchases"], result["revenueCents"]) == (0, 1, 0)
    store.close()


def test_heuristic_legacy_link_is_not_promoted_to_exact_attribution(database):
    store, service, campaign = sent(database)
    with Session(database[1]) as s, s.begin():
        s.add(Transaction(account_id=ACCOUNT, fan_id=FAN, message_id=9001, kind="ppv_message",
            provider_transaction_id="legacy-heuristic", amount_cents=1000, currency="USD",
            status="cleared", source="ledger", occurred_at=utcnow()))
    fresh(database[1])
    result = details(service, campaign)
    assert result["revenueCents"] is None and result["revenueStatus"] == "purchase_link_unverified"
    store.close()


def test_unlinked_purchase_evidence_holds_campaign_revenue_instead_of_guessing(database, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    store, service, campaign = sent(database)
    assert record(store, ACCOUNT, parsed(transaction())) == "applied"
    fresh(database[1])
    result = details(service, campaign)
    assert result["revenueStatus"] == "ledger_held" and result["purchases"] is None
    store.close()
