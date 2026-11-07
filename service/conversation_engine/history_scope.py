"""Only expose history whose current creator binding has source evidence."""
from sqlalchemy import or_, select
from db.models import Message
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_models import OfIngress


def current_binding_message(binding_id):
    ingress = select(OfIngress.event_key).where(OfIngress.account_id == Message.account_id,
        OfIngress.fan_id == Message.fan_id, OfIngress.message_id == Message.message_id,
        OfIngress.binding_id == binding_id, OfIngress.status == 'applied').correlate(Message).exists()
    receipt = select(OfTextIntent.intent_id).where(OfTextIntent.account_id == Message.account_id,
        OfTextIntent.fan_id == Message.fan_id, OfTextIntent.provider_message_id == Message.message_id,
        OfTextIntent.binding_id == binding_id, OfTextIntent.status == 'accepted',
        OfTextIntent.projected_at.is_not(None)).correlate(Message).exists()
    return or_(ingress, receipt)


def has_unscoped_messages(s, account, fan_id, binding_id):
    return s.scalar(select(Message.message_id).where(Message.account_id == account,
        Message.fan_id == fan_id, ~current_binding_message(binding_id)).limit(1)) is not None
