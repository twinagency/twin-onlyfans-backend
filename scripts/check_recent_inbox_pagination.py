from types import SimpleNamespace as N
from datetime import datetime
from onlyfans_runtime import recent_inbox as m
from onlyfans_runtime.provider_read_budget import ReadDeferred
seen=[];calls=[]
m._read=lambda store,tenant,account,revision,callback,consume:consume(callback(),None)
m._missing=lambda store,tenant,account,revision,page,claim:page.fan_ids
m._commit=lambda store,tenant,account,revision,fan,page,claim,history_only:(seen.append(fan) or 1)
class Reader:
 account_id='test';connection_revision=1
 fail=False
 def chats(self,*,cursor,limit):
  calls.append(cursor)
  if self.fail and cursor=='10':raise ReadDeferred(datetime.now())
  return {None:N(fan_ids=[1],next_cursor='10'),'10':N(fan_ids=[2],next_cursor='20'),'20':N(fan_ids=[3],next_cursor=None)}[cursor]
 def messages(self,**kw):return N()
r=Reader();s=N()
assert m.poll_recent(s,'tenant','test',r,connection_revision=1,clock=lambda:0)==2
assert seen==[1,2] and calls==[None,'10']
m.poll_recent(s,'tenant','test',r,connection_revision=1,clock=lambda:1)
assert seen==[1,2,3] and calls[-1]=='20'
s=N();r.fail=True
m.poll_recent(s,'tenant','test',r,connection_revision=1,clock=lambda:0)
assert s._recent_inbox_states[('tenant','test',1)].discovery_cursor=='10'
r.fail=False
m.poll_recent(s,'tenant','test',r,connection_revision=1,clock=lambda:1)
assert s._recent_inbox_states[('tenant','test',1)].discovery_cursor=='20'
m.poll_recent(s,'tenant','test',r,connection_revision=2,clock=lambda:2)
assert ('tenant','test',1) not in s._recent_inbox_states
print('PASS: short pages, continuation, deferred retry, connection replacement')
