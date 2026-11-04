"""Negation/quotation regressions from Jesse's review; no model or network."""
import pytest
from moxy_replacement.conversation import history_problem
from moxy_replacement.parts import repetition_texts,reused_wording


def h(direction,text,mid):return {'id':mid,'direction':direction,'text':text,'sender':'fan' if direction=='in' else 'ai'}
def snap(*rows,**kw):return {'history':list(rows),**kw}
def dec(text):return {'action':'reply','text':text,'messages':[{'text':text,'media':False}]}

@pytest.mark.parametrize('fan,reply,expected',[
 ("I never said I'd pass out","You said you'd pass out.",'unsupported_fan_attribution'),
 ("I'd pass out","You didn't say you'd pass out.",'unsupported_fan_attribution'),
 ("I never said I'd pass out","You didn't say you'd pass out.",None),
 ("I didn't say I'd pass out","You said you'd pass out.",'unsupported_fan_attribution'),
 ("I said I wouldn't pass out","You said you'd pass out.",'unsupported_fan_attribution'),
 ("I don't think I'd pass out","You said you'd pass out.",'unsupported_fan_attribution'),
 ("When did I say I'd pass out?","You said you'd pass out.",'unsupported_fan_attribution'),
 ("My friend said I'd pass out","You said you'd pass out.",'unsupported_fan_attribution'),
 ("I said I'd pass out?","You said you'd pass out.",'unsupported_fan_attribution'),
 ("I said I wouldn't pass out","You said you wouldn't pass out.",None),
 ("I said \"I'd stay awake\"","You said you'd stay awake.",None),
 ("I said, \"I'd stay awake\"","You said, \"I'd stay awake\".",None),
 ("I never said I'd pass out","You said \"I never said I'd pass out\".",None),
 ("You said I'd pass out","You said you'd pass out.",'unsupported_fan_attribution'),
 ("\"I'd pass out\" was your line","You said you'd pass out.",'unsupported_fan_attribution'),
 ("I'd stay awake","I was wrong when I said you said you'd pass out.",None),
 ("I'd stay awake","I was wrong when I said \"you said you'd pass out\".",None),
 ("I'd stay awake","I was wrong when I said you said you'd pass out, but you said you'd pass out.",'unsupported_fan_attribution'),
 ("I'd stay awake","You said you'd stay awake, and you said you'd pass out.",'unsupported_fan_attribution'),
 ("I'd stay awake","You said you'd stay awake, but you also said you'd pass out.",'unsupported_fan_attribution'),
 ("I'd stay awake","You said you'd stay awake, and we met yesterday.",'unsupported_relationship_history'),
 ("I never said I'd pass out, I would stay awake","You said you would stay awake.",None),
 ("I'd pass out, I said I'd stay awake","You said you'd pass out, and you said you'd stay awake.",None),
])
def test_attribution_preserves_denial_quotation_retraction_and_all_clauses(fan,reply,expected):
 assert history_problem(snap(h('in',fan,1)),[reply])==expected


def projected(snapshot,text):
 return [{'text':text,'media':False} for text in repetition_texts(snapshot,dec(text))]


def test_exact_requested_correction_is_not_a_loop():
 bad="after you said you'd pass out on me"
 snapshot=snap(h('out',"you'd be lucky to get a goodnight kiss before I pass out cold",1),h('out',bad,2),h('in','When did I say that?',3))
 reply="You didn't say you'd pass out on me. I said that"
 assert history_problem(snapshot,[reply]) is None
 assert reused_wording(projected(snapshot,reply),[bad]) is None


def test_correction_does_not_excuse_an_unrelated_repeated_sales_tail():
 bad="after you said you'd pass out on me";pitch='Buy the outfit photo and show me what you think about it'
 snapshot=snap(h('out',"a kiss before I pass out cold",1),h('out',bad,2),h('out',pitch,3),h('in','When did I say that?',4))
 reply="You didn't say you'd pass out on me. I said that. "+pitch
 assert reused_wording(projected(snapshot,reply),[bad,pitch])==pitch


def test_requested_evidence_backed_quote_is_not_a_loop():
 line='The cheeky outfit photograph is already waiting in our chat'
 snapshot=snap(h('out',line,1),h('in','What exactly did you say? Quote it.',2))
 reply='I said "'+line+'".'
 assert reused_wording(projected(snapshot,reply),[line]) is None


@pytest.mark.parametrize('line,fan_request,terms,products',[
 ('The photograph costs $20.00','Can you repeat the price?',{'price_cents':2000},{}),
 ('The video is five minutes long','How long is the video again?',{'price_cents':2000},{'one':{'assets':[{'facts':['The video is five minutes long']} ]}}),
 ('The video is 5 minutes long','How long is the video again?',{'price_cents':2000},{'one':{'assets':[{'facts':['The video is 5 minutes long']} ]}}),
])
def test_requested_grounded_price_or_duration_can_be_repeated(line,fan_request,terms,products):
 snapshot=snap(h('out',line,1),h('in',fan_request,2),inventory={'one':terms},products=products)
 assert reused_wording(projected(snapshot,line),[line]) is None


def test_invented_price_or_unrequested_sales_loop_gets_no_exemption():
 line='The photograph costs $999 and it is ready for you'
 snapshot=snap(h('out',line,1),h('in','Repeat the price?',2),inventory={'one':{'price_cents':2000}})
 assert reused_wording(projected(snapshot,line),[line])==line
 pitch='You should buy the outfit picture and show me what you think'
 snapshot=snap(h('out',pitch,1),h('in','hey',2))
 assert reused_wording(projected(snapshot,pitch),[pitch])==pitch


def test_quoting_a_known_creator_mistake_does_not_reassert_it():
 old="You never said you'd pass out"
 snapshot=snap(h('in',"I'd pass out",1),h('out',old,2),h('in','What was the mistake?',3))
 assert history_problem(snapshot,['I said "'+old+'". That was wrong.']) is None
