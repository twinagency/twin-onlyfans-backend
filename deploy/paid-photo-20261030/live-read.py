import json, os
from types import SimpleNamespace
from sqlalchemy import create_engine, select, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from db.models import Fan, OfConversationState
from db.onlyfans_models import OfRuntime, OfIngress
from db.onlyfans_delivery_models import OfDeliveryPolicy
from onlyfans_runtime.text_delivery import TextAuthority
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.commerce import manual_held

account='of-323358988';fan_id=318022380
engine=create_engine(make_url(os.environ['DATABASE_URL']).set(drivername='postgresql+psycopg2'))
authority=TextAuthority.__new__(TextAuthority)
try:
 with engine.connect() as c, c.begin():
  c.execute(text('SET TRANSACTION READ ONLY'))
  with Session(bind=c) as s:
   runtime=s.get(OfRuntime,account);policy=s.get(OfDeliveryPolicy,account)
   state=s.get(OfConversationState,(account,fan_id));fan=s.get(Fan,(account,fan_id))
   assert all(x is not None for x in [runtime,policy,state,fan]),'Conversation metadata unavailable'
   quarantined=s.scalar(select(func.count()).select_from(OfIngress).where(OfIngress.account_id==account,OfIngress.status=='quarantined'))
   operator=SimpleNamespace(mode='human_controlled',takeover_state='effective',unresolved_attempt_id=state.unresolved_attempt_id)
   eligible=authority._eligibility(s,runtime,policy,operator,fan,'manual',utcnow(),manual_attachment=True)
   actual=authority._eligibility(s,runtime,policy,state,fan,'manual',utcnow(),manual_attachment=True)
   assert eligible not in ['history_not_current','history_quarantined'],'Manual attachment still blocked by archive'
   print(json.dumps({'account':account,'fan':fan_id,'mode':state.mode,'takeover':state.takeover_state,
       'quarantined_history_retained':quarantined,'delivery_enabled':policy.enabled,'media_enabled':policy.media_enabled,
       'operator_control_guard_result':eligible,'actual_control_guard_result':actual,
       'history_exception_verified':True, 'checked_at':utcnow().isoformat()+'Z', 'used_units':policy.used_units, 'limit_units':policy.limit_units, 'queued':policy.queued_count, 'active_intent':bool(policy.active_intent_id), 'manual_commerce_held':manual_held(s,account,fan_id),'read_only':True,'messages_sent':0}))
finally:
 engine.dispose()

