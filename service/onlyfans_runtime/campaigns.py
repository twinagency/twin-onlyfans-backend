"""Frozen, bounded campaign audiences feeding the one delivery authority.

Campaigns contain human-authored text. Scheduling is local and durable; no
provider bulk endpoint, extra model call or legacy automation is invoked.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Fan, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_campaign_models import OfCampaign, OfCampaignRecipient, OfCampaignChange
from onlyfans_read.contract import conversation_id_for, fan_id_from_conversation_id
from .store import Refused

MAX_RECIPIENTS = 1000
MAX_CAMPAIGNS = 100
MAX_OFFER_SNAPSHOT_BYTES = 16384
STATUSES = ("pending", "queued", "dispatching", "accepted", "notAccepted", "unknown", "cancelled", "refused", "skipped")


class InvalidCampaign(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def stamp(value):
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def identity(value):
    return type(value) is str and bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value))


def command_identity(command_key, actor):
    if (type(command_key) is not str or not re.fullmatch(r"[a-f0-9]{64}", command_key)
            or type(actor) is not str or not 1 <= len(actor) <= 128):
        raise InvalidCampaign("invalid campaign command identity")


def intent_key(campaign_id, fan_id):
    return hashlib.sha256(f"onlyfans-campaign:{campaign_id}:{fan_id}".encode()).hexdigest()


def _frozen(row):
    return dict(bindingId=row.binding_id, name=row.name, bodyHash=digest(row.body),
        actor=row.actor, audienceKind=row.audience_kind, audienceHash=row.audience_hash,
        recipientCount=row.recipient_count, scheduledAt=stamp(row.scheduled_at),
        catalogItemId=row.catalog_item_id,
        offerSnapshotHash=digest(json.loads(row.catalog_snapshot_json)) if row.catalog_snapshot_json is not None else None)


def _offer_current(s, row):
    if row.catalog_item_id is None:
        return row.catalog_snapshot_json is None
    try:
        from .media_delivery import catalog_terms
        return row.catalog_snapshot_json == canonical(catalog_terms(s, row.account_id, row.catalog_item_id))
    except (Refused, ValueError, TypeError):
        return False


def _integrity(s, row):
    initial = s.get(OfCampaignChange, (row.account_id, row.campaign_id, 1))
    if initial is None or initial.action != "create" or initial.after_json != canonical(_frozen(row)):
        return False
    fans = list(s.scalars(select(OfCampaignRecipient.fan_id).where(
        OfCampaignRecipient.account_id == row.account_id,
        OfCampaignRecipient.campaign_id == row.campaign_id).order_by(OfCampaignRecipient.fan_id).limit(MAX_RECIPIENTS + 1)))
    return len(fans) == row.recipient_count and digest(fans) == row.audience_hash


def validate_prepare(s, runtime, *, fan_id, body, campaign_id, command_key, actor, now, catalog_item_id=None):
    """Called inside TextAuthority's runtime lock, before inserting its intent."""
    if not identity(campaign_id):
        raise Refused("campaign unavailable")
    campaign = s.get(OfCampaign, (runtime.account_id, campaign_id))
    recipient = s.get(OfCampaignRecipient, (runtime.account_id, campaign_id, fan_id))
    if (campaign is None or recipient is None or recipient.status != "pending" or recipient.intent_id is not None
            or campaign.binding_id != runtime.binding_id or campaign.status not in {"scheduled", "running"}
            or campaign.scheduled_at > now or body != campaign.body or actor != campaign.actor
            or catalog_item_id != campaign.catalog_item_id
            or command_key != intent_key(campaign_id, fan_id) or not _integrity(s, campaign) or not _offer_current(s, campaign)):
        raise Refused("campaign snapshot changed")
    if campaign.status == "scheduled":
        campaign.status, campaign.updated_at = "running", now
    return recipient


def attach(s, recipient, intent_id, now):
    """The intent and recipient link commit together; no crash gap or second key."""
    s.flush()
    recipient.intent_id, recipient.status, recipient.reason, recipient.updated_at = intent_id, "queued", None, now


def current(s, row, runtime, now):
    """Final fire-time check, also used immediately before network consume."""
    if row.origin != "campaign":
        return True
    recipient = s.scalar(select(OfCampaignRecipient).where(
        OfCampaignRecipient.account_id == row.account_id, OfCampaignRecipient.intent_id == row.intent_id))
    if recipient is None or recipient.fan_id != row.fan_id or recipient.status != "queued":
        return False
    campaign = s.get(OfCampaign, (row.account_id, recipient.campaign_id))
    return bool(campaign is not None and campaign.status == "running" and campaign.scheduled_at <= now
        and campaign.binding_id == runtime.binding_id == row.binding_id and campaign.body == row.body
        and campaign.actor == row.actor and row.command_key == intent_key(campaign.campaign_id, row.fan_id)
        and campaign.catalog_item_id == row.catalog_item_id
        and campaign.catalog_snapshot_json == row.catalog_snapshot_json
        and _offer_current(s, campaign)
        and _integrity(s, campaign))


def _recipient_status(recipient, intent):
    if intent is not None:
        return "notAccepted" if intent.status == "not_accepted" else intent.status
    return recipient.status


class CampaignService:
    def __init__(self, store):
        self.store, self.engine = store, store.engine

    def create(self, tenant_id, account_id, *, command_key, actor, name, body, audience, scheduled_at=None, catalog_item_id=None):
        command_identity(command_key, actor)
        if (type(name) is not str or not name.strip() or not 1 <= len(name) <= 128 or "\x00" in name
                or type(body) is not str or not body.strip() or "\x00" in body
                or type(audience) is not dict or type(audience.get("kind")) is not str
                or audience.get("kind") not in {"explicit", "all_known"}):
            raise InvalidCampaign("invalid campaign definition")
        try:
            name.encode("utf-8")
            if len(body.encode("utf-8")) > 20000:
                raise InvalidCampaign("campaign text is too large")
        except UnicodeError:
            raise InvalidCampaign("campaign text requires valid UTF-8") from None
        if catalog_item_id is not None and (type(catalog_item_id) is not int or not 0 < catalog_item_id < 2**31):
            raise InvalidCampaign("invalid catalogue item reference")
        kind = audience["kind"]
        explicit = None
        if kind == "all_known":
            if set(audience) != {"kind"}:
                raise InvalidCampaign("invalid all-known audience")
        else:
            raw = audience.get("conversationIds")
            if (set(audience) != {"kind", "conversationIds"} or type(raw) is not list
                    or not 1 <= len(raw) <= MAX_RECIPIENTS or any(type(v) is not str for v in raw)
                    or len(set(raw)) != len(raw)):
                raise InvalidCampaign("invalid explicit audience")
            explicit = []
            for value in raw:
                fan = fan_id_from_conversation_id(value, account_id)
                if fan is None or not 0 < fan < 2**63 or conversation_id_for(account_id, fan) != value:
                    raise InvalidCampaign("invalid conversation reference")
                explicit.append(fan)
            explicit.sort()
        target = None
        if scheduled_at is not None:
            try:
                if type(scheduled_at) is not str or len(scheduled_at) > 64:
                    raise ValueError()
                parsed = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
                target = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            except (ValueError, OverflowError):
                raise InvalidCampaign("schedule requires an explicit timezone") from None
        request = dict(name=name, body=body, audience=audience, scheduledAt=scheduled_at, actor=actor)
        if catalog_item_id is not None:
            request["catalogItemId"] = catalog_item_id
        fingerprint = digest(request)
        from .text_delivery import TextAuthority
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, account_id)
            prior = s.scalar(select(OfCampaign).where(OfCampaign.account_id == account_id,
                                                      OfCampaign.command_key == command_key))
            if prior is not None:
                if prior.binding_id != runtime.binding_id or prior.command_hash != fingerprint:
                    raise Refused("idempotency key conflict")
                return dict(campaignId=prior.campaign_id, recordedRevision=1)
            now = TextAuthority._db_now(s)
            if target is not None and not now - timedelta(seconds=60) <= target <= now + timedelta(days=366):
                raise InvalidCampaign("schedule is outside the supported window")
            target = target or now
            query = select(Fan.fan_id).where(Fan.account_id == account_id)
            if explicit is not None:
                query = query.where(Fan.fan_id.in_(explicit))
            fans = list(s.scalars(query.order_by(Fan.fan_id).limit(MAX_RECIPIENTS + 1)))
            if not fans or len(fans) > MAX_RECIPIENTS:
                raise Refused("campaign audience must contain between 1 and 1000 known fans")
            if any(type(fan) is not int or not 0 < fan < 2**63 for fan in fans):
                raise Refused("campaign audience unavailable")
            if explicit is not None and fans != explicit:
                raise Refused("campaign audience unavailable")
            terms = None
            if catalog_item_id is not None:
                from .media_delivery import catalog_terms
                terms = catalog_terms(s, account_id, catalog_item_id)
                if len(canonical(terms).encode()) > MAX_OFFER_SNAPSHOT_BYTES:
                    raise Refused("catalogue terms exceed campaign snapshot limit")
            row = OfCampaign(account_id=account_id, campaign_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
                command_key=command_key, command_hash=fingerprint, actor=actor, name=name, body=body,
                catalog_item_id=catalog_item_id, catalog_snapshot_json=canonical(terms) if terms is not None else None,
                audience_kind=kind, audience_hash=digest(fans), recipient_count=len(fans), revision=1,
                status="scheduled" if target > now else "running", scheduled_at=target, created_at=now, updated_at=now)
            s.add(row); s.flush()
            s.add_all(OfCampaignRecipient(account_id=account_id, campaign_id=row.campaign_id, fan_id=fan,
                status="pending", updated_at=now) for fan in fans)
            s.add(OfCampaignChange(account_id=account_id, campaign_id=row.campaign_id, revision=1,
                command_key=command_key, command_hash=fingerprint, action="create", actor=actor,
                before_json="{}", after_json=canonical(_frozen(row)), created_at=now))
            return dict(campaignId=row.campaign_id, recordedRevision=1)

    @staticmethod
    def _campaign(s, account_id, campaign_id):
        if not identity(campaign_id):
            raise Refused("campaign unavailable")
        row = s.get(OfCampaign, (account_id, campaign_id))
        if row is None:
            raise Refused("campaign unavailable")
        return row

    def action(self, tenant_id, account_id, campaign_id, *, command_key, actor, expected_revision, action):
        command_identity(command_key, actor)
        if (type(expected_revision) is not int or not 1 <= expected_revision < 2**31 - 1
                or type(action) is not str or action not in {"pause", "resume", "cancel"}):
            raise InvalidCampaign("invalid campaign action")
        fingerprint = digest(dict(campaignId=campaign_id, action=action, expectedRevision=expected_revision, actor=actor))
        from .text_delivery import TextAuthority
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, account_id)
            row = self._campaign(s, account_id, campaign_id)
            if row.binding_id != runtime.binding_id:
                raise Refused("campaign unavailable")
            prior = s.scalar(select(OfCampaignChange).where(OfCampaignChange.account_id == account_id,
                                                           OfCampaignChange.command_key == command_key))
            if prior is not None:
                if prior.command_hash != fingerprint:
                    raise Refused("idempotency key conflict")
                return dict(campaignId=prior.campaign_id, recordedRevision=prior.revision)
            if (row.revision != expected_revision or row.status in {"cancelled", "completed"}
                    or self._summary(s, row)[0]["status"] == "completed"):
                raise Refused("campaign revision conflict")
            now = TextAuthority._db_now(s)
            before = canonical(dict(status=row.status, revision=row.revision))
            row.status = "paused" if action == "pause" else "cancelled" if action == "cancel" else "scheduled" if row.scheduled_at > now else "running"
            row.revision += 1
            row.updated_at = now
            if action == "cancel":
                recipients = s.scalars(select(OfCampaignRecipient).where(OfCampaignRecipient.account_id == account_id,
                    OfCampaignRecipient.campaign_id == campaign_id)).all()
                policy = s.get(OfDeliveryPolicy, account_id)
                for recipient in recipients:
                    if recipient.intent_id is None and recipient.status == "pending":
                        recipient.status, recipient.reason, recipient.updated_at = "cancelled", "campaign_cancelled", now
                    elif recipient.intent_id is not None:
                        intent = s.get(OfTextIntent, (account_id, recipient.intent_id))
                        if intent is not None and intent.status == "queued":
                            if policy is None or policy.queued_count < 1:
                                raise Refused("delivery queue unavailable")
                            intent.status, intent.reason, intent.updated_at = "cancelled", "campaign_cancelled", now
                            policy.queued_count -= 1
                # Dispatching and unknown rows retain their attempted-send holds.
            s.add(OfCampaignChange(account_id=account_id, campaign_id=campaign_id, revision=row.revision,
                command_key=command_key, command_hash=fingerprint, action=action, actor=actor,
                before_json=before, after_json=canonical(dict(status=row.status, revision=row.revision)), created_at=now))
            return dict(campaignId=campaign_id, recordedRevision=row.revision)

    @staticmethod
    def _rows(s, row):
        return s.execute(select(OfCampaignRecipient, OfTextIntent).outerjoin(OfTextIntent,
            (OfCampaignRecipient.account_id == OfTextIntent.account_id) &
            (OfCampaignRecipient.intent_id == OfTextIntent.intent_id)).where(
                OfCampaignRecipient.account_id == row.account_id,
                OfCampaignRecipient.campaign_id == row.campaign_id).order_by(OfCampaignRecipient.fan_id)).all()

    def _summary(self, s, row, *, recipients=False):
        counts = {key: 0 for key in STATUSES}
        details = []
        for recipient, intent in self._rows(s, row):
            status = _recipient_status(recipient, intent)
            counts[status] += 1
            if recipients:
                details.append(dict(conversationId=conversation_id_for(row.account_id, recipient.fan_id),
                    status=status, reason=intent.reason if intent is not None else recipient.reason,
                    intentId=recipient.intent_id,
                    providerMessageId=str(intent.provider_message_id) if intent is not None and intent.provider_message_id is not None else None,
                    updatedAt=stamp(intent.updated_at if intent is not None else recipient.updated_at)))
        # Completion is derived from terminal receipt/skip state. A read never
        # changes the campaign revision or treats unknown as completed.
        status = row.status
        if status in {"running", "scheduled"} and not any(counts[key] for key in ("pending", "queued", "dispatching", "unknown")):
            status = "completed"
        from .campaign_reporting import report, unavailable
        result = dict(campaignId=row.campaign_id, accountId=row.account_id, name=row.name, body=row.body,
            audience=dict(kind=row.audience_kind, count=row.recipient_count, snapshotHash=row.audience_hash),
            scheduledAt=stamp(row.scheduled_at), createdAt=stamp(row.created_at), updatedAt=stamp(row.updated_at),
            status=status, revision=row.revision, counts=counts, recipientCount=row.recipient_count,
            deliveryMode="individual_messages", reporting=report(s, row) if recipients else unavailable())
        terms = json.loads(row.catalog_snapshot_json) if row.catalog_snapshot_json is not None else None
        label = terms["item"]["label"] if terms is not None else None
        result["offer"] = None if terms is None else dict(catalogItemId=row.catalog_item_id,
            label=label[:160] if type(label) is str and label.strip() else f"Catalogue item {row.catalog_item_id}",
            priceCents=terms["price_cents"], currency="USD",
            mediaIds=[str(mid) for mid in terms["media_ids"]], previewIds=[str(mid) for mid in terms["preview_ids"]],
            snapshotHash=digest(terms))
        return result, details

    def list(self, tenant_id, account_id):
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, account_id)
            rows = s.scalars(select(OfCampaign).where(OfCampaign.account_id == account_id,
                OfCampaign.binding_id == runtime.binding_id).order_by(OfCampaign.created_at.desc(), OfCampaign.campaign_id.desc()).limit(MAX_CAMPAIGNS + 1)).all()
            return dict(campaigns=[self._summary(s, row)[0] for row in rows[:MAX_CAMPAIGNS]], truncated=len(rows) > MAX_CAMPAIGNS)

    def detail(self, tenant_id, account_id, campaign_id):
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, account_id)
            row = self._campaign(s, account_id, campaign_id)
            if row.binding_id != runtime.binding_id:
                raise Refused("campaign unavailable")
            summary, recipients = self._summary(s, row, recipients=True)
            return dict(campaign=summary, recipients=recipients)

    def queue_due(self, tenant_id, account_id):
        """At most one intent/account/cycle; all sends use TextAuthority later."""
        from .text_delivery import TextAuthority
        authority = TextAuthority(self.store)
        selected = None
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, account_id)
            now = authority._db_now(s)
            policy = s.get(OfDeliveryPolicy, account_id)
            if (policy is None or not policy.enabled or not runtime.enabled or policy.active_intent_id
                    or policy.queued_count or policy.used_units >= policy.limit_units
                    or policy.next_dispatch_at is not None and policy.next_dispatch_at > now):
                return None
            candidates = s.execute(select(OfCampaign, OfCampaignRecipient).join(OfCampaignRecipient,
                (OfCampaign.account_id == OfCampaignRecipient.account_id) &
                (OfCampaign.campaign_id == OfCampaignRecipient.campaign_id)).where(
                    OfCampaign.account_id == account_id, OfCampaign.binding_id == runtime.binding_id,
                    OfCampaign.status.in_(("scheduled", "running")), OfCampaign.scheduled_at <= now,
                    OfCampaignRecipient.status == "pending", OfCampaignRecipient.intent_id.is_(None))
                .order_by(OfCampaign.scheduled_at, OfCampaign.campaign_id, OfCampaignRecipient.fan_id).limit(32)).all()
            for campaign, recipient in candidates:
                state = s.get(OfConversationState, (account_id, recipient.fan_id))
                fan = s.get(Fan, (account_id, recipient.fan_id))
                if state is None or fan is None or fan.is_muted or state.mode != "automated" or state.takeover_state != "none":
                    recipient.status, recipient.reason, recipient.updated_at = "skipped", "contact_or_owner_unavailable", now
                    continue
                if not _offer_current(s, campaign):
                    recipient.reason, recipient.updated_at = "catalogue_snapshot_changed", now
                    break
                if campaign.catalog_item_id is not None:
                    from .media_delivery import assert_unowned
                    try:
                        assert_unowned(s, account_id, recipient.fan_id, json.loads(campaign.catalog_snapshot_json))
                    except Refused as exc:
                        if str(exc) == "catalogue media already owned":
                            recipient.status, recipient.reason, recipient.updated_at = "skipped", "catalogue_media_already_owned", now
                            continue
                        recipient.reason, recipient.updated_at = "ownership_evidence_unavailable", now
                        break
                selected = dict(campaign_id=campaign.campaign_id, fan_id=recipient.fan_id,
                    expected_revision=state.revision, body=campaign.body, actor=campaign.actor,
                    catalog_item_id=campaign.catalog_item_id)
                break
        if selected is None:
            return None
        fan_id = selected.pop("fan_id")
        try:
            return authority.prepare(tenant_id, account_id, fan_id,
                command_key=intent_key(selected["campaign_id"], fan_id),
                origin="campaign", ttl_seconds=300, **selected)
        except Refused:
            # No intent was committed. Re-read current canonical state next
            # cycle; never create a second command key or infer a wire failure.
            return None
