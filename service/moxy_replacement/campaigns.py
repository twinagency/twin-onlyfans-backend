"""Campaign decisions retain their original audience and receipt owner."""
import json
from sqlalchemy import select
from db.onlyfans_campaign_models import OfCampaign, OfCampaignRecipient
from db.onlyfans_moxy_models import MoxyEvent
from onlyfans_runtime.store import Refused, utcnow


def context(s, account, fan, payload):
    identity = payload.get('campaign_id')
    if not identity:
        return None
    from onlyfans_runtime.campaigns import _integrity, _offer_current
    row = s.get(OfCampaign, (account, identity))
    recipient = s.get(OfCampaignRecipient, (account, identity, fan))
    if row is None or recipient is None:
        return {'available': False}
    return {
        'id': identity, 'revision': row.revision, 'binding_id': row.binding_id,
        'available': row.status in {'scheduled', 'running'} and row.scheduled_at <= utcnow()
            and recipient.status in {'pending', 'queued'} and _integrity(s, row) and _offer_current(s, row),
        'instructions': row.body, 'name': row.name, 'catalogue_item_id': row.catalog_item_id,
        'terms': json.loads(row.catalog_snapshot_json) if row.catalog_snapshot_json else None,
    }


def enqueue_recipient(s, runtime, campaign, recipient):
    from .runtime import enqueue
    # Repeated scheduler ticks use the same event. An explicit campaign revision
    # change invalidates old drafts and gives resumed pending work a new identity.
    payload = {'campaign_id': campaign.campaign_id, 'campaign_revision': campaign.revision}
    identity = enqueue(s, runtime.account_id, recipient.fan_id, 'campaign',
        f'campaign:{campaign.campaign_id}:{recipient.fan_id}:{campaign.revision}', payload)
    event = s.get(MoxyEvent, (runtime.account_id, identity))
    if event.status in {'held', 'stale', 'done'}:
        recipient.status, recipient.reason = 'skipped', event.reason or 'moxy_no_send_decision'
    else:
        recipient.reason = 'moxy_decision_pending'
    recipient.updated_at = utcnow()
    return identity


def validate(s, account, fan, event, *, intent_id=None):
    payload = json.loads(event.payload_json)
    value = context(s, account, fan, payload)
    if value is None:
        return None
    recipient = s.get(OfCampaignRecipient, (account, value.get('id'), fan))
    if (not value['available'] or value['revision'] != payload.get('campaign_revision')
            or recipient is None or recipient.intent_id != intent_id):
        raise Refused('moxy_campaign_changed')
    return recipient


def attach(s, event, identity):
    recipient = validate(s, event.account_id, event.fan_id, event)
    if recipient is None:
        return
    from onlyfans_runtime.campaigns import attach as attach_recipient
    row = s.get(OfCampaign, (event.account_id, recipient.campaign_id))
    row.status = 'running'
    row.updated_at = utcnow()
    attach_recipient(s, recipient, identity, utcnow())


def retire(s, event):
    """A no-send decision has a visible, terminal recipient reason."""
    payload = json.loads(event.payload_json)
    if not payload.get('campaign_id'):
        return
    row = s.get(OfCampaignRecipient, (event.account_id, payload['campaign_id'], event.fan_id))
    if row is not None and row.status == 'pending' and row.intent_id is None:
        row.status, row.reason, row.updated_at = 'skipped', event.reason or 'moxy_no_send_decision', utcnow()
