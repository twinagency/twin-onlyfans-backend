"""Moxy conversation repair: the real path with sending disabled.

Every scenario goes through the real MoxyService (snapshot -> writer prompt -> scripted model ->
parse -> validate_decision -> bounded correction -> queue -> simulated delivery on SyntheticTextTransport),
with the conversation state carried from one turn to the next in the database. The model is a scripted
stand-in: these tests prove what the code hands the writer, what it refuses, how the correction is
briefed and what is delivered. They are not a measure of real-model quality (see REPAIR-REPORT.txt for the
real-model replay command).

Set MOXY_REPAIR_TRANSCRIPT_OUT=<directory> to dump the six scenario transcripts (also works on an older tree).
Set MOXY_REPAIR_REAL_MODEL=1 (plus MOXY_TEST_MODEL_API_KEY) to let the real model write the drafts instead of the
script; the budget journal of moxy_replacement/eval/smoke.py applies. Sending stays disabled either way.
"""
from datetime import timedelta
import json
import os
from pathlib import Path
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import Fan, Message
from db.onlyfans_moxy_models import MoxyEvent
from test_onlyfans_foundation_pg import database,event
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,unthrottle
from test_moxy_integrated_pg import setup,decision
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.store import utcnow


# ------------------------------------------------------------------ the replay harness
class RealModel(SyntheticNativeModel):
    """Offline replay against the real writer model (OpenRouter, budget-journalled). Nothing is sent to a fan."""
    async def generate(self,messages):
        import asyncio
        from moxy_replacement.eval import smoke
        self.calls.append(messages)
        budget=smoke.Budget(os.environ.get('MOXY_REPAIR_BUDGET_JOURNAL','/tmp/moxy-repair-budget.json'))
        resp,_=await asyncio.to_thread(smoke.call,budget,'repair-replay',messages,os.environ['MOXY_TEST_MODEL_API_KEY'])
        self.raw_output=resp['choices'][0]['message']['content']
        return self.raw_output


class Replay:
    """One fan conversation through the real path. Drafts are scripted; everything else is the product code."""

    def __init__(self,database,monkeypatch,creator='amelia'):
        self.engine,self.store,self.service,self.authority,_,self.settings=setup(database,monkeypatch,creator)
        self.creator=creator;self.next_in=100;self.next_wire=9001;self.seeded=0
        self.fan(real_name=None)      # the shared fixture gives the fan a known name; each scenario sets the name state it needs

    def close(self):self.store.close()

    def fan(self,**columns):
        with Session(self.engine) as s,s.begin():
            row=s.get(Fan,(ACCOUNT,101))
            for k,v in columns.items():setattr(row,k,v)

    def out(self,body,*,price=0,paid=False,media=None):
        """Seed an earlier outgoing message (a locked card when price is set)."""
        self.seeded+=1
        with Session(self.engine) as s,s.begin():
            s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=7000+self.seeded,direction='out',body=body,
                media_ids=json.dumps([media] if media else []),media_count=1 if media else 0,price_cents=price,
                is_paid=paid if price else None,purchased_at=utcnow() if (price and paid) else None,
                created_at=utcnow()-timedelta(minutes=60-self.seeded)))

    def say(self,text):
        mid=self.next_in;self.next_in+=1
        self.store.record(TENANT,ACCOUNT,event(mid=mid,body=text));self.store.drain(TENANT,ACCOUNT)
        return mid

    def _model(self,draft):
        if os.environ.get('MOXY_REPAIR_REAL_MODEL'):return RealModel(raw_output='unset')
        m=SyntheticNativeModel(raw_output='unset')
        def write():
            sees=json.loads(m.calls[-1][1]['content'])
            m.raw_output=json.dumps(draft(sees) if callable(draft) else draft)
        m.on_generate=write
        return m

    def _corrections(self):
        with Session(self.engine) as s:
            return [e for e in s.scalars(select(MoxyEvent)) if 'correction_of' in json.loads(e.payload_json)
                    and e.status=='pending']

    def _attempt(self,event_id,m,kind):
        with Session(self.engine) as s:
            e=s.get(MoxyEvent,(ACCOUNT,event_id));payload=json.loads(e.payload_json)
            status,reason=e.status,e.reason
        system=m.calls[0][0]['content'];sees=json.loads(m.calls[0][1]['content'])
        refs=json.loads(system.split('\nWriting references:\n',1)[1]) if '\nWriting references:\n' in system else {}
        draft=json.loads(m.raw_output)
        return {'kind':kind,'event_id':event_id,'status':status,'reason':reason,
            'draft':[p['text'] for p in draft.get('messages') or [{'text':draft['text']}]],
            'draft_action':draft['action'],'draft_product':draft['product_id'],
            'rejected':payload.get('rejected'),
            'prompt':{'fan_name':sees.get('fan_name'),'current_input':(sees.get('commercial_state') or {}).get('current_input'),
                'first_offer':sees['products']['first'].get('no_offer','offerable'),
                'situation':refs.get('latest_exchange_reads_as'),
                'reference_situations':[x.get('situation',x.get('kind')) for x in refs.get('examples',[])]}}

    def turn(self,text,*drafts):
        """The fan speaks; drafts[0] is the first writer attempt, later drafts answer each bounded correction."""
        mid=self.say(text)
        first=self.service.add_event(TENANT,ACCOUNT,101,'turn','replay-%d'%mid,{'source_message_id':mid})
        m=self._model(drafts[0]);self.service.prepare(TENANT,ACCOUNT,first,m)
        attempts=[self._attempt(first,m,'first')];final=first
        for draft in drafts[1:]:
            pending=self._corrections()
            if not pending:break
            m=self._model(draft);self.service.prepare(TENANT,ACCOUNT,pending[0].event_id,m)
            attempts.append(self._attempt(pending[0].event_id,m,'correction'));final=pending[0].event_id
        sent=[]
        with Session(self.engine) as s:status=s.get(MoxyEvent,(ACCOUNT,final)).status
        for _ in range(8):
            if status!='ready' and status!='queued':break
            unthrottle(self.engine)
            intent=self.service.queue(TENANT,ACCOUNT,final)
            wire=SyntheticTextTransport(ACCOUNT,message_id=self.next_wire);self.next_wire+=1
            if self.authority.dispatch(TENANT,ACCOUNT,intent,wire)!='accepted':break
            sent.append({'body':wire.calls[0].body,'media':list(wire.calls[0].media_ids),'price_cents':wire.calls[0].price_cents})
            with Session(self.engine) as s:status=s.get(MoxyEvent,(ACCOUNT,final)).status
        return {'fan':text,'attempts':attempts,'final_status':status,'sent':sent}


def reply(*bubbles,action='reply',product=None,sequence=None,media_at=None,**extra):
    parts=[{'text':b,'media':i==media_at} for i,b in enumerate(bubbles)]
    return decision(action,bubbles[0],product,sequence=sequence,messages=parts,**extra)


def offer(*bubbles,media_at=-1):
    media_at=len(bubbles)-1 if media_at==-1 else media_at
    return reply(*bubbles,action='offer',product='first',sequence='fixture_level',media_at=media_at)


# ------------------------------------------------------------------ the six failure scenarios
def scenario_clarification(r):
    # A labelled synthetic approved fact exercises the factual correction path.
    r.settings['public_facts']['occupation']='I work as a graphic designer'
    r.service.configure(TENANT,ACCOUNT,expected_revision=1,mode='on',settings=r.settings,actor='fixture-operator')
    r.out('oh i know exactly what you mean, you wanna see the gym set right?')
    return {'id':'1_clarification','title':'Fan clarifies; the bot repeats its earlier interpretation','creator':'amelia',
        'turns':[r.turn('no no i meant what do you do for work, not the gym set',
            reply('oh i know exactly what you mean, you wanna see the gym set right? 😏'),
            reply('ha my bad','I work as a graphic designer'))]}


def scenario_question_vs_nudge(r):
    r.out('locked',price=7500,paid=False,media=7101)
    r.out('btw dont you wanna open what i recently sent you babe???')
    return {'id':'2_question_not_nudge','title':'Fan asks a question; the bot sends its sales nudge again','creator':'amelia',
        'turns':[r.turn('what time do you usually go live?',
            reply('btw dont you wanna open what i recently sent you babe?? you can open more 🍑'),
            reply('i dont have a confirmed live time to share right now','keep an eye on the page for an announcement')),
          r.turn('ok ill be there',reply('see you there babe'))]}


def scenario_unpaid_as_viewed(r):
    r.out('locked',price=7500,paid=False,media=7101)
    return {'id':'3_unpaid_viewed','title':'Unpaid content described as already viewed','creator':'amelia',
        'turns':[r.turn('hey you there?',
            reply('hope you enjoyed it babe, you already opened it right? 😏'),
            reply('hey! yep im here','the one i sent is still locked for you, open it whenever you feel like it'))]}


def scenario_known_name(r):
    r.fan(of_display_name='Matthew Campbell')
    def fix(sees):
        who=sees.get('fan_name')
        return reply('hey %s, good to see you'%who if who else 'hey you, good to see you','how was the gym today?')
    return {'id':'4_known_name','title':'Known name ignored; the bot restarts introductions','creator':'amelia',
        'turns':[r.turn('hey',reply('hey you, i dont think we have met yet, whats your name? 😊'),fix)]}


def scenario_repeated_joke_offer(r):
    r.out('ok but gym bros are the real sprint workout, i just lift cars 😂')
    def retry(sees):
        if sees['products']['first'].get('no_offer'):       # the offer was taken away from the writer
            return reply('ha ok ok you win','want me to send you something?')
        return offer('fine you win, here you go','this one is all yours')
    return {'id':'5_repeated_joke','title':'Repeated joke with a new ending; a valid offer must survive the rewrite','creator':'amelia',
        'turns':[r.turn('show me something then',
            offer('ok but gym bros are the real sprint workout, i just lift fridges 😂','this one is for you babe'),retry)]}


def scenario_bubbles(r):
    bubbles=['i understandd bestiee','you always make me blush with these compliments lol','sweet and lovely smile you have there!']
    return {'id':'6_bubbles','title':'Intended separate bubbles reach the fan in order','creator':'amelia',
        'turns':[r.turn('I’m not feeling frisky tonight, but you look really good',reply(*bubbles)),
                 r.turn('thanks, you too',reply('aw you are so sweet'))]}


SCENARIOS=[scenario_clarification,scenario_question_vs_nudge,scenario_unpaid_as_viewed,scenario_known_name,
           scenario_repeated_joke_offer,scenario_bubbles]


def run_scenario(database,monkeypatch,fn):
    r=Replay(database,monkeypatch)
    try:return fn(r)
    finally:r.close()


@pytest.mark.parametrize('name',[f.__name__ for f in SCENARIOS])
def test_dump_transcripts(database,monkeypatch,name):
    """Not an assertion: writes each scenario's transcript into MOXY_REPAIR_TRANSCRIPT_OUT (a directory) when it is set."""
    out=os.environ.get('MOXY_REPAIR_TRANSCRIPT_OUT')
    if not out:pytest.skip('set MOXY_REPAIR_TRANSCRIPT_OUT to dump transcripts')
    result=run_scenario(database,monkeypatch,{f.__name__:f for f in SCENARIOS}[name])
    Path(out).mkdir(parents=True,exist_ok=True)
    (Path(out)/(name+'.json')).write_text(json.dumps(result,indent=1,ensure_ascii=False))


@pytest.fixture(params=[f.__name__ for f in SCENARIOS])
def transcript(request,database,monkeypatch):
    fn={f.__name__:f for f in SCENARIOS}[request.param]
    return run_scenario(database,monkeypatch,fn)


def _bodies(t):return [x['body'] for turn in t['turns'] for x in turn['sent']]


# 1. Fan clarifies, bot repeats its interpretation -> the reply addresses the clarification.
def test_clarification_is_answered_not_repeated(database,monkeypatch):
    t=run_scenario(database,monkeypatch,scenario_clarification)
    turn=t['turns'][0];first,fix=turn['attempts']
    assert first['status']=='held' and first['reason']=='repeated_recent_phrase'
    assert fix['rejected']['kind']=='wording' and fix['rejected']['fan_message'].startswith('no no i meant what do you do for work')
    assert fix['prompt']['current_input']['text']==turn['fan']            # the writer is shown the clarification as the current input
    assert 'clarification' in fix['prompt']['situation']
    assert [x['body'] for x in turn['sent']]==['ha my bad','I work as a graphic designer']
    assert turn['final_status']=='done'


# 2. Fan asks a question, bot repeats a sales nudge -> the question is answered, the locked card is not re-sent.
def test_question_is_answered_and_nudge_not_repeated(database,monkeypatch):
    t=run_scenario(database,monkeypatch,scenario_question_vs_nudge)
    turn=t['turns'][0];first,fix=turn['attempts']
    assert first['reason']=='repeated_recent_phrase' and fix['prompt']['current_input']['text']=='what time do you usually go live?'
    assert 'question' in fix['prompt']['situation']
    assert [x['body'] for x in turn['sent']]==['i dont have a confirmed live time to share right now','keep an eye on the page for an announcement']
    assert all(x['price_cents']==0 and not x['media'] for turn in t['turns'] for x in turn['sent'])   # no second card
    # State carried forward: the second turn is written against a history that contains the answer.
    assert t['turns'][1]['final_status']=='done' and t['turns'][1]['sent'][0]['body']=='see you there babe'


# 3. Unpaid content described as viewed -> wording reflects the actual status.
def test_unpaid_card_cannot_be_described_as_opened(database,monkeypatch):
    t=run_scenario(database,monkeypatch,scenario_unpaid_as_viewed)
    turn=t['turns'][0];first,fix=turn['attempts']
    assert first['status']=='held' and first['reason']=='content_status_wording'
    assert fix['rejected']['kind']=='wording'
    assert 'locked' in turn['sent'][-1]['body'] and 'opened it right' not in ' '.join(x['body'] for x in turn['sent'])


def test_same_wording_is_fine_once_the_card_is_purchased(database,monkeypatch):
    r=Replay(database,monkeypatch)
    try:
        r.out('locked',price=7500,paid=True,media=7101)
        turn=r.turn('hey you there?',reply('hope you enjoyed it babe, you already opened it right? 😏'))
        assert turn['attempts'][0]['status']=='ready' and turn['final_status']=='done'
    finally:r.close()


def test_fans_own_report_of_opening_is_evidence(database,monkeypatch):
    r=Replay(database,monkeypatch)
    try:
        r.out('locked',price=7500,paid=False,media=7101)
        turn=r.turn('i just opened it, wow',reply('so glad you liked it, did you enjoy it?'))
        assert turn['attempts'][0]['status']=='ready'
    finally:r.close()


# 4. Known name ignored -> the name is passed to the writer, introductions are not restarted.
def test_known_name_reaches_writer_and_is_not_asked_for(database,monkeypatch):
    t=run_scenario(database,monkeypatch,scenario_known_name)
    first,fix=t['turns'][0]['attempts']
    assert first['prompt']['fan_name']=='Matthew' and fix['prompt']['fan_name']=='Matthew'
    assert first['reason']=='asks_known_name'
    assert t['turns'][0]['sent'][0]['body']=='hey Matthew, good to see you'


@pytest.mark.parametrize('display',['u12345','Redddd1','YC','C. A. A.','@bigobj77',None])
def test_unusable_display_names_are_not_passed_and_not_demanded(database,monkeypatch,display):
    r=Replay(database,monkeypatch)
    try:
        r.fan(of_display_name=display)
        turn=r.turn('hey',reply('hey you, how was your day?'))
        a=turn['attempts'][0]
        assert a['prompt']['fan_name'] is None and a['status']=='ready'       # continues without a name
        turn2=r.turn('good, you?',reply('good! what are your plans tonight, whats your name by the way?'))
        assert turn2['attempts'][0]['status']=='ready'                      # asking is not blocked when no name is known
    finally:r.close()


# 5. Repeated joke/opening -> rejected and corrected, the valid offer is preserved.
def test_repeated_joke_is_rewritten_and_the_offer_survives(database,monkeypatch):
    t=run_scenario(database,monkeypatch,scenario_repeated_joke_offer)
    turn=t['turns'][0];first,fix=turn['attempts']
    assert first['reason']=='repeated_recent_phrase'
    assert fix['rejected']['kind']=='wording' and fix['rejected']['product_id'] is None
    assert fix['rejected']['keep']=={'action':'offer','product_id':'first','sequence':'fixture_level'}
    assert fix['prompt']['first_offer']=='offerable'                      # not "refused_in_this_turn"
    assert fix['status']=='ready' and turn['final_status']=='done'
    assert [x['body'] for x in turn['sent']]==['fine you win, here you go','this one is all yours']
    assert [x['media'] for x in turn['sent']]==[[],[7101]] and turn['sent'][1]['price_cents']==7500


def test_product_failure_still_refuses_the_product_and_keeps_the_question(database,monkeypatch):
    r=Replay(database,monkeypatch)
    try:
        r.out('locked',price=7500,paid=False,media=7101)
        turn=r.turn('what are you wearing in it?',
            reply('here you go again','this is the one',action='offer',product='first',sequence='fixture_level',media_at=1),
            reply('it is a little sports set, still locked for you whenever you want it'))
        first,fix=turn['attempts']
        assert first['reason']=='media_previously_sent_requires_explicit_resend'
        assert fix['rejected']['kind']=='product' and fix['rejected']['product_id']=='first'
        assert fix['rejected']['fan_message']=='what are you wearing in it?'
        assert fix['prompt']['first_offer']=='refused'
        assert turn['final_status']=='done'
    finally:r.close()


def test_wording_correction_can_still_not_smuggle_in_a_bad_product(database,monkeypatch):
    """Wording and product are judged separately: a wording rewrite that keeps an INVALID product is a product failure."""
    r=Replay(database,monkeypatch)
    try:
        r.out('ok but gym bros are the real sprint workout, i just lift cars 😂')
        turn=r.turn('show me something then',
            offer('ok but gym bros are the real sprint workout, i just lift fridges 😂','this one is for you babe'),
            reply('fine you win','here you go',action='offer',product='missing-product',sequence='fixture_level',media_at=1))
        assert turn['attempts'][1]['status']=='held' and turn['attempts'][1]['reason']=='real_inventory_missing_or_stale'
    finally:r.close()


def test_product_failure_outranks_wording_failure(database,monkeypatch):
    r=Replay(database,monkeypatch)
    try:
        r.out('ok but gym bros are the real sprint workout, i just lift cars 😂')
        turn=r.turn('show me something then',
            reply('ok but gym bros are the real sprint workout, i just lift fridges 😂','here',action='offer',product='missing-product',
                  sequence='fixture_level',media_at=1),
            reply('ha you win'))
        first,fix=turn['attempts']
        assert first['reason']=='real_inventory_missing_or_stale' and fix['rejected']['kind']=='product'
    finally:r.close()


def test_a_second_wording_failure_uses_one_validated_fallback_without_chat_lock(database,monkeypatch):
    from db.onlyfans_case_models import OfOperatorCase
    r=Replay(database,monkeypatch)
    try:
        r.out('ok but gym bros are the real sprint workout, i just lift cars 😂')
        again=reply('ok but gym bros are the real sprint workout, i just lift boats 😂')
        turn=r.turn('show me something then',again,again,again)
        assert [a['kind'] for a in turn['attempts']]==['first','correction']          # one bounded correction only
        assert turn['attempts'][1]['status']=='ready' and len(turn['sent'])==1
        assert turn['sent'][0]['media']==[] and turn['sent'][0]['price_cents']==0
        with Session(r.engine) as s:
            assert not [c for c in s.scalars(select(OfOperatorCase))
                if c.scope=='all' and c.reason in {'moxy_correction_failed','moxy_technical_attention'}]
    finally:r.close()


# 6. Separate bubbles -> simulated delivery preserves text and order.
def test_separate_bubbles_are_delivered_in_order_with_exact_text(database,monkeypatch):
    t=run_scenario(database,monkeypatch,scenario_bubbles)
    first=t['turns'][0]
    assert [x['body'] for x in first['sent']]==['i understandd bestiee','you always make me blush with these compliments lol',
        'sweet and lovely smile you have there!']
    assert first['final_status']=='done' and t['turns'][1]['sent'][0]['body']=='aw you are so sweet'


def test_one_message_is_enough_and_no_emoji_or_bubble_quota(database,monkeypatch):
    r=Replay(database,monkeypatch)
    try:
        turn=r.turn('hey',decision('reply','hey you, how was your day'))                 # bare text, no messages array, no emoji
        assert turn['attempts'][0]['status']=='ready' and [x['body'] for x in turn['sent']]==['hey you, how was your day']
        with Session(r.engine) as s:
            stored=json.loads(s.scalars(select(MoxyEvent).order_by(MoxyEvent.created_at)).all()[-1].decision_json)
        assert stored['messages']==[{'text':'hey you, how was your day','media':False}]   # the array is the normal shape
    finally:r.close()


def test_card_reply_without_messages_array_is_normalised_with_one_media_part(database,monkeypatch):
    r=Replay(database,monkeypatch)
    try:
        turn=r.turn('show me',decision('offer','here you go babe','first',sequence='fixture_level'))
        assert [(x['body'],x['media'],x['price_cents']) for x in turn['sent']]==[('here you go babe',[7101],7500)]
    finally:r.close()


def test_every_scenario_ends_with_a_delivered_reply(transcript):
    assert all(t['final_status']=='done' for t in transcript['turns']) and _bodies(transcript)


# ------------------------------------------------------------------ approved voice is not blocked by the new detectors
def test_approved_chatter_bubbles_pass_the_repetition_and_status_detectors():
    from moxy_replacement.examples import _bank
    from moxy_replacement.runtime import wording_problem
    for ex in _bank()['examples']:
        history=[]
        for i,t in enumerate(ex['turns']):
            if t['who']=='marker':continue
            if t['who']=='chatter':
                snap={'history':history,'event':{'payload':{}}}
                assert wording_problem(snap,{'action':'reply','text':t['text']}) is None,(ex['record'],t['text'])
            history.append({'id':i,'direction':'out' if t['who']=='chatter' else 'in','text':t['text'],'price_cents':0})


@pytest.mark.parametrize('record',[45,89])
def test_held_back_conversations_run_with_a_bank_that_excludes_them(database,monkeypatch,record):
    """Generalisation set: the held-back excerpt's own last exchange is replayed while its text is hidden from the model."""
    from moxy_replacement import examples
    from moxy_replacement.examples import _bank
    monkeypatch.setattr(examples,'EXCLUDE_HELD_BACK',True)
    ex=next(e for e in _bank()['examples'] if e['record']==record)
    assert ex['held_back']
    turns=[t for t in ex['turns'] if t['who']!='marker']
    start=max(i for i,t in enumerate(turns) if t['who']=='chatter' and i>0 and turns[i-1]['who']=='fan')
    burst=[];i=start
    while i<len(turns) and turns[i]['who']=='chatter':burst.append(turns[i]['text']);i+=1
    r=Replay(database,monkeypatch,'cristina')
    try:
        turn=r.turn(turns[start-1]['text'],reply(*burst))
        a=turn['attempts'][0]
        assert a['status']=='ready' and turn['final_status']=='done'
        assert [x['body'] for x in turn['sent']]==burst                       # approved voice passes every detector, in order
        assert len(a['prompt']['reference_situations'])>=2
        system_text=json.dumps(examples.references('cristina',exclude_held_back=True),ensure_ascii=False)
        assert not any(t['text'] in system_text for t in ex['turns'] if t['who']=='chatter' and len(t['text'])>15)
    finally:r.close()


# ------------------------------------------------------------------ example bank and selector (no database)
APPROVED={2,4,6,45,55,64,70,89}
APPROVED_FILE=Path('/home/ronan/twin/moxy-convo-repair-inputs/APPROVED-EXCERPTS.txt')


def _hist(*fan_texts,out=None):
    rows=[{'id':1,'direction':'out','text':out}] if out else []
    return rows+[{'id':10+i,'direction':'in','text':t} for i,t in enumerate(fan_texts)]


def _records(refs):
    from moxy_replacement.examples import _bank
    by_text={e['lesson']:e['record'] for e in _bank()['examples']}
    return [by_text[e['lesson']] for e in refs['examples']]


def test_bank_is_exactly_the_eight_approved_records_and_verbatim():
    from moxy_replacement.examples import _bank,verified,verbatim
    bank=_bank()['examples']
    assert {e['record'] for e in bank}==APPROVED and all(verified(e) for e in bank)
    assert all(e['source']['sha256'].startswith('b30c585b') for e in bank)
    if APPROVED_FILE.exists():           # the inputs folder is only on the build machine
        import re
        blocks={int(m.group(1)):m.group(2).rstrip('\n') for m in re.finditer(r'RECORD #(\d+) \|[^\n]*\n(.*?)(?=\n={20,}|\Z)',APPROVED_FILE.read_text(),re.S)}
        assert all(verbatim(e)==blocks[e['record']] for e in bank)


def test_every_excerpt_is_labelled_and_chatter_bubbles_stay_separate():
    from moxy_replacement.examples import _bank
    labels={e['record']:e['situation'] for e in _bank()['examples']}
    assert labels=={2:'sales_flow',4:'money_trouble_rapport',6:'declined_offer',45:'personal_chat',55:'complaint_recovery',
        64:'spending_limit',70:'honest_unavailable_alternative',89:'sales_flow'}
    ex={e['record']:e for e in _bank()['examples']}
    assert [t['text'] for t in ex[6]['turns'] if t['who']=='chatter'][:2]==[
        'did you love my collections, bestie???','you can open more bestiee if you wantt']      # two bubbles, not one blob


def test_held_back_set_is_documented_and_excluded_on_request():
    from moxy_replacement.examples import held_back,references
    assert held_back()==[45,89]
    refs=references('cristina',_hist('you look so hot, i want to fuck you'),exclude_held_back=True)
    assert not set(_records(refs))&{45,89}


def test_projection_carries_no_provenance_and_says_names_prices_are_not_facts():
    from moxy_replacement.examples import references
    from moxy_replacement.configuration import canonical
    refs=references('cristina',_hist('i cant afford it right now'))
    text=canonical(refs)
    assert 'b30c585b' not in text and 'TOP93' not in text and '"record"' not in text and '"at"' not in text
    assert 'NOT facts' in refs['scope'] and 'prices' in refs['scope'] and 'instructions' in refs['scope']


@pytest.mark.parametrize('fan',[
    'i want to fuck your throat','show me your pussy','im so horny for you daddy 😈','suck my cock','i wanna cum on your tits',
    'you look so hot in that booty shot','send me something naughty','what would you do to me tonight? 🥵'])
@pytest.mark.parametrize('spender',[False,True])
def test_amelia_never_gets_the_explicit_sexting_excerpts(fan,spender):
    from moxy_replacement.examples import references,select
    state={'vip':spender,'spender':spender,'high_spender':spender}
    chosen,_=select('amelia',_hist(fan),state)
    assert not {e['record'] for e in chosen}&{2,89} and len(chosen)>=2
    assert not set(_records(references('amelia',_hist(fan),state)))&{2,89}


def test_cristina_gets_the_explicit_sales_excerpt_for_a_sexting_fan():
    from moxy_replacement.examples import select
    chosen,_=select('cristina',_hist('i want to fuck your throat, on your knees'))
    assert chosen[0]['record'] in {2,89}
    chosen,_=select('emma_karter',_hist('suck my cock baby 😈'))
    assert chosen[0]['record'] in {2,89}
    chosen,_=select('someone_new',_hist('suck my cock baby 😈'))
    assert not {e['record'] for e in chosen}&{2,89}          # an unknown creator is never assumed to allow explicit text


@pytest.mark.parametrize('fan,expected',[
    ('I cant afford that right now, sticking to cheaper ones',{64,4}),
    ('I am so behind on my finances at the moment',{4,64}),
    ('do you make customs where you spread your cheeks?',{70}),
    ('Do you ever sell your panties here?',{70}),
    ('I have this bundle already',{55}),
    ('Im not in the mood tonight but you look really good',{6}),
    ('Yesterday was leg day, then I watched soccer all afternoon, my birthday is next week',{45,6,4}),
])
def test_selector_matches_the_latest_exchange(fan,expected):
    from moxy_replacement.examples import select
    chosen,_=select('amelia',_hist(fan))
    assert chosen[0]['record'] in expected,[e['record'] for e in chosen]


def test_selector_uses_the_latest_message_not_an_earlier_one():
    from moxy_replacement.examples import select
    history=_hist('i have this bundle already','ok no worries, how was your day at work today?')
    chosen,weights=select('amelia',history)
    assert chosen[0]['record']!=55 and weights['personal_chat']>weights.get('complaint_recovery',0)


def test_clarification_and_question_are_read_from_the_recent_exchange():
    from moxy_replacement.examples import situations
    assert 'clarification' in situations(_hist('no no i meant what do you do for work, not the gym set'))
    assert 'question' in situations(_hist('what time do you usually go live?'))
    assert 'hardship' in situations(_hist('ok'),{'financial_hardship':True})


def test_selector_is_deterministic_and_always_returns_two_to_three():
    from moxy_replacement.examples import select
    for creator in ('amelia','cristina'):
        for fan in ('','ok','hey','🙂','random words here'):
            a,_=select(creator,_hist(fan));b,_=select(creator,_hist(fan))
            assert [e['record'] for e in a]==[e['record'] for e in b] and 2<=len(a)<=3


# ------------------------------------------------------------------ writer prompt: one consistent instruction set
def _prompt(creator='amelia',**extra):
    from moxy_replacement import writer
    from moxy_replacement.configuration import load
    snap={'fan_id':101,'pilot':None,'instructions':load(creator).public_context(),'event':{'kind':'turn','payload':{}},'history':[],**extra}
    return writer.messages(snap)[0]['content']


def test_prompt_no_longer_holds_contradictory_rules():
    text=_prompt()
    assert 'Actively develop' not in text and 'median about' not in text and 'often 1 to 3 short bubbles' not in text
    assert 'unless the action is a handover' not in text            # contradicted "you will look into it" for complaints
    assert 'playfully in one short line' not in text                  # contradicted plain, kind availability answers
    assert 'turn the interest into an invite or offer' not in text   # turned every flirt into a sale
    assert text.count('No message-count quota')==1 and 'no emoji quota' in text
    assert 'Order of work' in text and 'fan\'s latest message' in text and 'clarification' in text
    assert 'only to the activity and conditions it names' in text
    assert 'only when it is relevant' in text and 'verified current asset information' in text


def test_prompt_states_each_rule_once():
    text=_prompt()
    for phrase in ('Affection alone neither','Do not SEND a duplicate card','There is no rule about free content'):
        assert text.count(phrase)==1,phrase


def test_prompt_correction_and_name_instructions():
    text=_prompt()
    assert 'kind wording' in text and 'keep the valid offer' in text and 'kind product' in text and 'answer fan_message first' in text
    assert 'Never ask for his name' in text


def test_prompt_carries_the_situation_selected_references_for_the_creator():
    text=_prompt('amelia',history=_hist('i want to fuck your throat'))
    refs=json.loads(text.split('\nWriting references:\n',1)[1])
    assert 'sexting_scene' in refs['latest_exchange_reads_as']
    assert all(e['situation']!='sales_flow' for e in refs['examples'])      # Amelia: no explicit sales excerpt
    cristina=json.loads(_prompt('cristina',history=_hist('i want to fuck your throat')).split('\nWriting references:\n',1)[1])
    assert cristina['examples'][0]['situation']=='sales_flow'


# ------------------------------------------------------------------ repetition, status wording, names, correction brief
@pytest.mark.parametrize('new,old,flagged',[
    ('ok but gym bros are the real sprint workout, i just lift fridges 😂','ok but gym bros are the real sprint workout, i just lift cars',True),
    ('btw dont you wanna open what i recently sent you babe??','btw dont you wanna open what i recently sent you',True),
    ('haha youre so bad, i love it babe','haha youre so bad babe 😏 i love that',True),
    ('hey babe how was your day','hey baby how was your day today',False),
    ('omg babe ur not ready 😈','omg baby ur not ready',False),
    ('hey babe 😏','hey baby 🥵',False),
    ('lol what are you up to','what are you doing tonight lol',False),
    ('i love hearing that, tell me more about the gym','i would love to hear more about your gym day',False),
    ('yes baby 💕','yes babyyy 💕',False),
    ('how was your day babe','how was your day',False),
])
def test_repetition_catches_reused_phrases_not_common_words_pet_names_or_emoji(new,old,flagged):
    from moxy_replacement.parts import shared_wording
    assert shared_wording(new,old) is flagged


def test_status_wording_check_follows_actual_card_status():
    from moxy_replacement.runtime import content_status_violation
    locked=[{'direction':'out','price_cents':7500,'paid':False},{'direction':'in','text':'hey'}]
    paid=[{'direction':'out','price_cents':7500,'paid':True},{'direction':'in','text':'hey'}]
    free=[{'direction':'out','price_cents':0,'media_count':1},{'direction':'in','text':'hey'}]
    for text in ('hope you enjoyed it','did you like the video?','you already opened it right','what did you think of the set'):
        assert content_status_violation({'history':locked},[text])
        assert not content_status_violation({'history':paid},[text]) and not content_status_violation({'history':free},[text])
    assert not content_status_violation({'history':locked},['the one i sent is still locked, open it whenever'])
    assert not content_status_violation({'history':locked+[{'direction':'in','text':'i just unlocked it'}]},['hope you enjoyed it'])


@pytest.mark.parametrize('value,name',[('Matthew Campbell','Matthew'),('richard silva','Richard'),('Jay','Jay'),('🔥Mike🔥','Mike'),
    ('JOHN','John'),('u12345',None),('Redddd1',None),('bigobj7',None),('YC',None),('C. A. A.',None),('@jules',None),('',None),(None,None),
    ('Guest',None),('12345',None),('x_y',None)])
def test_clean_name(value,name):
    from moxy_replacement.runtime import clean_name
    assert clean_name(value)==name


def test_usable_fan_name_prefers_verified_memory_then_chosen_then_profile():
    from moxy_replacement.runtime import usable_fan_name
    memory={'facts':{'real_name':{'value':'Robert','source':'history'}}}
    fan={'fan_chosen_nickname':'Bobby','real_name':'Rob','is_name_real':True,'of_display_name':'Matthew Campbell'}
    assert usable_fan_name(fan,memory)=='Robert' and usable_fan_name(fan,{})=='Bobby'
    assert usable_fan_name({'of_display_name':'Matthew Campbell'},{})=='Matthew'
    assert usable_fan_name({'of_display_name':'u1234','real_name':'x1','is_name_real':False},{}) is None
    assert usable_fan_name(None,None) is None


def test_correction_brief_separates_wording_from_product():
    from moxy_replacement.runtime import correction_brief
    snap={'history':[{'direction':'in','text':'show me something'}]}
    result={'action':'offer','product_id':'first','sequence':'s','text':'again','messages':[{'text':'again','media':False},{'text':'x','media':True}]}
    wording=correction_brief('repeated_recent_phrase',result,snap)
    assert wording['kind']=='wording' and wording['product_id'] is None and wording['keep']=={'action':'offer','product_id':'first','sequence':'s'}
    assert wording['avoid']==['again','x'] and wording['fan_message']=='show me something'
    product=correction_brief('product_unavailable',result,snap)
    assert product['kind']=='product' and product['product_id']=='first' and 'keep' not in product and product['fan_message']=='show me something'
    reply_only=correction_brief('invented_policy_claim',{'action':'reply','product_id':None,'text':'no freebies'},snap)
    assert 'keep' not in reply_only


def test_only_a_product_brief_makes_the_product_unofferable():
    from moxy_replacement.runtime import correction_brief,offer_eligibility
    class PA:
        products={'first':{},'second':{}};sequences={}
        def terms(self,*a,**k):return {}
    snap={'history':[]}
    result={'action':'offer','product_id':'first','sequence':None,'text':'t'}
    wording={'rejected':correction_brief('repeated_recent_line',result,snap)}
    product={'rejected':correction_brief('product_unavailable',result,snap)}
    assert offer_eligibility(PA(),{},wording)=={'first':'ok','second':'ok'}
    assert offer_eligibility(PA(),{},product)=={'first':'refused_in_this_turn','second':'ok'}


def test_own_delivered_parts_do_not_count_as_repeated_lines_for_later_parts():
    from moxy_replacement.runtime import wording_problem
    history=[{'id':5,'direction':'out','text':'omg babe ur so not ready for this one','price_cents':0}]
    later={'action':'reply','text':'omg babe ur so not ready for this one too'}
    assert wording_problem({'history':history,'event':{'payload':{}}},later)=='repeated_recent_phrase'
    own={'event':{'payload':{'_moxy_delivery':{'receipts':[{'intent_id':'i','message_id':5}]}}},'history':history}
    assert wording_problem(own,later) is None


def test_parse_makes_the_messages_array_the_normal_shape():
    from moxy_replacement import writer
    v=writer.parse(json.dumps(decision('reply','hey you')))
    assert v['messages']==[{'text':'hey you','media':False}]
    v=writer.parse(json.dumps(decision('offer','here','first',sequence='s')))
    assert v['messages']==[{'text':'here','media':True}]
    v=writer.parse(json.dumps(decision('handover','')))
    assert 'messages' not in v
    v=writer.parse(json.dumps(decision('reply','a',messages=[{'text':'a','media':False},{'text':'b','media':False}])))
    assert [m['text'] for m in v['messages']]==['a','b']
