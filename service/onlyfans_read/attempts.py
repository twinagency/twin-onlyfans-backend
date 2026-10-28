"""Read durable attempt state before the transcript receipt projection catches up."""
from datetime import datetime, timedelta, timezone
import re
from sqlalchemy import select, text
from db.onlyfans_delivery_models import OfTextIntent

def outcome(s, account, fan, ref, *, now=None):
    if not ref:return None
    now=now or datetime.now(timezone.utc).replace(tzinfo=None)
    row=None
    if re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",ref):
        row=s.scalar(select(OfTextIntent).where(OfTextIntent.account_id==account,OfTextIntent.fan_id==fan,
        OfTextIntent.attempt_id==ref))
    if row:
        if row.status=='accepted':return 'sent' if row.projected_at else 'accepted_pending'
        if row.status=='dispatching':return 'dispatching' if row.deadline and row.deadline>now else 'unknown'
        if row.status=='not_accepted':return 'failed'
        return 'unknown'
    match=re.fullmatch(r'fastt-pilot-([1-9][0-9]*)',ref)
    if not match or s.bind.dialect.name!='postgresql':return None
    if not s.scalar(text("SELECT to_regclass('fastt_runtime.native_pilot_receipts') IS NOT NULL")):return None
    r=s.execute(text('SELECT r.state,r.provider_message_id,r.created_at FROM fastt_runtime.native_pilot_receipts r '
        'JOIN fastt_runtime.native_pilots p ON p.pilot_id=r.pilot_id '
        'JOIN fastt_runtime.twin_bridge_send_attempts a ON a.id=r.attempt_id '
        'WHERE p.account_id=:account AND p.fan_id=:fan AND a.account_id=:account AND a.fan_id=:fan AND r.attempt_id=:attempt'),
        {'account':account,'fan':fan,'attempt':int(match[1])}).first()
    if not r:return None
    if r[0]=='sent' and r[1]:return 'accepted_pending'
    if r[0]=='not_sent':return 'failed'
    if r[0]=='dispatching':
        try:
            at=datetime.fromisoformat(str(r[2]).replace('Z','+00:00'))
            if at.tzinfo:at=at.astimezone(timezone.utc).replace(tzinfo=None)
            if timedelta(0)<=now-at<=timedelta(seconds=90):return 'dispatching'
        except ValueError:pass
    return 'unknown'
