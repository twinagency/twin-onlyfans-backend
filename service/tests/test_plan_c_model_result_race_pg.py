import pytest
from test_onlyfans_foundation_pg import database
from test_engine_model_pg import with_model,ok,config,_make_request,_reserve,rows
from test_engine_simulated_journey_pg import incoming
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,FAN
from conversation_engine.model_client import Usage,ModelBusy
from onlyfans_runtime.store import Refused

@pytest.mark.parametrize('usage_known',[True,False])
def test_completed_identical_result_between_read_and_reserve_does_not_reserve_again(database,usage_known):
 store,_,engine,runtime,client,_=with_model(database,[ok('Already completed')])
 try:
  incoming(store,1,'hello')
  request=_make_request(database,engine,runtime,client)
  first=_reserve(runtime,database,request,calling=True,worker='worker-a')
  runtime.ledger.settle(ACCOUNT,first.call_id,'worker-a',text_value=ok('Already completed'),usage=Usage(10,10,100) if usage_known else Usage(None,None,None))
  runtime.ledger.set_outcome(ACCOUNT,first.call_id,'ok')
  # Worker B made its cache read before A reserved, but reaches the existing
  # serialized reservation owner after A has already completed the same work.
  with pytest.raises(ModelBusy):
   _reserve(runtime,database,request,worker='stale-reader-b')
  saved=rows(database)
  assert len(saved)==1
  assert saved[0].usage_known is usage_known
  assert runtime.ledger.committed(day=saved[0].cost_day)==(100 if usage_known else saved[0].reserved_microusd)
 finally:store.close()

def test_cached_verified_answer_remains_reusable_at_call_limit(database):
 store,_,engine,runtime,client,_=with_model(database,[ok('Stored answer')],cfg=config(max_calls_per_generation=1))
 try:
  incoming(store,1,'hello')
  original=engine._model_recheck;count=0
  def crash(*args,**kwargs):
   nonlocal count
   count+=1
   if count==2:raise RuntimeError('after paid result before decision')
   return original(*args,**kwargs)
  engine._model_recheck=crash
  with pytest.raises(RuntimeError,match='after paid result'):engine.plan(TENANT,ACCOUNT,FAN,1)
  engine._model_recheck=original
  assert len(client.requests)==1 and rows(database)[0].status=='settled'
  assert engine.plan(TENANT,ACCOUNT,FAN,1)
  assert len(client.requests)==1 and len(rows(database))==1
 finally:store.close()


def test_changed_snapshot_and_prompt_are_not_blocked_by_completed_result(database):
 store,_,engine,runtime,client,_=with_model(database,[ok('Old result')])
 try:
  incoming(store,1,'hello');request=_make_request(database,engine,runtime,client)
  first=_reserve(runtime,database,request,calling=True,worker='worker-a')
  runtime.ledger.settle(ACCOUNT,first.call_id,'worker-a',text_value=ok('Old result'),usage=Usage(10,10,100))
  runtime.ledger.set_outcome(ACCOUNT,first.call_id,'ok')
  old=rows(database)[0]
  second=runtime.ledger.reserve(account=ACCOUNT,request_id=request,generation=0,fan_id=FAN,
   binding_id=old.binding_id,owner_epoch=old.owner_epoch,chain_pos=0,chain_step='primary',model='m/primary',
   prefixed=False,snapshot_sha256='2'*64,prompt_sha256='3'*64,worker_id='changed-context-b',provider='fixture')
  saved=rows(database)
  assert second.call_id!=first.call_id and len(saved)==2
  assert runtime.ledger.committed(day=old.cost_day)==100+saved[1].reserved_microusd
 finally:store.close()

def test_changed_memory_cannot_use_old_cache_or_bypass_exhausted_call_cap(database):
 from test_engine_memory_pg import correct
 store,_,engine,runtime,client,memory=with_model(database,[ok('Stored answer')],cfg=config(max_calls_per_generation=1))
 try:
  incoming(store,1,'hello');original=engine._model_recheck;count=0
  def crash(*args,**kwargs):
   nonlocal count
   count+=1
   if count==2:raise RuntimeError('after paid result')
   return original(*args,**kwargs)
  engine._model_recheck=crash
  with pytest.raises(RuntimeError):engine.plan(TENANT,ACCOUNT,FAN,1)
  engine._model_recheck=original
  correct(memory,value='Brisbane',key='model-cache-cap-correction')
  with pytest.raises(Refused):engine.plan(TENANT,ACCOUNT,FAN,1)
  assert len(client.requests)==1 and len(rows(database))==1
  assert rows(database)[0].actual_microusd==1000
 finally:store.close()


def test_resultless_unknown_created_after_cache_read_cannot_be_blindly_reinvoked(database):
 store,_,engine,runtime,client,_=with_model(database,[ok('Must not be called')])
 try:
  incoming(store,1,'hello')
  reserve=runtime.ledger.reserve
  def peer_completed_before_reserve(**kwargs):
   peer={**kwargs,'worker_id':'peer-before-stale-reader'}
   first=reserve(**peer)
   runtime.ledger.mark_calling(ACCOUNT,first.call_id,peer['worker_id'])
   runtime.ledger.mark_unknown(ACCOUNT,first.call_id,peer['worker_id'],outcome='timeout',error_class='fixture_timeout')
   return reserve(**kwargs)
  runtime.ledger.reserve=peer_completed_before_reserve
  with pytest.raises(ModelBusy):engine.plan(TENANT,ACCOUNT,FAN,1)
  saved=rows(database)
  assert client.requests==[] and len(saved)==1
  assert saved[0].status=='unknown' and not saved[0].usage_known
  assert runtime.ledger.committed(day=saved[0].cost_day)==saved[0].reserved_microusd
 finally:store.close()
