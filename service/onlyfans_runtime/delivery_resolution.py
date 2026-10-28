"""Positive delivery reconciliation. Missing evidence never authorizes a resend."""
import os,re
from types import SimpleNamespace
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from db.models import OfConversationState
from db.onlyfans_delivery_models import OfTextIntent
from .store import Refused

def operational():
    return os.environ.get('ONLYFANS_DELIVERY_RESOLUTION_ENABLED')=='1' and bool(os.environ.get('ONLYFANS_CONNECTION_KEY'))

async def resolve(store,tenant,account,fan,*,attempt,provider_message_id,expected_revision,command_key,actor):
    if not operational():raise Refused('delivery resolution unavailable')
    if type(provider_message_id) is not int or provider_message_id<=0:raise Refused('provider evidence required')
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        state=s.get(OfConversationState,(account,fan))
        prior=s.execute(text('SELECT kind FROM of_launch_command WHERE account_id=:account AND command_key=:key'),{'account':account,'key':command_key}).first()
        if not prior and (not state or state.revision!=expected_revision or state.unresolved_attempt_id!=attempt):
            raise Refused('control revision conflict')
        intent=s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id==account,OfTextIntent.fan_id==fan,OfTextIntent.attempt_id==attempt))
        pilot=None;number=None
        if not intent:
            match=re.fullmatch(r'fastt-pilot-([1-9][0-9]*)',attempt)
            if not match or s.bind.dialect.name!='postgresql':raise Refused('delivery evidence unavailable')
            number=int(match[1])
            pilot=s.scalar(text('SELECT p.pilot_id FROM fastt_runtime.native_pilot_receipts r '
                'JOIN fastt_runtime.native_pilots p ON p.pilot_id=r.pilot_id '
                'WHERE p.account_id=:account AND p.fan_id=:fan AND r.attempt_id=:attempt'),{'account':account,'fan':fan,'attempt':number})
            if not pilot:raise Refused('delivery evidence unavailable')
    from native_pilot.ledger import Ledger
    from native_pilot.recovery import command
    args=SimpleNamespace(account=account,tenant=tenant,fan=fan,message_id=provider_message_id,intent_id=intent,
        pilot_id=pilot,attempt_id=number,command_key=command_key,actor=actor,
        reason='Dashboard operator: reconcile exact provider receipt',expected_revision=expected_revision)
    return await command(Ledger(store.engine.url.render_as_string(hide_password=False)),args)
