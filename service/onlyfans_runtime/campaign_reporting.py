"""Read-only campaign measurements from canonical history and exact purchases."""
from datetime import timedelta

from sqlalchemy import and_, case, func, select, cast, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased

from db.models import Message, Transaction
from db.onlyfans_models import OfRuntime
from db.onlyfans_campaign_models import OfCampaignRecipient
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_ledger_models import OfLedgerScan, OfLedgerEvidence
from .store import utcnow


def unavailable(reason="detail_required"):
    return dict(replies=None, replyWindowHours=24, purchases=None, refundedPurchases=None,
        revenueCents=None, currency=None, ledgerAsOf=None, revenueStatus=reason)


def report(session, campaign):
    now = utcnow()
    result = unavailable("ledger_unavailable")
    runtime = session.get(OfRuntime, campaign.account_id)
    sent = aliased(Message)
    incoming = aliased(Message)
    source = (select(OfCampaignRecipient.fan_id, sent.created_at.label("sent_at"),
            OfTextIntent.provider_message_id.label("message_id"))
        .join(OfTextIntent, and_(OfTextIntent.account_id == OfCampaignRecipient.account_id,
            OfTextIntent.intent_id == OfCampaignRecipient.intent_id))
        .join(sent, and_(sent.account_id == OfTextIntent.account_id,
            sent.fan_id == OfTextIntent.fan_id, sent.message_id == OfTextIntent.provider_message_id))
        .where(OfCampaignRecipient.account_id == campaign.account_id,
            OfCampaignRecipient.campaign_id == campaign.campaign_id,
            OfTextIntent.status == "accepted", OfTextIntent.projected_at.is_not(None),
            sent.direction == "out", sent.is_unsent.is_(False)).subquery())
    latest_send = session.scalar(select(func.max(source.c.sent_at)))
    # This measures a response after a send, not proof of causal attribution.
    # History freshness gates zero as well as positive counts.
    if (runtime is not None and not runtime.recovery_needed and runtime.last_sync_at is not None
            and (latest_send is None or runtime.last_sync_at >= latest_send)
            and timedelta(0) <= now - runtime.last_sync_at <= timedelta(minutes=15)):
        result["replies"] = session.scalar(select(func.count(func.distinct(source.c.fan_id)))
            .select_from(source).join(incoming, and_(incoming.account_id == campaign.account_id,
                incoming.fan_id == source.c.fan_id, incoming.direction == "in", incoming.is_unsent.is_(False),
                incoming.created_at > source.c.sent_at,
                incoming.created_at <= source.c.sent_at + timedelta(hours=24),
                incoming.created_at <= runtime.last_sync_at))) or 0
    scan = session.get(OfLedgerScan, campaign.account_id)
    if (scan is None or scan.last_complete_at is None or scan.last_full_at is None
            or latest_send is not None and scan.last_complete_at < latest_send
            or not timedelta(0) <= now - scan.last_complete_at <= timedelta(minutes=15)):
        return result
    if campaign.created_at < scan.last_full_at - timedelta(days=90):
        result["revenueStatus"] = "outside_coverage"
        return result
    from .commerce import held
    if held(session, campaign.account_id):
        result["revenueStatus"] = "ledger_held"
        return result
    linked = and_(Transaction.account_id == campaign.account_id,
        Transaction.fan_id == source.c.fan_id, Transaction.message_id == source.c.message_id,
        Transaction.kind == "ppv_message", Transaction.source == "ledger")
    exact_proof = select(OfLedgerEvidence.evidence_hash).where(
        OfLedgerEvidence.account_id == campaign.account_id,
        OfLedgerEvidence.transaction_id == Transaction.id,
        OfLedgerEvidence.provider_transaction_id == Transaction.provider_transaction_id,
        OfLedgerEvidence.outcome == "applied",
        cast(OfLedgerEvidence.observation_json, JSONB)["message_id"].astext == cast(Transaction.message_id, String)
    ).correlate(Transaction).exists()
    if session.scalar(select(func.count()).select_from(source).join(Transaction, linked)
            .where((~exact_proof) | (Transaction.currency != "USD"))):
        result["revenueStatus"] = "purchase_link_unverified"
        return result
    # A seven-day refresh does not establish that older reversals were checked
    # more recently than the daily full scan. Display that actual cutoff.
    as_of = scan.last_complete_at if campaign.created_at >= scan.window_start else scan.last_full_at
    purchases, refunds, revenue = session.execute(select(
        func.count(case((Transaction.status.in_(("cleared", "pending", "refund_pending")), Transaction.id))),
        func.count(case((Transaction.status.in_(("refunded", "chargedback")), Transaction.id))),
        func.coalesce(func.sum(case((Transaction.status == "cleared", Transaction.amount_cents), else_=0)), 0))
        .select_from(source).join(Transaction, linked).where(exact_proof, Transaction.currency == "USD")).one()
    result.update(purchases=purchases, refundedPurchases=refunds, revenueCents=revenue,
        currency="USD", ledgerAsOf=as_of.isoformat() + "Z", revenueStatus="available")
    return result
