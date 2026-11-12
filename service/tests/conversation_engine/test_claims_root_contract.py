"""Independent root-contract matrix; preserved safety expectations, no implementation edits.
Run both real judge() and the shared text gate used by operator approval.edit.
The direct shared-gate test is not an operator API/Postgres end-to-end proof.
"""
import json
import pytest
from conversation_engine.claims import check_plain_text, grounding_from_snapshot
from conversation_engine.engine import SyntheticProposal
from conversation_engine.model_output import JudgeContext, judge
from conversation_engine.writer_context import CreatorProfile
from onlyfans_runtime.store import Refused

PROFILE=CreatorProfile.from_facts('Riley',{'age':'27'},voice='warm',offers_permitted=True,approved_item_ids=('5',))
# case_id, attached_kind, text, should_accept, purpose
CASES=[
 ('known_copula_false_kind','photo','It is a video.',False,'existing explicit-kind control'),
 ('original_presentation_false_kind','photo','Here you go, a short video clip for you.',False,'original reproduced claim'),
 ('imperative_false_kind','photo','Enjoy this video.',False,'imperative product description'),
 ('verbless_false_kind','photo','A little video clip for you.',False,'verbless product description'),
 ('passive_false_kind','photo','Your video is attached.',False,'passive product description'),
 ('truthful_photo','photo','Here you go, three photos for you.',True,'truthful exact card contents'),
 ('truthful_video','video','Enjoy this video.',True,'same wording when actual video'),
 ('false_count','photo','Six pictures in this set.',False,'false content quantity'),
 ('true_count','photo','Three pictures in this set.',True,'exact content quantity'),
 ('unsupported_duration_explicit','video','It lasts ten minutes.',False,'duration absent from approved facts'),
 ('unsupported_duration_presentation','video','Enjoy this ten-minute video.',False,'same missing duration in alternative structure'),
 ('wrong_price','photo','It is USD 5.00 for this set.',False,'price must match exact grant'),
 ('true_price','photo','It is USD 12.00 for this set.',True,'approved price control'),
 ('question_kind','photo','Would you prefer a photo or a video?',True,'legitimate question'),
 ('quoted_kind','photo','You said "Your video is attached."',True,'quoted statement is not a new assertion'),
 ('negated_kind','photo','It is not a video.',True,'negation preserved'),
 ('unrelated_video','photo','Video games are fun.',True,'unrelated conversation'),
 ('ordinary_number','photo','Two coffees sounds good.',True,'unrelated quantity'),
]

def context(kind):
 media={'total':3,'pictures':3,'videos':0} if kind=='photo' else {'total':1,'pictures':0,'videos':1}
 snapshot={'account_id':'root-review','fan_id':1,'sales_mode':'open','sales_scope':[],
 'current_message':{'id':9,'body':'the beach set'},'history':[],
 'memory':{'state':'current','memory':{'facts':{}}},'offer_reference':{'state':'matches','offers':[{'itemId':'5','label':'Beach Set','priceCents':1200,'media':media}]},
 'catalogue_search':{'items':[]},'verified_purchases':[],
 'offer_grants':[{'grantId':'g1','itemId':'5','label':'Beach Set','description':'beach',
 'mediaCount':media['total'],'media':media,'priceCents':1200,'currency':'USD'}]}
 return JudgeContext(snapshot=snapshot,profile=PROFILE,allowed_grant_ids=frozenset({'g1'}))

def assess(path,case):
 name,kind,body,expected,purpose=case
 ctx=context(kind)
 if path=='judge':
  verdict=judge(json.dumps({'body':body,'action':'offer','offer_grant_id':'g1','pitch':True}),ctx)
  accepted=verdict.outcome=='ok';reasons=list(verdict.reasons)
 else:
  proposal=SyntheticProposal(body,action='offer',offer_grant_id='g1',pitch=True)
  try:
   check_plain_text([body],proposal,'open',{1200},grounding=grounding_from_snapshot(ctx.snapshot,proposal))
   accepted=True;reasons=[]
  except Refused as exc:
   accepted=False;reasons=[str(exc)]
 return {'path':path,'id':name,'attached':kind,'text':body,'expectedAccept':expected,
         'accepted':accepted,'purpose':purpose,'reasons':reasons,'passed':accepted==expected}

@pytest.mark.parametrize('path',['judge','shared_operator_text_gate'])
@pytest.mark.parametrize('case',CASES,ids=[c[0] for c in CASES])
def test_exact_product_facts_and_legitimate_conversation(path,case):
 result=assess(path,case)
 assert result['passed'],json.dumps(result)

if __name__=='__main__':
 results=[assess(path,case) for path in ('judge','shared_operator_text_gate') for case in CASES]
 print(json.dumps({'qualification':'source diagnostic, not immutable artifact or real model writing proof',
                  'total':len(results),'passed':sum(r['passed'] for r in results),
                  'failed':sum(not r['passed'] for r in results),'results':results},indent=2))
