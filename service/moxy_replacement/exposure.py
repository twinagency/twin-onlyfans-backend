"""Canonical per-fan exposure, independent of catalogue identity and paid ownership."""
import json
from sqlalchemy import select
from db.models import Message, VaultSend

def previously_sent_assets(session, account, fan):
    """Retain history from all senders; no arbitrary recent-message cutoff.

    An existing card's preview may be reused as a preview, but is not new paid
    content. Payment ownership remains governed by ownership.py. Unsent rows
    remain evidence of a past send, never evidence of a new purchase.
    """
    found=set();verified=True
    rows=session.execute(select(Message.media_ids).where(
        Message.account_id==account, Message.fan_id==fan,
        Message.direction=='out', Message.media_ids!='[]')).yield_per(500)
    for (raw,) in rows:
        try:
            ids=json.loads(raw)
            if type(ids) is not list or any(type(mid) is not int or mid<=0 for mid in ids):
                raise ValueError()
        except (ValueError,TypeError):
            verified=False
            continue
        found.update(ids)
    found.update(session.scalars(select(VaultSend.media_id).where(
        VaultSend.account_id==account,VaultSend.fan_id==fan)))
    return sorted(found),verified
