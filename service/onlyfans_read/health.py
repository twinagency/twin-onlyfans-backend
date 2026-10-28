"""Independent observed health. Reading never manufactures a successful sync."""
from datetime import datetime, timezone
import json, os
from pathlib import Path
from sqlalchemy import select,func
from db.models import Message,OfConversationState
from db.onlyfans_models import OfRuntime,OfIngress,OfRecovery
from db.onlyfans_delivery_models import OfDeliveryPolicy,OfTextIntent
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerScan,OfLedgerHistory
from .contract import iso

def sender_status(account, *, now=None, path=None):
    now=now or datetime.now(timezone.utc).replace(tzinfo=None)
    value={'state':'unknown','observedAt':None,'reason':'Sender heartbeat is unavailable.'}
    try:
        p=Path(path or os.environ.get('FASTT_SUPERVISOR_STATUS','/var/lib/fastt/supervisor-status.json'))
        if p.stat().st_size>65536:return value
        raw=json.loads(p.read_text());at=datetime.fromisoformat(raw['updated_at'].replace('Z','+00:00')).astimezone(timezone.utc).replace(tzinfo=None)
        row=raw['accounts'].get(account);interval=raw['interval_s']
        if type(interval) not in (int,float) or not 5<=interval<=600 or not 0<=(now-at).total_seconds()<=max(90,interval*3):return value
        if not row or row.get('state') not in {'running','held','restarting','stopping'}:return value
        return {'state':row['state'],'observedAt':iso(at),'reason':None if row['state']=='running' else 'The account sender is held or restarting. Check Operations.'}
    except (OSError,ValueError,KeyError,TypeError,AttributeError):return value

async def account_health(s,account):
    runtime=await s.get(OfRuntime,account);policy=await s.get(OfDeliveryPolicy,account)
    connection=await s.get(OfProviderConnection,account);scan=await s.get(OfLedgerScan,account);history=await s.get(OfLedgerHistory,account)
    counts=dict((await s.execute(select(OfIngress.status,func.count()).where(OfIngress.account_id==account,OfIngress.status.in_(('pending','quarantined'))).group_by(OfIngress.status))).all())
    # Use the existing account/status/received_at index and bound heap reads.
    # A global MAX(applied_at) scans every historical payload on large accounts.
    recent=select(OfIngress.applied_at,OfIngress.source).where(OfIngress.account_id==account,
        OfIngress.status=='applied').order_by(OfIngress.received_at.desc(),OfIngress.event_key.desc()).limit(256).subquery()
    processed=await s.scalar(select(func.max(recent.c.applied_at)).where(recent.c.source.in_(('ws','rest','catchup'))))
    incoming=await s.scalar(select(func.max(Message.created_at)).where(Message.account_id==account,Message.direction=='in'))
    recovery=await s.scalar(select(func.count()).select_from(OfRecovery).where(OfRecovery.account_id==account,OfRecovery.status!='complete'))
    unknown=await s.scalar(select(func.count()).select_from(OfTextIntent).where(OfTextIntent.account_id==account,OfTextIntent.status=='unknown'))
    sender=sender_status(account)
    from onlyfans_runtime.launch_operations_models import OfLaunchCommand
    observation=await s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.account_id==account,
        OfLaunchCommand.binding_id==runtime.binding_id,OfLaunchCommand.kind=='provider_profile_observation')
        .order_by(OfLaunchCommand.created_at.desc(),OfLaunchCommand.command_key).limit(1)) if runtime else None
    creator={}
    if observation:
        from onlyfans_runtime.profiles import profile
        # Recorded values have normalized keys; validate their source form again.
        try:
            saved=json.loads(observation.result_json)
            creator=profile({'name':saved.get('name'),'username':saved.get('handle'),'avatar':saved.get('avatar')})
        except (ValueError,TypeError,AttributeError):creator={}
    enabled=bool(runtime and runtime.enabled)
    health={
      'connection':{'lastSuccessAt':iso(connection.verified_at) if connection else None,'reason':None if connection and connection.status=='verified' else 'Connection needs verification.'},
      'incoming':{'lastSuccessAt':iso(processed),'latestMessageAt':iso(incoming),'pending':counts.get('pending',0),'coverage':'Latest 256 applied intake events.','reason':None if processed else 'No recent incoming processing observed in this sample.'},
      'history':{'lastSuccessAt':iso(runtime.last_sync_at) if runtime else None,'pendingConversations':recovery,'quarantined':counts.get('quarantined',0),'reason':'History import requires repair.' if counts.get('quarantined',0) else 'History import is incomplete.' if not runtime or runtime.recovery_needed else None},
      'financial':{'lastSuccessAt':iso(scan.last_complete_at) if scan else None,'fullHistoryAt':iso(history.last_full_at) if history else None,'reason':'Financial sync requires review.' if scan and scan.status=='held' or history and history.status=='held' else 'Financial coverage is incomplete.' if not history or not history.last_full_at else None},
      'sender':sender,
    }
    return {'creatorProfile':{'name':creator.get('name'),'handle':creator.get('handle'),'avatarUrl':creator.get('avatar'),'observedAt':iso(observation.created_at) if observation else None},'runtime':{'enabled':enabled,'sender':sender,'deliveryEnabled':bool(policy and policy.enabled)},'syncHealth':health,
        'needsAttention':bool(counts.get('quarantined',0) or unknown or health['connection']['reason'] or health['history']['reason'] or health['financial']['reason'] or enabled and sender['state']!='running')}
