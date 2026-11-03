"""Bounded paid qualification evidence for ONE authorised pair, no fan events, no sends.

Authorised pair: account of-312988353 (Amelia), fan 318022380 (Gervonta). Anything else exits.

A real run takes one read-only snapshot (the transaction is rolled back), deep-copies it per case,
REPLACES the history with labelled SYNTHETIC multi-turn history, optionally overrides in-memory
fan_state / invitation, calls the real configured model through NativeProviderModel with the
existing reserve/settle cost accounting, parses with writer.parse and validates with
runtime.validate_decision. Nothing is written to the database and nothing is sent.

The automatic checks are SCREENS only. They never decide pass/fail. Every result is written to
review.md (and results.json) with an empty `Verdict:` line for a human reviewer. The exit code is 0
even when screens flag something; budget exhaustion or unknown usage is the only non-zero stop.

Budget: ONE shared journal (default DEFAULT_JOURNAL, independent of --out) plus a read-only import
of earlier journals so prior spend counts. Caps apply to the combined total. Unknown usage stops the
run and is never retried.

--dry-run uses onlyfans_runtime.native_planning.SyntheticNativeModel with a canned decision per case
and a fabricated in-memory snapshot. It needs no database, credential or journal.
"""
import argparse,asyncio,difflib,glob,json,os,re,sys
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from .. import writer
from ..parts import plan
from ..runtime import snapshot,validate_decision,configuration
from ..configuration import ActionBlocked

ACCOUNT='of-312988353'
FAN=318022380
LIVE_PRODUCT='moxy-37ce6f1b28be64a23db6ca4b'      # the $75 card actually sent on 3 Nov

# Combined caps over this run PLUS every imported earlier journal (operator to confirm the numbers).
MAX_CALLS_TOTAL=90
MAX_MILLICENTS_TOTAL=10000
DEFAULT_JOURNAL='/var/lib/twin-release/moxy-qualification/budget.json'
IMPORT_GLOBS=['/var/lib/twin-release/moxy-gervonta-repair-*/qual*/**/budget.json',
              '/var/lib/twin-release/moxy-behaviour-qualification-20261103/**/budget.json']

COMMERCIAL={'invite','offer','resend','alternative','continue','correct','fulfil'}


# ---------------------------------------------------------------- budget journal
def _read_entries(path):
    try:data=json.loads(Path(path).read_text())
    except (OSError,ValueError) as exc:raise SystemExit('Unreadable budget journal %s (%s): stop.'%(path,type(exc).__name__))
    if isinstance(data,dict) and isinstance(data.get('entries'),list):data=data['entries']
    if not isinstance(data,list) or any(not isinstance(e,dict) or 'actual' not in e for e in data):
        raise SystemExit('Unrecognised budget journal format %s: stop (fail closed).'%path)
    return data


class Budget:
    """One shared journal, locked against concurrent runs, plus read-only imported totals."""
    def __init__(self,path,globs=IMPORT_GLOBS):
        import fcntl
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self._lock=(self.path.parent/(self.path.name+'.lock')).open('w')
        try:fcntl.flock(self._lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise SystemExit('Another qualification run holds the shared budget journal: stop.')
        self.entries=_read_entries(self.path) if self.path.exists() else []
        self.imported=[];self.imported_files=[]
        own=self.path.resolve()
        for pattern in globs:
            for name in sorted(glob.glob(pattern,recursive=True)):
                if Path(name).resolve()==own or name in self.imported_files:continue
                self.imported_files.append(name);self.imported+=_read_entries(name)
        if any(e.get('actual') is None for e in self.entries+self.imported):
            raise SystemExit('Unknown prior usage in a budget journal: stop.')

    def totals(self):
        every=self.entries+self.imported
        return len(every),sum(e['actual'] for e in every)

    def check(self,reserve):
        calls,spent=self.totals()
        if calls+1>MAX_CALLS_TOTAL or spent+reserve>MAX_MILLICENTS_TOTAL:
            raise SystemExit('Qualification budget exhausted (combined %d calls, %d millicents; caps %d/%d).'
                %(calls,spent,MAX_CALLS_TOTAL,MAX_MILLICENTS_TOTAL))

    def save(self):
        tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(self.entries,indent=2));tmp.replace(self.path)


# ---------------------------------------------------------------- synthetic history helpers
def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Built(SimpleNamespace):
    """What a case contributes: ordered synthetic history, in-memory overrides and expectations."""


def msg(direction,text,**kw):
    return (direction,text,kw)


def eligible(ctx,fan_state,category=None,price=None,exclude=()):
    """Inventory keys the real authority would let us offer to this fan right now, in stable order."""
    keys=[]
    for key,p in sorted(ctx.base['products'].items()):
        if key in exclude or (category and p['category']!=category) or (price and p['price_cents']!=price):continue
        try:ctx.pa.terms(key,fan_state)
        except Exception:continue
        keys.append(key)
    return keys


def paid_assets(ctx,key):
    terms=ctx.base['inventory'][key]
    return sorted(set(terms['media_ids'])-set(terms['preview_ids']))


def pick(ctx,fan_state,categories,exclude=()):
    for category in categories:
        found=eligible(ctx,fan_state,category,exclude=exclude)
        if found:return found[0]
    return None


def skip(reason):
    return Built(skip=reason)


# ---------------------------------------------------------------- cases
# Each case: build(ctx)->Built, canned(ctx,built)->decision (dry-run), optional canned_bad.
def _dec(action,text,product_id=None,**extra):
    return {'action':action,'text':text,'product_id':product_id,'sequence':None,'flags':[],
        'reason':'DRY-RUN canned decision; not a model result.','permission':None,'question':None,
        'resolve_obligation':None,**extra}


def _latest_in(built):
    return [h for h in built.history if h['direction']=='in'][-1]


def _first_eligible(ctx,built,cats=('covered_booty','bikini_lingerie_booty','covered_boobs'),exclude=()):
    fs=dict(ctx.base['fan_state'],**built.fan_state)
    return pick(ctx,fs,cats,exclude=set(exclude)|set(built.prior_products))


def _b(history,*,fan_state=None,invitation=None,prior=(),allow=(),note='',**expect):
    return Built(skip=None,history_spec=history,fan_state=fan_state or {},invitation=invitation,
        prior_products=set(prior),allow_products=set(allow),expect=expect,note=note)


CAMPING=[msg('in','Come camping with me'),
    msg('out',"camping sounds cute but you'd have to carry me and the snacks the whole way \U0001f602 what's the plan, tent or roughing it?"),
    msg('in','I like it rough'),
    msg('out',"rough is cute until you've gotta set up the tent in the dark \U0001f602 but noted, I like a challenge"),
    msg('in','I meant a different kind of rough \U0001fae2')]


def build_camping(ctx):
    return _b(CAMPING,actions={'reply','invite','offer','alternative'},no_tent=True,no_name_question=True,
        note='Replay of the real 3 Nov exchange (texts copied from the incident record).')


def canned_camping(ctx,built):
    key=_first_eligible(ctx,built)
    return _dec('invite',"ohh you meant THAT kind of rough \U0001f60f i like where your head's at. want to see something cheeky?",key,
        messages=[{'text':"ohh you meant THAT kind of rough \U0001f60f i like where your head's at. want to see something cheeky?",'media':False}])


def bad_camping(ctx,built):
    return _dec('reply',"haha so tent or roughing it? also what's your name btw — I never asked")


def _unpaid_card(ctx):
    key=LIVE_PRODUCT if LIVE_PRODUCT in ctx.base['products'] else next(
        (k for k,p in sorted(ctx.base['products'].items()) if p['price_cents']==7500),None)
    return key


def _unpaid_base(ctx):
    key=_unpaid_card(ctx)
    if key is None:return None,None,None
    fs=dict(ctx.base['fan_state'])
    over={'pending_products':sorted(set(fs.get('pending_products',[]))|{key}),
        'previously_sent_assets':sorted(set(fs.get('previously_sent_assets',[]))|set(paid_assets(ctx,key)))}
    card=msg('out',"can't do explicit pussy content, that's a hard no... bare ass, bent over, looking right at you. want it?",
        media_count=1,price_cents=7500)
    return key,over,card


def build_unpaid_a(ctx):
    key,over,card=_unpaid_base(ctx)
    if key is None:return skip('no $75 product in inventory for the unpaid-card case')
    return _b([card,msg('in','I bet your pussy so good baby')],fan_state=over,prior=[key],
        actions={'reply','invite','offer','alternative'},no_ownership=True,no_lecture=True,
        note='Card text reconstructed from the incident handoff (elided parts shown as ...). Card is unpaid: pending_products and previously_sent_assets set in memory.')


REAL_FALSE_REPLY="i know you do... you've already got my favorite in your hands. go look at it again"


def build_unpaid_b(ctx):
    key,over,card=_unpaid_base(ctx)
    if key is None:return skip('no $75 product in inventory for the unpaid-card case')
    return _b([card,msg('in','I bet your pussy so good baby'),msg('out',REAL_FALSE_REPLY),
        msg('in','Okay would you sukc me until I cum in your mouth')],fan_state=over,prior=[key],
        actions={'reply','invite','offer','alternative'},no_ownership=True,no_lecture=True,
        note='The earlier bot reply is the real false one from the incident (dash replaced by a full stop, ellipsis as in the handoff), kept so the model must not repeat or build on it.')


def build_correction(ctx):
    """The live correction path: same unpaid-card chat, the re-offer of the sent card was already rejected."""
    built=build_unpaid_b(ctx)
    if getattr(built,'skip',None):return built
    key=next(iter(built.prior_products))
    built.payload={'correction_of':'qualification-synthetic','rejected':{'reason':'media_previously_sent_requires_explicit_resend','action':'offer','product_id':key}}
    built.note='Correction attempt: the previous decision re-offered the already-sent card and was rejected; payload.rejected is set as the runtime does.'
    return built


def canned_unpaid_a(ctx,built):
    return _dec('reply',"mmm you're bad \U0001f60f keep talking like that and see what happens")


def bad_unpaid_a(ctx,built):
    return _dec('reply',"i know you do — you've already got my favorite in your hands, go look at it again",None)


def canned_unpaid_b(ctx,built):
    key=_first_eligible(ctx,built)
    text="haha easy tiger, not that far today \U0001f60f but this one's a cheeky tease for you"
    return _dec('alternative',text,key,messages=[{'text':text,'media':True}])


def bad_unpaid_b(ctx,built):
    key=next(iter(built.prior_products))
    text="you know you want it, here it is again"
    return _dec('offer',text,key,request_kind='direct',request_quote='Okay would you sukc me',messages=[{'text':text,'media':True}])


def build_direct(ctx):
    return _b([],actions={'offer'},product_required=True,note='Single fan message.')


def canned_direct(ctx,built):
    key=_first_eligible(ctx,built,('bikini_lingerie_booty','covered_booty'))
    text="got you, this is a cute one \U0001f60f"
    return _dec('offer',text,key,request_kind='direct',request_quote='outfit photo I can buy',messages=[{'text':text,'media':True}])


def build_accept(ctx):
    fs=dict(ctx.base['fan_state'])
    key=pick(ctx,fs,['covered_booty','bikini_lingerie_booty'])
    if key is None:return skip('no eligible covered_booty/bikini product for an invitation')
    inv={'product_id':key,'fingerprint':ctx.pa.fingerprint,'sequence':None}
    return _b([msg('out','Want this covered outfit photograph?')],invitation=inv,allow=[key],
        actions={'offer'},product_required=True,note='Invitation retained for the product shown in the review.')


def canned_accept(ctx,built):
    key=built.invitation['product_id'];text="yay, here you go \U0001f60f"
    return _dec('offer',text,key,request_kind='accept',request_quote='send that photo',messages=[{'text':text,'media':True}])


def build_purchase(ctx):
    fs=dict(ctx.base['fan_state'])
    found=eligible(ctx,fs,'covered_booty')
    if len(found)<2:
        found=eligible(ctx,fs,'bikini_lingerie_booty') if len(found)==0 else found+[k for k in eligible(ctx,fs,'bikini_lingerie_booty')]
    if len(found)<2:return skip('fewer than two distinct eligible photos for purchase continuation')
    key=found[0]
    over={'purchased_products':sorted(set(fs.get('purchased_products',[]))|{key}),
          'owned_assets':sorted(set(fs.get('owned_assets',[]))|set(ctx.base['inventory'][key]['media_ids']))}
    card=msg('out','The selected covered outfit photograph.',media_count=1,price_cents=ctx.base['products'][key]['price_cents'])
    return _b([card],fan_state=over,prior=[key],actions={'offer','invite'},product_required=True,allow_ownership=True,
        note='SYNTHETIC in-memory verified purchase; no financial write.')


def canned_purchase(ctx,built):
    key=_first_eligible(ctx,built);text="glad you liked it \U0001f60f here's another one for you"
    return _dec('offer',text,key,request_kind='direct',request_quote='another different outfit photo',messages=[{'text':text,'media':True}])


def build_single(ctx):
    return _b([],actions={'reply'},no_product=True,single_short=True)


def canned_single(ctx,built):
    return _dec('reply',"haha thank you \U0001f602 gym was good, legs are dead though. how was your day?")


def build_multipart(ctx):
    return _b([],actions={'offer','invite','reply','alternative'},record_parts=True,
        note='Record whether the model splits into bubbles and which bubble carries the media.')


def canned_multipart(ctx,built):
    key=_first_eligible(ctx,built)
    a="long day huh? come here \U0001f60f";b="this one should cheer you up"
    return _dec('offer',a,key,request_kind='direct',request_quote='send me something cheeky',messages=[{'text':a,'media':False},{'text':b,'media':True}])


def _obs(built,kind,quote):
    m=_latest_in(built)
    return [{'kind':kind,'active':True,'message_id':m['id'],'quote':quote,'product_id':None}]


def build_no_offers(ctx):
    return _b([],actions={'reply','none'},no_product=True,obs_ordinary=True)


def canned_no_offers(ctx,built):
    return _dec('reply',"deal, just chatting \U0001f60a what's been the best part of your week?",observations=_obs(built,'ordinary_chat_only','no offers please'))


def build_tier(ctx):
    return _b([],actions={'reply','invite','offer','alternative'},restricted=True,no_lecture=True,
        note='Fan is the real non-VIP; the requested category is prohibited/VIP-restricted in Amelia rules.')


def canned_tier(ctx,built):
    key=_first_eligible(ctx,built);text="haha that one's not happening baby \U0001f60f but I've got something cheeky for you"
    return _dec('alternative',text,key,messages=[{'text':text,'media':True}])


def build_complaint(ctx):
    return _b([],actions={'reply','handover'},no_new_sale=True)


def canned_complaint(ctx,built):
    return _dec('handover','',flags=['complaint'])


def build_affection(ctx):
    return _b([],actions={'reply','invite','offer'},no_affection_obs=True)


def canned_affection(ctx,built):
    return _dec('reply',"aww you're sweet \U0001f618 how's your day going?")


def build_ordinary(ctx):
    return _b([],actions={'reply','none'},obs_ordinary=True)


def canned_ordinary(ctx,built):
    return _dec('reply',"sure, just chatting \U0001f60a what are you up to?",observations=_obs(built,'ordinary_chat_only','no offers please'))


def build_repeat(ctx):
    return _b([msg('out','haha easy there, we just met. How did training go?')],actions={'reply','invite','offer'},no_restart=True)


def canned_repeat(ctx,built):
    return _dec('reply',"nice, did you smash it? I'm about to hit legs \U0001f4aa")


# name, group, fan message (None = the history already ends with the fan message), build, canned, canned_bad
CASES=[
 ('camping_clarification','camping_clarification',None,build_camping,canned_camping,bad_camping),
 ('unpaid_card_followups_a','unpaid_card_followups',None,build_unpaid_a,canned_unpaid_a,bad_unpaid_a),
 ('unpaid_card_followups_b','unpaid_card_followups',None,build_unpaid_b,canned_unpaid_b,bad_unpaid_b),
 ('direct_request','direct_request','Have you got an outfit photo I can buy?',build_direct,canned_direct,None),
 ('accepted_invitation','accepted_invitation','Yes please, send that photo.',build_accept,canned_accept,None),
 ('purchase_continuation','purchase_continuation','That was great. Have you got another different outfit photo?',build_purchase,canned_purchase,None),
 ('single_reply','single_reply','haha you\'re funny, how was the gym today?',build_single,canned_single,None),
 ('multipart_offer','multipart_offer',"send me something cheeky, I've had a long day",build_multipart,canned_multipart,None),
 ('no_offers','no_offers','Just talk for now, no offers please.',build_no_offers,canned_no_offers,None),
 ('tier_restriction','tier_restriction','send me a full nude b/g video',build_tier,canned_tier,None),
 ('complaint','complaint','I paid for the last photo but it will not open. Can you check?',build_complaint,canned_complaint,None),
 ('affection','affection','Sup baby, I love you',build_affection,canned_affection,None),
 ('ordinary_chat','ordinary_chat','Just talk for now, no offers please.',build_ordinary,canned_ordinary,None),
 ('repeat_no_restart','repeat_no_restart','Finished training. What photos have you got?',build_repeat,canned_repeat,None),
 ('correction_after_resend_block','unpaid_card_followups',None,build_correction,canned_unpaid_a,None),
]
GROUPS={c[1] for c in CASES}|{c[0] for c in CASES}


def select_cases(only):
    if not only:return list(CASES)
    wanted=[x.strip() for x in only.split(',') if x.strip()]
    unknown=[w for w in wanted if w not in GROUPS]
    if unknown:raise SystemExit('Unknown case(s): %s. Known: %s'%(', '.join(unknown),', '.join(sorted(GROUPS))))
    return [c for c in CASES if c[0] in wanted or c[1] in wanted]


def assemble(ctx,case):
    """Deep-copy the base snapshot and inject the synthetic scenario. Returns (snap,built) or (None,built)."""
    name,group,fan_text,build,canned,bad=case
    built=build(ctx)
    if getattr(built,'skip',None):return None,built
    spec=list(built.history_spec)
    if fan_text is not None:spec.append(msg('in',fan_text))
    now=_utcnow();n=len(spec);items=[]
    for i,(direction,text,kw) in enumerate(spec):
        items.append({'id':9000000001+i,'direction':direction,'text':text,
            'at':(now-timedelta(seconds=90*(n-i))).isoformat(),
            'sender':'fan' if direction=='in' else 'ai','attachment_description':kw.get('attachment_description'),
            'media_count':kw.get('media_count',0),'price_cents':kw.get('price_cents',0)})
    built.history=items
    snap=deepcopy(ctx.base)
    snap['history']=items
    snap['fan_state'].update(deepcopy(built.fan_state))
    snap['invitation']=deepcopy(built.invitation)
    snap['event']={'kind':'turn','payload':{'qualification_only':True,**deepcopy(getattr(built,'payload',None) or {})}}
    built.commercial_state='unavailable'
    try:
        from ..state import commercial_state
    except ImportError:
        pass
    else:
        try:
            snap['commercial_state']=commercial_state(snap);built.commercial_state='applied'
        except Exception as exc:
            built.commercial_state='error:%s'%type(exc).__name__
            built.skip='commercial_state raised %s: %s (no model call made)'%(type(exc).__name__,exc)
            return None,built
    return snap,built


# ---------------------------------------------------------------- screens
DASH=re.compile('[–—]')
OWN=re.compile(r"\b(already|alr)\s+(got|have|has|own|bought|saw|seen)|\byou('ve|\s+have|\s+already)\s+(got|seen|had|bought|own|looked)|in your hands|\byou\s+(own|have|got|saw)\s+(it|that|this|my|the)\b|\bgo\s+(look|check)\b|look(ed)?\s+at\s+it\s+again|\byou('ve)?\s+(seen|unlocked|opened)\b",re.I)
NAMEQ=re.compile(r"\byour\s+name\b|\bwhat'?s\s+your\s+name|\bwho\s+are\s+you\b|\bnice\s+to\s+meet\s+you\b|\bwe\s+just\s+met\b",re.I)
TENT=re.compile(r"\btents?\b|\bcamp(ing|s)?\b|roughing\s+it|\bsnacks?\b",re.I)
LECTURE=re.compile(r"\b(rules?|policy|policies|guidelines|not\s+allowed|terms\s+of|platform|prohibited|against\s+the)\b",re.I)


def norm(text):
    return ' '.join(re.sub(r'[^\w\s]','',str(text).lower()).split())


def raw_strings(raw):
    try:
        value=json.loads(raw);found=[]
        def walk(v):
            if isinstance(v,str):found.append(v)
            elif isinstance(v,list):[walk(x) for x in v]
            elif isinstance(v,dict):[walk(x) for x in v.values()]
        walk(value);return found
    except (TypeError,ValueError):return [raw or '']


def restricted_reason(ctx,fan_state,key):
    p=ctx.base['products'].get(key)
    if not p:return 'unknown product'
    try:
        if p['category'] in ctx.cfg.resolve('prohibited_categories',fan_state).value:return 'category %s is prohibited'%p['category']
        rule=ctx.cfg.resolve('product:'+p['category'],fan_state).value
        for field,expected in (rule.get('requires',{}) if isinstance(rule,dict) else {}).items():
            if fan_state.get(field)!=expected:return 'category %s needs %s=%s (fan has %s)'%(p['category'],field,expected,fan_state.get(field))
    except ActionBlocked as exc:return 'category %s restricted (%s)'%(p['category'],exc)
    return None


def run_screens(ctx,snap,built,rec):
    """Screens flag things for the human; none of them is a verdict."""
    out=[];exp=built.expect;d=rec['decision'];parts=rec['parts']
    texts=[p['text'] for p in parts]
    def add(name,flag,detail='',applies=True):
        out.append({'screen':name,'status':'n/a' if not applies else ('FLAG' if flag else 'ok'),'detail':detail if (flag and applies) else ''})
    add('validation_error',bool(rec['error']),rec['error'] or '')
    add('action_label',bool(d) and d['action'] not in exp.get('actions',set(COMMERCIAL)|{'reply','none','handover'}),
        'action %s not in expected %s'%(d and d['action'],sorted(exp.get('actions',[]))),applies=d is not None and 'actions' in exp)
    hits=[s for s in raw_strings(rec['raw_output']) if DASH.search(s)] if rec['raw_output'] else []
    add('no_em_en_dash_in_model_output',bool(hits),'model emitted a dash (parser strips it before send): %r'%hits[:2],applies=bool(rec['raw_output']))
    ph=[t for t in texts if t and ('[' in t or ']' in t or 'placeholder' in t.lower() or 'paid card' in t.lower())]
    add('placeholder_or_bracket_text',bool(ph),'fan-facing text looks like a placeholder: %r'%ph[:2],applies=bool(texts))
    sent=set((snap.get('commercial_state') or {}).get('previously_sent_products') or [])
    add('offers_previously_sent_product',bool(d) and d.get('product_id') in sent,'re-offered %s'%(d and d.get('product_id')),applies=bool(d) and bool(sent))
    prior=[h for h in snap['history'] if h['direction']=='out']
    repeated=[]
    for t in texts:
        n=norm(t)
        for h in prior:
            hn=norm(h['text'])
            if n and hn and (n==hn or (min(len(n),len(hn))>=20 and (n in hn or hn in n)) or
                (min(len(n),len(hn))>=12 and difflib.SequenceMatcher(None,n,hn).ratio()>=0.88)):
                repeated.append('%r ~ %r'%(t[:50],h['text'][:50]));break
    add('repeats_recent_outgoing_line',bool(repeated),'; '.join(repeated),applies=bool(texts))
    if 'no_tent' in exp:
        found=[m.group(0) for t in texts for m in TENT.finditer(t)]
        add('camping_or_tent_wording',bool(found),'mentions %s (check it moves on rather than repeats)'%sorted(set(x.lower() for x in found)),applies=bool(texts))
    if 'no_name_question' in exp or 'no_restart' in exp:
        found=[t for t in texts if NAMEQ.search(t)]
        add('name_or_introduction_reset',bool(found),'; '.join(x[:60] for x in found),applies=bool(texts))
    if 'allow_ownership' not in exp:
        found=[t for t in texts if OWN.search(t)]
        add('ownership_or_viewing_claim',bool(found),'; '.join(x[:80] for x in found),applies=bool(texts))
    bad=set(snap['fan_state'].get('rejected_products',[]))|set(snap['fan_state'].get('pending_products',[]))|set(snap['fan_state'].get('purchased_products',[]))|built.prior_products
    bad-=built.allow_products
    key=d and d.get('product_id')
    add('product_already_sent_rejected_or_owned',bool(key and key in bad),'product %s'%key,applies=bool(key))
    if exp.get('no_product') or exp.get('no_new_sale'):
        add('no_product_expected',bool(key) or bool(d and d['action'] in COMMERCIAL),'action=%s product=%s'%(d and d['action'],key),applies=d is not None)
    if exp.get('product_required'):
        add('product_selected',not key,'no product selected',applies=d is not None)
    if exp.get('single_short'):
        total=sum(len(t) for t in texts)
        add('single_short_message',len(texts)!=1 or total>200,'%d parts, %d chars'%(len(texts),total),applies=d is not None)
    if exp.get('obs_ordinary'):
        has=bool(d) and any(o['kind']=='ordinary_chat_only' and o['active'] for o in d.get('observations',[]))
        add('ordinary_chat_only_observation',not has,'missing active ordinary_chat_only observation',applies=d is not None)
    if exp.get('no_affection_obs'):
        bad_obs=bool(d) and (any(o['active'] for o in d.get('observations',[])) or 'boundary_reset' in d['flags'])
        add('affection_not_treated_as_restriction',bad_obs,'active observation or boundary_reset flag',applies=d is not None)
    if exp.get('restricted'):
        reason=restricted_reason(ctx,snap['fan_state'],key) if key else None
        add('restricted_item_selected',bool(reason),reason or '',applies=bool(key))
    if exp.get('no_lecture'):
        found=[t for t in texts if LECTURE.search(t)]
        total=sum(len(t) for t in texts)
        add('lecture_heuristic',bool(found) or total>240,'rule/policy wording %r or %d chars'%([x[:60] for x in found],total),applies=bool(texts))
    if exp.get('no_restart'):
        found=[t for t in texts if norm(t).startswith('haha easy there')]
        add('restarts_conversation',bool(found),'; '.join(found),applies=bool(texts))
    return out


# ---------------------------------------------------------------- review output
def render(results,header):
    lines=['# Moxy qualification review','',*header,'',
        'Automatic screens are SCREENS ONLY. They are not a pass/fail verdict. A human fills in each `Verdict:` line.','']
    for r in results:
        lines+=['---','','## %s  rep %d'%(r['case'],r['repetition']+1),'']
        if r.get('skipped'):
            lines+=['SKIPPED (no model call, no cost): %s'%r['skipped'],'','Verdict: ','']
            continue
        lines.append('Context is SYNTHETIC (injected in memory for this evaluation; replaces the real history).')
        if r.get('note'):lines.append('Note: '+r['note'])
        lines+=['','### Synthetic context','']
        for h in r['context']:
            extra=' [card: media_count=%s price_cents=%s]'%(h['media_count'],h['price_cents']) if h['media_count'] or h['price_cents'] else ''
            lines.append('- (%s, id %s) %s%s'%(h['direction'],h['id'],h['text'].replace('\n',' / '),extra))
        if r['fan_state_overrides'] or r['invitation']:
            lines+=['','In-memory overrides: fan_state=%s invitation=%s'%(json.dumps(r['fan_state_overrides']),json.dumps(r['invitation']))]
        lines.append('commercial_state: %s'%r['commercial_state'])
        d=r['decision'];lines+=['','### Decision','']
        if d is None:lines.append('- none (error: %s)'%r['error'])
        else:
            lines+=['- action: %s'%d['action'],'- product_id: %s'%d['product_id'],'- sequence: %s'%d['sequence'],
                '- flags: %s'%d['flags'],'- question: %s'%d['question'],'- request_kind: %s'%d.get('request_kind','none'),
                '- request_quote: %s'%d.get('request_quote'),'- observations: %s'%json.dumps(d.get('observations',[])),
                '- reason: %s'%d['reason']]
        p=r['product']
        lines+=['','### Product (from inventory)','',
            '- %s'%('label=%r category=%s price_cents=%s'%(p['label'],p['category'],p['price_cents']) if p else 'none selected')]
        lines+=['','### Message parts','']
        if not r['parts']:lines.append('- (no message sent)')
        for i,m in enumerate(r['parts'],1):
            lines.append('%d. %s  %r'%(i,'[MEDIA CARD HERE]' if m['media'] else '[text only]',m['text']))
        lines+=['','### Cost','','- %s millicents%s'%(r['actual_millicents'],' (dry-run synthetic)' if r['dry_run'] else '')]
        lines+=['','### Automatic screens (not a verdict)','']
        for s in r['screens']:
            lines.append('- [%s] %s%s'%(s['status'],s['screen'],(': '+s['detail']) if s['detail'] else ''))
        if r['error']:lines.append('- error: %s'%r['error'])
        lines+=['','Verdict: ','']
    return '\n'.join(lines)


def summary(results):
    rows=[('case','rep','action','product','flags','millicents')]
    for r in results:
        if r.get('skipped'):rows.append((r['case'],str(r['repetition']+1),'SKIPPED','-','-','0'));continue
        d=r['decision'];p=r['product']
        flagged=[s['screen'] for s in r['screens'] if s['status']=='FLAG']
        rows.append((r['case'],str(r['repetition']+1),d['action'] if d else 'ERROR',
            ('%s/%s'%(p['category'],p['price_cents'])) if p else '-',
            ','.join(flagged) or 'none',str(r['actual_millicents'])))
    widths=[max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return '\n'.join('  '.join(c.ljust(w) for c,w in zip(row,widths)) for row in rows)


# ---------------------------------------------------------------- dry-run fabrication
def fabricate():
    """In-memory settings and snapshot shaped like runtime.snapshot, no database."""
    from ..configuration import load
    from ..examples import references
    cfg=load('amelia')
    spec=[(LIVE_PRODUCT,'nude_booty',7500,'Nude booty, bent over'),
          ('dry-covered-booty-1','covered_booty',3000,'Covered booty, black leggings'),
          ('dry-covered-booty-2','covered_booty',3000,'Covered booty, grey shorts'),
          ('dry-bikini-1','bikini_lingerie_booty',3500,'Bikini, side view'),
          ('dry-covered-boobs-1','covered_boobs',3000,'Sports bra covered boobs'),
          ('dry-feet-1','feet_photos',2000,'Feet on the couch')]
    products=[];inventory={}
    for i,(key,category,cents,label) in enumerate(spec):
        mid=7000+i;fact='DRY-RUN fixture description: %s'%label
        products.append({'id':key,'revision':1,'category':category,'price_cents':cents,'currency':'USD',
            'assets':[{'id':mid,'revision':1,'facts':[fact],'evidence_ref':'asset:dry-run-%d'%mid}],
            'source':{'document':'01_Amelia.pdf','pages':[2]},'approval_ref':'dry-run:fixture','available':True,
            'requires':{},'caption_instructions':'Accurate caption only.'})
        inventory[key]={'item':{'id':100+i,'label':label,'description_for_ai':fact,'price_cents':cents,'kind':'image_set'},
            'source':None,'media_ids':[mid],'preview_ids':[],'price_cents':cents,
            'files':[{'media_id':mid,'kind':'photo','description_basis':{},'operator_corrected':False}]}
    settings={'creator_key':'amelia','products':products,'catalogue_bindings':{},'sequences':{},
        'operational':{f:'dry-run' for f in cfg.creator['required_operational_fields']},'public_facts':{},'fixture':True}
    settings['operational']['assigned_operator']='dry-run'
    cfg,pa=configuration(settings)
    fan_state={'ownership_verified':True,'purchase_links_verified':True,'commerce_uncertain':False,'human_owned':False,
        'safety_hold':False,'financial_hardship':False,'ordinary_chat_only':False,'sales_refused':False,'boundary_hold':False,
        'no_push':False,'owned_assets':[],'previously_sent_assets':[],
        'paid_assets_by_product':{k:[products[i]['assets'][0]['id']] for i,(k,_,_,_) in enumerate(spec)},
        'exposure_history_verified':True,'purchased_products':[],'pending_products':[],'rejected_products':[],
        'budget_cents':None,'requested_resend_product':None,'vip':False,'spender':False}
    base={'fan_id':FAN,'pilot':None,'account_revision':1,'binding_id':'dry-run','configuration_fingerprint':pa.fingerprint,
        'instructions':cfg.public_context(),'public_facts':{},'fan_state':fan_state,'fan_revision':1,
        'control':{'revision':1,'input_revision':1,'mode':'automated','takeover':'none'},
        'event':{'kind':'turn','payload':{}},'history':[],'memory':{},'invitation':None,'obligations':[],
        'products':{p['id']:p for p in products},'inventory':inventory,'sequences':{},'operational':settings['operational'],
        'approved_remedies':{},'native_fingerprint':'dry-run','campaign':None}
    return SimpleNamespace(base=base,settings=settings,cfg=cfg,pa=pa,dry_run=True,chosen='dry-run/synthetic')


# ---------------------------------------------------------------- real path
def real_setup(args,out):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from db.models import AccountAiConfig
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_moxy_models import MoxyAccount
    from onlyfans_runtime.connections import _open
    from onlyfans_runtime.store import utcnow
    budget=Budget(args.journal)
    engine=create_engine(os.environ['DATABASE_URL'])
    with Session(engine) as s:
        # Snapshot takes shared consistency locks; ALL preparation is rolled back.
        from sqlalchemy import text
        s.execute(text("SET LOCAL statement_timeout='8s'"))
        row=s.get(OfProviderConnection,args.account)
        if not row or row.status!='verified' or row.expires_at<=utcnow():raise SystemExit('Account credential is not current.')
        clear=_open(row,'twin-internal');credential=clear['model'];expires=row.expires_at.replace(tzinfo=timezone.utc)
        cfg_row=s.get(AccountAiConfig,args.account)
        config={c.name:getattr(cfg_row,c.name) for c in cfg_row.__table__.columns}
        from automations._common import resolve_model_from_config
        chosen=resolve_model_from_config(cfg_row,'ai_chatter',account_id=args.account,require_explicit=True)
        event=SimpleNamespace(kind='turn',payload_json='{}')
        base=snapshot(s,args.account,args.fan,event)
        settings=json.loads(s.get(MoxyAccount,args.account).settings_json)
        s.rollback()
    # The known false historical flags are excluded ONLY from synthetic scenarios.
    # No fan facts, controls, purchases or holds are changed in the real database.
    base['fan_state'].update(ordinary_chat_only=False,sales_refused=False,boundary_hold=False)
    # EVALUATION ONLY, in memory: an unrelated unlinked ledger purchase can hold all commerce on the
    # account (known separate blocker). It is recorded here and lifted only inside this isolated snapshot
    # so the writer's commercial behaviour can be evaluated; live state and reconciliation are untouched.
    base['_eval_lifted_commerce_hold']=base['fan_state'].get('commerce_uncertain') is not False
    base['fan_state']['commerce_uncertain']=False
    base['invitation']=None
    cfg,pa=configuration(settings)
    return SimpleNamespace(base=base,settings=settings,cfg=cfg,pa=pa,dry_run=False,chosen=chosen,engine=engine,
        credential=credential,expires=expires,config=config,budget=budget,account=args.account,fan=args.fan)


def real_call(ctx,prompts,name,repetition):
    """Reserve, call, settle. Unknown usage stops the whole run; nothing is retried."""
    from sqlalchemy.orm import Session
    from onlyfans_runtime.provider_model import NativeProviderModel,MAX_OUTPUT_TOKENS
    from onlyfans_runtime.store import utcnow
    model=NativeProviderModel(account_id=ctx.account,model_id=ctx.credential['modelId'],
        api_key=ctx.credential['apiKey'],expires_at=ctx.expires)
    model.configure_qualification(account_id=ctx.account,chosen_model=ctx.chosen,config=ctx.config,messages=prompts)
    reserve=model._cost(len(json.dumps(prompts).encode())+4096,MAX_OUTPUT_TOKENS)
    ctx.budget.check(reserve)
    entry={'case':name,'repetition':repetition,'reserved':reserve,'actual':None,'at':_utcnow().isoformat()}
    ctx.budget.entries.append(entry);ctx.budget.save()
    with Session(ctx.engine) as s,s.begin():
        call,units,day=model.reserve(s,account_id=ctx.account,fan_id=ctx.fan,model=ctx.chosen,messages=prompts,now=utcnow())
        entry['call_id']=call.id
    ctx.budget.save()
    raw=None;error=None
    try:raw=asyncio.run(asyncio.wait_for(model.generate(prompts),timeout=45))
    except Exception as exc:error=type(exc).__name__
    with Session(ctx.engine) as s,s.begin():
        actual=model.settle(s,account_id=ctx.account,call_id=entry['call_id'],reserved_millicents=units,
            actual_millicents=model.reported_cost_millicents if model.completed else None,now=utcnow())
    entry['actual']=actual;ctx.budget.save()
    return raw,actual,error


def dry_call(ctx,prompts,raw_text):
    from onlyfans_runtime.native_planning import SyntheticNativeModel
    model=SyntheticNativeModel(raw_output=raw_text)
    raw=asyncio.run(model.generate(prompts))
    return raw,model.reported_cost_millicents,None


# ---------------------------------------------------------------- one case
def run_case(ctx,case,repetition):
    name=case[0]
    snap,built=assemble(ctx,case)
    base_rec={'case':name,'repetition':repetition,'dry_run':ctx.dry_run}
    if snap is None:return {**base_rec,'skipped':built.skip}
    prompts=writer.messages(snap)
    if ctx.dry_run:
        canned=case[5] if (repetition%2==1 and case[5]) else case[4]
        raw,actual,error=dry_call(ctx,prompts,json.dumps(canned(ctx,built),ensure_ascii=False))
    else:
        raw,actual,error=real_call(ctx,prompts,name,repetition)
    parsed=None;parts=[]
    if actual is not None and raw:
        try:
            parsed=writer.parse(raw)
            validate_decision(ctx.settings,snap,parsed)
        except Exception as exc:error=str(exc)
    if parsed:
        try:parts=[{'text':m['text'],'media':m['media']} for m in plan(parsed)]
        except Exception as exc:error=error or str(exc)
    key=parsed.get('product_id') if parsed else None
    product=None
    if key and key in snap['products']:
        product={'id':key,'label':snap['inventory'][key]['item']['label'],'category':snap['products'][key]['category'],
            'price_cents':snap['products'][key]['price_cents']}
    rec={**base_rec,'note':built.note,'context':built.history,'fan_state_overrides':built.fan_state,'invitation':built.invitation,
        'commercial_state':built.commercial_state,'decision':parsed,'product':product,'parts':parts,'raw_output':raw,
        'error':error,'actual_millicents':actual,'real_inventory_products':len(ctx.base['products'])}
    rec['screens']=run_screens(ctx,snap,built,rec)
    return rec


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--account',required=True);parser.add_argument('--fan',required=True,type=int)
    parser.add_argument('--out',required=True)
    parser.add_argument('--only',help='comma list of case names or groups (e.g. unpaid_card_followups)')
    parser.add_argument('--reps',type=int,default=1)
    parser.add_argument('--journal',default=DEFAULT_JOURNAL,help='shared budget journal, independent of --out')
    parser.add_argument('--dry-run',action='store_true',help='offline: canned decisions, fabricated snapshot, no journal')
    args=parser.parse_args(argv)
    if args.account!=ACCOUNT or args.fan!=FAN:
        raise SystemExit('This qualification is authorised only for Amelia/Gervonta.')
    if args.reps<1 or args.reps>5:raise SystemExit('--reps must be 1..5')
    cases=select_cases(args.only)
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    ctx=fabricate() if args.dry_run else real_setup(args,out)
    results=[];stop=None
    def header():
        h=['Mode: %s'%('DRY-RUN (synthetic model, fabricated snapshot, no journal)' if ctx.dry_run else 'REAL MODEL'),
           'Pair: %s / %s'%(ACCOUNT,FAN),'Model: %s'%ctx.chosen,'Run at: %s UTC'%_utcnow().isoformat(timespec='seconds')]
        if not ctx.dry_run:
            calls,spent=ctx.budget.totals()
            h.append('Shared budget so far: %d calls, %d millicents of caps %d/%d (journal %s; imported %d earlier journal files)'
                %(calls,spent,MAX_CALLS_TOTAL,MAX_MILLICENTS_TOTAL,ctx.budget.path,len(ctx.budget.imported_files)))
        return h
    def flush():
        (out/'results.json').write_text(json.dumps(results,indent=2,default=str))
        (out/'review.md').write_text(render(results,header()))
    try:
        for repetition in range(args.reps):
            for case in cases:
                rec=run_case(ctx,case,repetition)
                results.append(rec);flush()
                if rec.get('skipped'):print('%s rep %d SKIPPED: %s'%(case[0],repetition+1,rec['skipped']),flush=True)
                elif rec['actual_millicents'] is None:
                    stop='Qualification usage unknown; no retry, no fan event created.';break
            if stop:break
    except SystemExit as exc:
        stop=str(exc.code) if exc.code else 'stopped'
    finally:
        flush()
    print(summary(results))
    print('\nreview: %s'%(out/'review.md'))
    if stop:raise SystemExit(stop)
    return 0


if __name__=='__main__':main()
