import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from automations.contextual_evidence import evidence,recent_context
from automations import content_prompts as cp

def allow_hook(monkeypatch,payload):
 monkeypatch.setattr(cp,"_thread_lines",AsyncMock(return_value=["FAN: hello"]))
 monkeypatch.setattr(cp,"_sold_lines",AsyncMock(return_value=[]))
 monkeypatch.setattr(cp,"vault_lexicon",AsyncMock(return_value=["portrait","mirror"]))
 monkeypatch.setattr(cp,"resolve_model",AsyncMock(return_value="test"))
 monkeypatch.setattr(cp.llm_client,"effort_options",lambda _:[])
 monkeypatch.setattr(cp._hook_upsell,"effort_for",lambda *a:None)
 monkeypatch.setattr(cp,"runtime_chat",AsyncMock(return_value=NS(parsed=payload)))

def context(text="That looks interesting, tell me more"):
 return {"id":3,"text":text,"messages":[{"id":1,"role":"fan","text":"hello"},{"id":2,"role":"creator","text":"Interested in a clip?"},{"id":3,"role":"fan","text":text}]}
def decision(c,action="send_ppv"):
 return {"hook":True,"action":action,"source":"situation","reason":"Fan is actively interested",
 "supporting_message_ids":[3],"evidence":[{"id":3,"quote":c["text"]}],
 "bridge":"what kind do you like?","what":"portrait","subject":"portrait","terms":["portrait","mirror"],"exclusions":[]}
def test_fan_evidence():
 c=context();assert evidence(decision(c),c)==(3,)
@pytest.mark.parametrize("change",["creator","stale","invented","unknown","bad_action"])
def test_reject_unsupported(change):
 c=context();p=decision(c)
 if change=="creator":p.update(supporting_message_ids=[2],evidence=[{"id":2,"quote":"Interested in a clip?"}])
 if change=="stale":p.update(supporting_message_ids=[1],evidence=[{"id":1,"quote":"hello"}])
 if change=="invented":p["evidence"][0]["quote"]="not in this conversation"
 if change=="unknown":p["supporting_message_ids"]=[999]
 if change=="bad_action":p["action"]="send_now"
 assert evidence(p,c) is None
def test_bounded_context():
 c=NS(messages=[("in","hello")]*30,msg_ids=list(range(1,31)),last_in_mid=30,last_in_text="hello")
 r=recent_context(c,NS());assert len(r["messages"])==20;assert r["messages"][0]["id"]==11
def test_stale_context():
 c=NS(messages=[("out","hello")],msg_ids=[1],last_in_mid=1,last_in_text="hello")
 assert recent_context(c,NS()) is None

@pytest.mark.parametrize("action",["chat","explore","send_ppv"])
def test_hook_actions(monkeypatch,action):
 c=context();p=decision(c,action);p["hook"]=action!="chat"
 monkeypatch.setattr(cp,"_thread_lines",AsyncMock(return_value=["FAN: hello"]))
 monkeypatch.setattr(cp,"_sold_lines",AsyncMock(return_value=[]))
 monkeypatch.setattr(cp,"vault_lexicon",AsyncMock(return_value=["portrait","mirror"]))
 monkeypatch.setattr(cp,"resolve_model",AsyncMock(return_value="test"))
 monkeypatch.setattr(cp.llm_client,"effort_options",lambda _:[])
 monkeypatch.setattr(cp._hook_upsell,"effort_for",lambda *a:None)
 call=AsyncMock(return_value=NS(parsed=p))
 monkeypatch.setattr(cp,"runtime_chat",call)
 hook=asyncio.run(cp.read_hook("of-test",1,contextual_message=c))
 if action=="chat":assert hook is None
 else:assert hook.action==action and hook.supporting_message_ids==(3,)
 assert "Explicit buying words are NOT required" in call.call_args.kwargs["messages"][0]["content"]
 assert "Only kind 2 SITUATION" not in call.call_args.kwargs["messages"][1]["content"]

def test_contextual_explore_allows_media_format_clarification(monkeypatch):
 c=context();p=decision(c,"explore");p["bridge"]="want a photo in blue?"
 allow_hook(monkeypatch,p)
 hook=asyncio.run(cp.read_hook("of-test",1,contextual_message=c))
 assert hook is not None and hook.action=="explore" and hook.bridge=="want a photo in blue?"


def test_contextual_explore_rejects_future_delivery_promise(monkeypatch):
    c=context();p=decision(c,"explore");p["bridge"]="I will send you a video tonight, okay?"
    allow_hook(monkeypatch,p)
    seen=[]
    hook=asyncio.run(cp.read_hook("of-test",1,contextual_message=c,on_reject=seen.append))
    assert hook is None and seen==["invalid_contextual_wording"]


def test_existing_caption_and_contextual_send_still_reject_media_words():
 from automations import _hook_upsell
 assert not _hook_upsell.bridge_ok("want a photo in blue?")
 assert not _hook_upsell.contextual_bridge_ok("want a photo in blue?","send_ppv")


@pytest.mark.parametrize("bridge",[
 "want 3 photos?",
 "want two videos?",
 "want a cheaper photo?",
 "sending you a photo",
 "I will send you a video tonight, okay?",
 "I'll send it later",
 "I'm sending it tonight",
 "you'll get it tomorrow",
 "send you one soon",
])
def test_contextual_explore_keeps_claim_and_delivery_guards(bridge):
 from automations import _hook_upsell
 assert not _hook_upsell.contextual_bridge_ok(bridge,"explore")


@pytest.mark.parametrize("line",[
 "I will send you a video tonight, okay?",
 "I'll send it later",
 "I'm sending it tonight",
 "you'll get it tomorrow",
 "send you one soon",
])
def test_shared_caption_validator_rejects_delivery_promises(line):
 from automations.pack_claim import compose_caption, voice_line_ok, Claim
 assert not voice_line_ok(line)
 assert compose_caption(Claim("3 pics",3),line)=="3 pics"


@pytest.mark.parametrize("line",[
 "I will send you a video tonight, okay?",
 "I\u2019ll deliver it tomorrow",
 "I'm going to send you one later",
 "I am sending it now",
 "you will receive it soon",
 "it'll be in your inbox tonight",
])
def test_final_text_boundary_rejects_future_delivery_promises(line):
 from native_pilot.media_claims import claims_media_sent
 assert claims_media_sent(line)


@pytest.mark.parametrize("line",[
 "I will get some sleep soon",
 "I will have coffee tomorrow",
 "I can share how my day went",
 "I'm going to bed soon",
 "I'm going to the gym later",
 "I'll send my love tomorrow",
 "you will have a great day",
 "I will think about it tonight",
 "I can share more about my day later",
])
def test_ordinary_replies_are_not_delivery_promises(line):
 from automations.pack_claim import has_delivery_promise
 from native_pilot.media_claims import claims_media_sent
 assert not has_delivery_promise(line)
 assert not claims_media_sent(line)


@pytest.mark.parametrize("line",[
 "I will send you a video tonight, okay?",
 "I'll send you a spicy pic later",
 "ill drop the vid tomorrow",
 "I can send you a pic",
 "you will get a video tonight",
 "you'll get it soon",
])
def test_delivery_promises_with_media_are_still_blocked(line):
 from automations.pack_claim import has_delivery_promise
 from native_pilot.media_claims import claims_media_sent
 assert has_delivery_promise(line)
 assert claims_media_sent(line)


@pytest.mark.parametrize("line,promise,media",[
 ("want me to send you a photo?",False,False),
 ("do you want a video tonight?",False,False),
 ("i sent you a video",False,True),
 ("i'm sending you a video now",True,True),
])
def test_delivery_promise_guard_keeps_questions_and_past_claims_distinct(line,promise,media):
 from automations.pack_claim import has_delivery_promise
 from native_pilot.media_claims import claims_media_sent
 assert has_delivery_promise(line) is promise
 assert claims_media_sent(line) is media


@pytest.mark.parametrize("change,expected",[
 ("no_interest","no_interest"),
 ("invalid_output","invalid_model_output"),
 ("unsupported_evidence","unsupported_conversation_evidence"),
 ("invalid_wording","invalid_contextual_wording"),
])
def test_contextual_rejection_codes_are_safe_and_specific(monkeypatch,change,expected):
 c=context();p=decision(c,"explore")
 if change=="no_interest":
  p={"hook":False,"action":"chat"}
 elif change=="invalid_output":
  p={}
 elif change=="unsupported_evidence":
  p.update(supporting_message_ids=[2],evidence=[{"id":2,"quote":"Interested in a clip?"}])
 elif change=="invalid_wording":
  p["bridge"]="want 3 photos?"
 allow_hook(monkeypatch,p)
 seen=[]
 hook=asyncio.run(cp.read_hook("of-test",1,contextual_message=c,on_reject=seen.append))
 assert hook is None and seen==[expected]


def test_provider_failure(monkeypatch):
 monkeypatch.setattr(cp,"_thread_lines",AsyncMock(return_value=["FAN: hi"]))
 monkeypatch.setattr(cp,"_sold_lines",AsyncMock(return_value=[]))
 monkeypatch.setattr(cp,"vault_lexicon",AsyncMock(return_value=[]))
 monkeypatch.setattr(cp,"resolve_model",AsyncMock(return_value="test"))
 monkeypatch.setattr(cp.llm_client,"effort_options",lambda _:[])
 monkeypatch.setattr(cp._hook_upsell,"effort_for",lambda *a:None)
 monkeypatch.setattr(cp,"runtime_chat",AsyncMock(side_effect=RuntimeError("unavailable")))
 seen=[]
 assert asyncio.run(cp.read_hook("of-test",1,contextual_message=context(),on_reject=seen.append)) is None
 assert seen==["model_provider_failure"]

from contextlib import asynccontextmanager
from automations import contextual_offers as co, content_resolver as cr, pack_sender as ps
from automations.content_contract import Contract

@pytest.mark.parametrize("case",["eligible","blocked","takeover","stale","ignored","bought","missing","explore","no_interest","duplicate","disabled","provider"])
def test_planner_preserves_gates(monkeypatch,case):
 c=NS(messages=[("in","hello"),("out","hi"),("in","I would love something like that")],
      msg_ids=[1,2,3],last_in_mid=3,last_in_text="I would love something like that",last_in_at=None)
 fan=NS(fan_id=1)
 events=[]
 def check(mode):
  if case=="stale":
   from native_pilot.ledger import Refused
   raise Refused("contextual-purchase-state-stale")
  if case in ("takeover","ignored","duplicate"):
   from native_pilot.ledger import Refused
   raise Refused(case)
  return {}
 authority=NS(source_message_id=3,check=check,emit=events.append,reply_override=None)
 lane=NS(account_id="of-test",cfg={"fastt_contextual_sales_mode":"off" if case=="disabled" else "live"},
         on=True,may_sell=AsyncMock(return_value=True),plan=AsyncMock(return_value=NS(delivery="plan")))
 hook=cp.Hook("situation","portrait","what kind do you like?",Contract(terms=["portrait"]),
              action="explore" if case=="explore" else "send_ppv",reason="active_interest",supporting_message_ids=(3,))
 def no_interest(*args,**kwargs):
  kwargs["on_reject"]("no_interest")
  return None
 if case=="provider":read=AsyncMock(side_effect=RuntimeError("unavailable"))
 elif case=="no_interest":read=AsyncMock(side_effect=no_interest)
 else:read=AsyncMock(return_value=hook)
 monkeypatch.setattr(cp,"read_hook",read)
 @asynccontextmanager
 async def session():
  items=[] if case=="missing" else [NS(media_id=12,account_id="of-test",removed_at=None,content_hash="v1",kind="photo",
        description="portrait in soft window light",default_price_cents=1500)]
  yield NS(execute=AsyncMock(return_value=NS(first=lambda:None if case=="missing" else (10,),scalars=lambda:NS(all=lambda:items))))
 monkeypatch.setattr(co,"get_session",session)
 monkeypatch.setattr("automations.sales_turn.prepare",AsyncMock(return_value=None))
 monkeypatch.setattr("automations.sales_turn.progress_decision",AsyncMock(return_value=None))
 monkeypatch.setattr(ps,"_bought_media",AsyncMock(return_value={10}))
 offered=AsyncMock(return_value=set())
 monkeypatch.setattr(ps,"offered_media",offered)
 resolver=AsyncMock(return_value=NS(ok=case!="bought",media_ids=[11]))
 monkeypatch.setattr(cr,"resolve",resolver)
 result=asyncio.run(co.plan_contextual(lane,authority,c,fan,blocked=case=="blocked"))
 if case=="eligible":
  assert result and result[2].supporting_message_ids==(3,)
  assert offered.await_count==2 and all(c.args==("of-test",1) for c in offered.await_args_list)  # R4 shelf check + resolver
  assert resolver.call_args.kwargs["seen"]=={10}
  assert read.call_args.kwargs["contextual_message"]["messages"][-1]["id"]==3
 elif case=="explore":
  assert result is None and authority.reply_override is None  # no conversation authority in this unit fixture
  assert not resolver.called
 else:assert result is None
 if case in ("blocked","takeover","stale","ignored","duplicate","missing","disabled"):
  assert not read.called
 expected={
  "takeover":"sales_eligibility_refused",
  "stale":"purchase_freshness_refused",
  "ignored":"sales_eligibility_refused",
  "duplicate":"sales_eligibility_refused",
  "missing":"no_matching_media",
  "no_interest":"no_interest",
  "provider":"evaluation_internal_failure",
 }.get(case)
 if expected:
  assert next(e["failure_code"] for e in reversed(events)
              if e.get("failure_code"))==expected
 assert events

