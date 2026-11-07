"""Fence work whose source webhook later conflicted with different bytes.

The first accepted payload and the conflicting observation are both retained.
Neither is silently chosen: any unsent work derived from the source message is
made ineligible and the request stays visible for review.
"""
import json

from sqlalchemy import and_, select

from db.onlyfans_engine_models import OfFansWebhook, OfFansWebhookConflict


def _identity(raw):
    try:
        body = json.loads(bytes(raw).decode('utf-8'))
        payload = body['payload']
        return int(payload['fromUser']['id']), int(payload['id'])
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def conflicted_sources(s, account):
    """(fan_id, message_id) pairs whose accepted webhook has a conflicting twin."""
    rows = s.scalars(select(OfFansWebhook.raw_body).join(OfFansWebhookConflict, and_(
        OfFansWebhookConflict.account_id == OfFansWebhook.account_id,
        OfFansWebhookConflict.epoch == OfFansWebhook.epoch,
        OfFansWebhookConflict.event_key == OfFansWebhook.event_key)).where(
        OfFansWebhook.account_id == account, OfFansWebhook.status == 'accepted').distinct().limit(1000))
    return {identity for identity in (_identity(raw) for raw in rows) if identity is not None}


def source_conflicted(s, account, fan_id, message_id):
    return (fan_id, message_id) in conflicted_sources(s, account)
