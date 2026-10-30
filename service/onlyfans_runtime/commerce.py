"""Synthetic payment-evidence intake into existing canonical commerce owners.

This bounded rehearsal contract has an explicit source revision. It is not an
OnlyFans event parser or a live registration. Payment evidence cannot invent a
message, resolve an unknown send, trigger a model call or send anything.
"""
from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
import os

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from db.models import (ContentOffer, Fan, LadderQuote, LadderState, Message,
    OfConversationState, OfCreatorBinding, Transaction, VaultSend)
from db.onlyfans_delivery_models import OfCommerceEvidence, OfTextIntent
from .native_context import canonical
from .store import Refused, _id
from .text_delivery import TextAuthority, _stamp


@dataclass(frozen=True)
class SyntheticCommerceEvent:
    account_id: str
    fan_id: int
    message_id: int
    event_key: str
    provider_transaction_id: str
    provider_revision: int
    kind: str
    amount_cents: int
    currency: str
    occurred_at: str
    evidence_ref: str
    payout_status: str = "cleared"


def _unlinked_paid(account_id, fan_id=None, *, excluded=True):
    """Paid, unlinked ppv_message ledger rows since the current binding; ``excluded=False`` hides audited exclusions."""
    from db.onlyfans_ledger_models import OfLedgerExclusion
    from db.onlyfans_models import OfRuntime
    from .ledger import PAID
    query = (select(Transaction.id).join(OfRuntime, OfRuntime.account_id == Transaction.account_id)
        .join(OfCreatorBinding, OfCreatorBinding.id == OfRuntime.binding_id).where(
        Transaction.account_id == account_id,
        Transaction.source == "ledger", Transaction.kind == "ppv_message",
        Transaction.message_id.is_(None), Transaction.status.in_(tuple(PAID)),
        Transaction.occurred_at >= OfCreatorBinding.created_at))
    if fan_id is not None:
        query = query.where(Transaction.fan_id == fan_id)
    if not excluded:
        query = query.where(~select(OfLedgerExclusion.id).where(
            OfLedgerExclusion.account_id == Transaction.account_id,
            OfLedgerExclusion.transaction_id == Transaction.id,
            OfLedgerExclusion.binding_id == OfRuntime.binding_id).exists())
    return query.limit(1)


def held(s, account_id, fan_id=None):
    """Commerce hold. Account-wide unless ``fan_id`` is given, which adds that fan's own unlinked paid purchases.

    An audited exclusion (of_ledger_exclusion) removes one unlinked purchase from the account-wide hold only;
    it still holds its own fan, so that fan is never sold or discounted into a purchase we cannot link.
    """
    from db.onlyfans_ledger_models import OfLedgerEvidence, OfLedgerScan
    from .launch_operations_models import OfLedgerResolution
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from .ledger import coverage_proven
    from .store import utcnow
    policy = s.get(OfDeliveryPolicy, account_id)
    scan = s.get(OfLedgerScan, account_id)
    if policy is not None and policy.transport_kind == "native" and policy.media_enabled:
        now = utcnow()
        if scan is None or not coverage_proven(scan) or not timedelta(0) <= now - scan.last_complete_at <= timedelta(minutes=15):
            return True
    return (s.scalar(select(OfCommerceEvidence.evidence_hash).where(
        OfCommerceEvidence.account_id == account_id, OfCommerceEvidence.status == "conflict").limit(1)) is not None
        or s.scalar(select(OfLedgerEvidence.evidence_hash).where(OfLedgerEvidence.account_id == account_id,
            OfLedgerEvidence.outcome == "conflict", ~select(OfLedgerResolution.evidence_hash).join(
                OfRuntime,OfRuntime.account_id==OfLedgerResolution.account_id).where(
                    OfLedgerResolution.account_id==OfLedgerEvidence.account_id,
                    OfLedgerResolution.evidence_hash==OfLedgerEvidence.evidence_hash,
                    OfLedgerResolution.binding_id==OfRuntime.binding_id).exists()).limit(1)) is not None
        # Unlinked purchases from before the current binding was created cannot
        # be sales this binding made, so they stay visible but do not hold it.
        or s.scalar(_unlinked_paid(account_id, excluded=False)) is not None
        or (fan_id is not None and s.scalar(_unlinked_paid(account_id, fan_id)) is not None)
        or s.scalar(select(OfLedgerScan.account_id).where(OfLedgerScan.account_id == account_id,
            ((OfLedgerScan.status == "held") | (OfLedgerScan.last_complete_at.is_(None)))).limit(1)) is not None)


def manual_held(s, account_id, fan_id):
    """Hold operator-selected media only for this fan's unresolved payment evidence.

    A human chooses the files and price. Unfinished account-wide archive coverage
    does not prevent that action; it remains required for automated catalogue
    selling. Known ownership is checked separately at prepare and dispatch.
    """
    from db.onlyfans_ledger_models import OfLedgerEvidence
    from .launch_operations_models import OfLedgerResolution
    from db.onlyfans_models import OfRuntime
    _id(fan_id)
    return (s.scalar(select(OfCommerceEvidence.evidence_hash).where(
        OfCommerceEvidence.account_id == account_id,
        OfCommerceEvidence.fan_id == fan_id,
        OfCommerceEvidence.status == "conflict").limit(1)) is not None
        or s.scalar(select(OfLedgerEvidence.evidence_hash).where(
            OfLedgerEvidence.account_id == account_id,
            OfLedgerEvidence.fan_id == fan_id,
            OfLedgerEvidence.outcome == "conflict",
            ~select(OfLedgerResolution.evidence_hash).join(
                OfRuntime, OfRuntime.account_id == OfLedgerResolution.account_id).where(
                    OfLedgerResolution.account_id == OfLedgerEvidence.account_id,
                    OfLedgerResolution.evidence_hash == OfLedgerEvidence.evidence_hash,
                    OfLedgerResolution.binding_id == OfRuntime.binding_id).exists()).limit(1)) is not None
        or s.scalar(_unlinked_paid(account_id, fan_id)) is not None)



class CommerceIntake:
    def __init__(self, store):
        if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
            raise Refused("commerce intake requires the explicit worker role")
        self.store, self.engine = store, store.engine

    def record(self, tenant_id, account_id, event):
        if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker" or type(event) is not SyntheticCommerceEvent
                or event.account_id != account_id):
            raise Refused("only scoped synthetic commerce evidence is registered")
        _id(event.fan_id); _id(event.message_id)
        if (type(event.provider_revision) is not int or not 0 < event.provider_revision < 2**63
                or type(event.amount_cents) is not int or not 300 <= event.amount_cents <= 20000
                or event.currency != "USD" or type(event.kind) is not str or event.kind not in {"purchase", "refund", "chargeback"}
                or type(event.payout_status) is not str or event.payout_status not in {"cleared", "pending"}
                or any(type(v) is not str or not v.strip() or len(v) > n for v, n in (
                    (event.event_key, 64), (event.provider_transaction_id, 64), (event.evidence_ref, 256)))):
            raise Refused("invalid commerce evidence")
        stamp = _stamp(event.occurred_at)
        encoded = canonical(asdict(event))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with Session(self.engine) as s, s.begin():
            runtime = TextAuthority._historical_runtime(s, tenant_id, account_id)
            now = TextAuthority._db_now(s)
            prior = s.get(OfCommerceEvidence, (account_id, fingerprint))
            if prior is not None:
                return prior.status
            intent = s.scalar(select(OfTextIntent).where(OfTextIntent.account_id == account_id,
                OfTextIntent.fan_id == event.fan_id, OfTextIntent.provider_message_id == event.message_id))
            if (intent is None or intent.status != "accepted" or intent.projected_at is None
                    or intent.catalog_item_id is None or intent.offer_id is None):
                raise Refused("accepted media history required before payment projection")
            binding = s.get(OfCreatorBinding, intent.binding_id)
            if binding is None or binding.tenant_id != tenant_id:
                raise Refused("commerce evidence scope unavailable")
            message = s.scalar(select(Message).where(Message.account_id == account_id,
                Message.fan_id == event.fan_id, Message.message_id == event.message_id).with_for_update())
            offer = s.scalar(select(ContentOffer).where(ContentOffer.account_id == account_id,
                ContentOffer.fan_id == event.fan_id, ContentOffer.id == intent.offer_id).with_for_update())
            if (message is None or offer is None or message.direction != "out" or message.price_cents != intent.price_cents
                    or event.amount_cents != intent.price_cents or stamp < message.created_at
                    or offer.offer_message_id != event.message_id or offer.price_cents != intent.price_cents):
                raise Refused("payment does not match its accepted media terms")
            try:
                payload_conflict = (message.body != intent.body or json.loads(message.media_ids) != json.loads(intent.media_json)
                    or message.media_count != len(json.loads(intent.media_json)))
            except (ValueError, TypeError):
                payload_conflict = True
            duplicate = s.scalar(select(OfCommerceEvidence.evidence_hash).where(OfCommerceEvidence.account_id == account_id,
                or_(OfCommerceEvidence.event_key == event.event_key,
                    (OfCommerceEvidence.provider_transaction_id == event.provider_transaction_id)
                    & (OfCommerceEvidence.provider_revision == event.provider_revision))).limit(1))
            latest = s.scalar(select(OfCommerceEvidence).where(OfCommerceEvidence.account_id == account_id,
                OfCommerceEvidence.provider_transaction_id == event.provider_transaction_id,
                OfCommerceEvidence.status == "applied").order_by(OfCommerceEvidence.provider_revision.desc()).limit(1))
            txs = s.scalars(select(Transaction).where(Transaction.account_id == account_id,
                or_(Transaction.provider_transaction_id == event.provider_transaction_id,
                    (Transaction.fan_id == event.fan_id) & (Transaction.message_id == event.message_id))).with_for_update()).all()
            tx = txs[0] if len(txs) == 1 else None
            status = "applied"
            if (payload_conflict or duplicate is not None or len(txs) > 1 or (tx is not None and
                    (tx.fan_id != event.fan_id or tx.message_id != event.message_id
                     or tx.provider_transaction_id != event.provider_transaction_id
                     or tx.amount_cents != event.amount_cents or tx.currency != "USD" or tx.kind != "ppv_message"))):
                status = "conflict"
            elif tx is not None and tx.status in {"refunded", "chargedback"} and event.kind == "purchase":
                status = "stale" if latest is not None and event.provider_revision < latest.provider_revision else "conflict"
            elif latest is not None and event.provider_revision < latest.provider_revision:
                status = "stale"
            elif latest is not None and (stamp < latest.occurred_at or
                    latest.kind in {"refund", "chargeback"} and event.kind == "purchase"):
                status = "conflict"
            evidence = OfCommerceEvidence(account_id=account_id, evidence_hash=fingerprint,
                event_key=event.event_key, provider_transaction_id=event.provider_transaction_id,
                provider_revision=event.provider_revision, fan_id=event.fan_id, message_id=event.message_id,
                intent_id=intent.intent_id, kind=event.kind, status=status, occurred_at=stamp,
                recorded_at=now, evidence_ref=event.evidence_ref, payload_json=encoded,
                transaction_id=tx.id if tx is not None else None)
            state = s.get(OfConversationState, (account_id, event.fan_id))
            if status == "conflict":
                state.requires_attention = True
                s.add(evidence)
                return status
            if status == "stale":
                s.add(evidence)
                return status
            was_paid = tx is not None and tx.status in {"cleared", "pending"}
            if tx is None:
                tx = Transaction(account_id=account_id, fan_id=event.fan_id, message_id=event.message_id,
                    provider_transaction_id=event.provider_transaction_id, kind="ppv_message",
                    amount_cents=event.amount_cents, currency="USD", occurred_at=stamp, source="synth")
                s.add(tx)
                s.flush()
                evidence.transaction_id = tx.id
            s.add(evidence)
            paid = event.kind == "purchase"
            tx.status = event.payout_status if paid else "refunded" if event.kind == "refund" else "chargedback"
            tx.raw_json, tx.cleared_at = encoded, stamp if paid and event.payout_status == "cleared" else None
            fan = s.scalar(select(Fan).where(Fan.account_id == account_id, Fan.fan_id == event.fan_id).with_for_update())
            if paid != was_paid:
                fan.lifetime_spend_cents = max(0, fan.lifetime_spend_cents + (event.amount_cents if paid else -event.amount_cents))
            settle_media_purchase(s, account_id=account_id, fan_id=event.fan_id,
                message=message, offer=offer, paid=paid, was_paid=was_paid, stamp=stamp, now=now)
            s.flush()
            from .launch_engagement.events import cancel_for_fan
            cancel_for_fan(s, account_id, event.fan_id, "purchase", now=now)
            # Monetary context moving makes queued drafts obsolete through the
            # same native fact fingerprint. No claim, model or send is created.
            return status


def settle_media_purchase(s, *, account_id, fan_id, message, offer, paid, was_paid, stamp, now):
    """Shared canonical media effects, after the caller validates payment evidence.

    The caller owns the account lock, transaction and financial delta. This
    function creates no money, message, plan or provider operation.
    """
    if paid and (message.purchased_at is None or message.is_paid is not True):
        message.purchased_at = stamp
    message.is_paid = paid
    purchase_stamp = message.purchased_at or stamp
    # Only this purchase's exact sent rows change. Another bought copy
    # of the same file remains independent evidence of ownership.
    sends = s.scalars(select(VaultSend).where(VaultSend.account_id == account_id,
        VaultSend.fan_id == fan_id, VaultSend.message_id == message.message_id).with_for_update()).all()
    for sent in sends:
        sent.was_purchased = paid
    if offer is not None:
        offer.status, offer.resolved_by = ("delivered", "ppv_ledger") if paid else ("cancelled", None)
        offer.resolved_at, offer.updated_at = purchase_stamp if paid else stamp, now
        if paid:
            offer.delivery_message_id = message.message_id
    quotes = s.scalars(select(LadderQuote).where(LadderQuote.account_id == account_id,
        LadderQuote.fan_id == fan_id, LadderQuote.message_id == message.message_id).with_for_update()).all()
    for quote in quotes:
        quote.paid, quote.paid_at = paid, purchase_stamp if paid else None
    from automations import upsell
    lad = s.get(LadderState, (account_id, fan_id))
    if lad is None:
        lad = LadderState(account_id=account_id, fan_id=fan_id)
        s.add(lad)
    if paid and not was_paid:
        lad.last_paid_at = max(lad.last_paid_at or purchase_stamp, purchase_stamp)
        if lad.status not in {upsell.STATUS_STOPPED, upsell.STATUS_COMPANION}:
            lad.status = upsell.STATUS_HOT
            lad.unpaid_rungs = 0
    elif not paid:
        lad.status = upsell.STATUS_STOPPED
        state = s.get(OfConversationState, (account_id, fan_id))
        if state is not None:
            state.requires_attention = True
    lad.updated_at = now
