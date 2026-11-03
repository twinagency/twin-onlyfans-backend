"""Backend fan-personality simulation against the Moxy writer: scripted fans, real configured model.

Each persona is a fixed script of five fan messages (the SAME script is used for every version and creator,
so before/after is a fair comparison). The bot answers every fan message through the real writer prompt,
real parse and real validation. History, cards and purchases are kept in memory only.

Isolation: the account snapshot is read once and rolled back (like qualify.py); no fan events are created,
nothing is sent, nothing is written to any live fan record. Real-model calls use the shared budget journal.

  --version BEFORE  = the code as installed (no correction retry, no emoji dedupe, no commercial_state)
  --version AFTER   = adds what the repaired runtime does around the model: emoji dedupe, commercial_state,
                      and ONE bounded correction attempt for a recoverable refusal or unusable output.
"""
import argparse,json,re,sys
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from . import qualify as Q
from .. import writer
from ..parts import plan
from ..runtime import validate_decision
from ..configuration import ActionBlocked

COMMERCIAL=Q.COMMERCIAL


# ------------------------------------------------------------------ personas (identical for every run)
# A turn is a string, or (text_if_previous_bot_reply_had_a_card, text_otherwise).
PERSONAS=[
 dict(name='Mike',persona='Direct buyer: asks for photos, unlocks what he is sent',buys=True,turns=[
    'hey beautiful',
    'got any photos I can buy?',
    ('unlocked it, wow. what else you got?','so what photos do you have'),
    ('something different from that one','send me one babe'),
    ('ill take another one','ok how much is the cheapest')]),
 dict(name='Dan',persona='Shy chatter: wants to talk, says no offers',buys=False,turns=[
    'hey, can we just chat for a bit?',
    'had a rough day at work tbh',
    'just talk to me please, no offers',
    'what did you do today?',
    'thanks for listening babe']),
 dict(name='Chris',persona='Falling in love: affection, asks about meeting',buys=False,turns=[
    'hi gorgeous',
    'i think im falling for you',
    'do you love me too?',
    'would you meet me for coffee?',
    'ok then show me something']),
 dict(name='Tom',persona='Budget fan: says he only has $20',buys=False,turns=[
    'hey',
    'how much are your pics?',
    ('thats too much, i only have $20','thats too much, i only have $20'),
    'anything cheaper?',
    'ok maybe next week']),
 dict(name='Jake',persona='Explicit demander: pushy, gets bored fast',buys=False,turns=[
    'Show me your tits',
    'I want to see your pussy',
    'send it now',
    'youre boring',
    'ok what can you show me']),
 dict(name='Ben',persona='Complaint: paid but could not open it',buys=False,turns=[
    'hey i paid for your last photo but it wont open',
    'can you check?',
    'i want my money back',
    'ok thanks',
    'sorry for being rude']),
 dict(name='Sam',persona='Returning buyer: buys, wants something different',buys=True,turns=[
    'its me again, got anything new?',
    ('love it. what else?','what do you have today'),
    'something different from the last one',
    'do you have videos',
    'ill take it']),
 dict(name='Greg',persona='Innuendo fan (the camping case): clarifies what he meant',buys=False,turns=[
    'Come camping with me',
    'I like it rough',
    'I meant a different kind of rough',
    'yeah',
    'so what do you have for me']),
 dict(name='Luke',persona='Lifestyle chatter: small talk, then asks to see something',buys=True,turns=[
    'just got home from work, what are you up to tonight',
    'you always look so good',
    'got anything to show me?',
    ('nice, got more?','how much is that'),
    ('ill take that one too','ok send it')]),
 dict(name='Ryan',persona='Freebie seeker: asks for a free pic first',buys=False,turns=[
    'send me a free pic first',
    'just one free one pls',
    'come on babe, then ill buy',
    'ok how much for the cheapest',
    'fine ill think about it']),
]

EMOJI=re.compile('(?:[\U0001F000-\U0001FAFF]|[☀-➿])️?')
DASH=re.compile('[–—]')


def words(t):return len(t.split())


def neutral_base(ctx):
    base=deepcopy(ctx.base)
    fs=base['fan_state']
    # A brand-new fan: none of the real fan's purchases, exposure, memory or holds travel into the simulation.
    fs.update(purchased_products=[],pending_products=[],owned_assets=[],previously_sent_assets=[],rejected_products=[],
              ordinary_chat_only=False,sales_refused=False,boundary_hold=False,financial_hardship=False,
              human_owned=False,safety_hold=False,no_push=False,commerce_uncertain=False)
    fs.pop('constraint_evidence',None)
    base['memory']={}
    base['obligations']=[]
    base['invitation']=None
    return base


def flags_for(text,prior_outs,own_prior_emoji):
    out=[]
    if DASH.search(text):out.append('dash')
    emo={e.rstrip('️') for e in EMOJI.findall(text)}
    if emo & own_prior_emoji:out.append('repeated emoji')
    n=Q.norm(text)
    if n and any(n==Q.norm(p) for p in prior_outs):out.append('repeated line')
    if words(text)>25:out.append('long (%d words)'%words(text))
    if Q.OWN.search(text):out.append('ownership claim')
    if Q.NAMEQ.search(text):out.append('name reset')
    return out


def decide(ctx,snap,version,label,k):
    """One bot turn: real call, parse, (AFTER: dedupe), validate. AFTER may take ONE correction attempt."""
    after=version=='AFTER'
    attempt=0;rejected=None;corrected=None
    while True:
        s=deepcopy(snap)
        if rejected:
            s['event']['payload'].update(correction_of='simulation',rejected=rejected)
        prompts=writer.messages(s)
        raw,actual,error=Q.real_call(ctx,prompts,label,k*2+attempt)
        if actual is None:
            return dict(ok=False,error='model usage unknown',parsed=None,corrected=corrected,cost=0)
        parsed=None;reason=error
        if raw and not error:
            try:
                parsed=writer.parse(raw)
                if after and hasattr(writer,'dedupe_emoji'):
                    parsed=writer.dedupe_emoji(parsed,s['history'])
                validate_decision(ctx.settings,s,parsed)
            except Exception as exc:
                reason=str(exc);parsed_failed=parsed;parsed=None
                last=parsed_failed
        if parsed is not None:
            return dict(ok=True,parsed=parsed,error=None,corrected=corrected,snap=s)
        # a refusal / unusable output
        if after and attempt==0:
            from .. import runtime as R
            recoverable=getattr(R,'recoverable',lambda r:False)(reason) or reason in getattr(R,'FORMAT_REASONS',())
            if recoverable:
                prior=locals().get('last')
                rejected={'reason':reason[:120],'action':prior.get('action') if isinstance(prior,dict) else None,
                          'product_id':prior.get('product_id') if isinstance(prior,dict) else None}
                corrected=reason;attempt=1;continue
        return dict(ok=False,error=reason,parsed=None,corrected=corrected,snap=s)


def run_conversation(ctx,persona,version,creator_label):
    base=neutral_base(ctx)
    fs=deepcopy(base['fan_state']);invitation=None
    history=[];msgs=[];metrics=dict(fan_turns=0,creator_msgs=0,words=0,cards=0,no_reply=0,dash=0,rep_emoji=0,rep_line=0,
                                    long=0,own=0,name=0,corrections=0,escalated=0,cost=0)
    now=Q._utcnow()-timedelta(minutes=60)
    mid=[9100000000+hash((persona['name'],version,creator_label))%1000000*100]
    def nid():mid[0]+=1;return mid[0]
    had_card=False;last_card=None
    for k,turn in enumerate(persona['turns']):
        text=turn if isinstance(turn,str) else (turn[0] if had_card else turn[1])
        # a buyer unlocks the card he was just sent
        if had_card and persona['buys'] and last_card:
            key=last_card['key']
            fs['purchased_products']=sorted(set(fs['purchased_products'])|{key})
            fs['owned_assets']=sorted(set(fs['owned_assets'])|set(Q.paid_assets(ctx,key)))
            fs['pending_products']=[p for p in fs['pending_products'] if p!=key]
            if version=='AFTER':
                for m in history:
                    if m['id']==last_card['id']:m['paid']=True
            msgs[last_card['msg_index']]['note']=((msgs[last_card['msg_index']].get('note') or '')+' | fan unlocked this card').strip(' |')
            had_card=False
        history.append({'id':nid(),'direction':'in','text':text,'at':(now+timedelta(seconds=90*len(history))).isoformat(),
            'sender':'fan','attachment_description':None,'media_count':0,'price_cents':0})
        msgs.append({'from':'fan','text':text,'card':None,'flags':[],'note':''})
        metrics['fan_turns']+=1
        snap=deepcopy(base)
        snap['history']=deepcopy(history);snap['fan_state']=deepcopy(fs);snap['invitation']=deepcopy(invitation)
        snap['event']={'kind':'turn','payload':{'qualification_only':True,'source_message_id':history[-1]['id']}}
        if version=='AFTER':
            try:
                from ..state import commercial_state
                snap['commercial_state']=commercial_state(snap)
            except Exception:pass
        else:
            snap.pop('commercial_state',None)
        res=decide(ctx,snap,version,'%s/%s/%s'%(creator_label,version,persona['name']),k)
        if res.get('corrected'):metrics['corrections']+=1
        if not res['ok']:
            metrics['no_reply']+=1
            if version=='AFTER':metrics['escalated']+=1
            msgs.append({'from':'creator','text':'(no reply sent: %s)'%(res['error'] or 'held')[:90],'card':None,
                         'flags':['held, fan gets silence' if version=='BEFORE' else 'held after retry, goes to operator'],'note':''})
            had_card=False
            continue
        parsed=res['parsed'];parts=plan(parsed);snapd=res['snap']
        key=parsed.get('product_id')
        prior_outs=[m['text'] for m in history if m['direction']=='out']
        prior_emo={e.rstrip('️') for m in history if m['direction']=='out'
                   for e in EMOJI.findall(m['text'])} if False else {e.rstrip('️') for m in history[-6:] if m['direction']=='out' for e in EMOJI.findall(m['text'])}
        had_card=False
        for i,part in enumerate(parts):
            card=None
            if part['media'] and key and key in snap['products']:
                price=snap['products'][key]['price_cents']
                card={'price_cents':price,'label':snap['inventory'][key]['item']['label']}
            fl=flags_for(part['text'],prior_outs,prior_emo)
            note=''
            if i==len(parts)-1 or i==0:
                note='decision: %s%s'%(parsed['action'],(' '+snap['products'][key]['category']+' $%.2f'%(snap['products'][key]['price_cents']/100)) if key and key in snap['products'] else '')
                if res.get('corrected'):note+=' (after one correction: %s)'%res['corrected'][:60]
                if i!=0 and not (i==len(parts)-1):note=''
            if i>0:note=note if i==len(parts)-1 and card else ''
            history.append({'id':nid(),'direction':'out','text':part['text'],
                'at':(now+timedelta(seconds=90*len(history)+30)).isoformat(),'sender':'ai','attachment_description':None,
                'media_count':1 if card else 0,'price_cents':card['price_cents'] if card else 0,
                **({'paid':False,'product_id':key} if (card and version=='AFTER') else {})})
            msgs.append({'from':'creator','text':part['text'],'card':card,'flags':fl,'note':note if (i==0 or card) else ''})
            metrics['creator_msgs']+=1;metrics['words']+=words(part['text'])
            for f in fl:
                if f=='dash':metrics['dash']+=1
                elif f=='repeated emoji':metrics['rep_emoji']+=1
                elif f=='repeated line':metrics['rep_line']+=1
                elif f.startswith('long'):metrics['long']+=1
                elif f=='ownership claim':metrics['own']+=1
                elif f=='name reset':metrics['name']+=1
            if card:
                metrics['cards']+=1;had_card=True
                last_card={'key':key,'id':history[-1]['id'],'msg_index':len(msgs)-1}
                fs['pending_products']=sorted(set(fs['pending_products'])|{key})
                fs['previously_sent_assets']=sorted(set(fs['previously_sent_assets'])|set(Q.paid_assets(ctx,key)))
        # the repaired runtime applies evidenced observations (e.g. a real "no offers" request) to fan state
        try:
            from ..observations import observations,apply
            fs=apply(fs,observations(parsed,snapd))
        except Exception:pass
    return dict(fan_name=persona['name'],persona=persona['persona'],messages=msgs,metrics=metrics)


def fabricate_cristina(ctx):
    """FIXTURE ONLY. Cristina has no Moxy account or catalogue on the live box, so there is no real inventory to read.
    Her real rule file is loaded; the products below are placeholders shaped to her sheet (4-photo bundles, $60+).
    They are labelled as fixtures in every report. The model call and its cost accounting still run through the
    already-configured Amelia connection (same model); nothing is read from or written to Cristina's account."""
    from ..configuration import load
    from ..runtime import configuration
    cfg=load('cristina')
    spec=[('fx-booty-1','booty_photos',6000,'Booty, black thong, 4 pictures',4),
          ('fx-booty-2','booty_photos',6000,'Booty, grey shorts, 4 pictures',4),
          ('fx-bikini-1','bikini_lingerie_booty',6500,'Bikini, side view',3),
          ('fx-bikini-2','bikini_lingerie_booty',6000,'Lingerie set, red',3),
          ('fx-boobs-1','boobs_photos',9000,'Boobs, covered, 4 pictures',4),
          ('fx-nude-booty-1','nude_booty',7000,'Bare booty, bent over',3)]
    products=[];inventory={};mid=8000;paid={}
    for i,(key,category,cents,label,n) in enumerate(spec):
        assets=[];files=[];ids=[]
        for j in range(n):
            mid+=1;ids.append(mid);fact='FIXTURE description (not a real vault item): %s, picture %d'%(label,j+1)
            assets.append({'id':mid,'revision':1,'facts':[fact],'evidence_ref':'asset:fixture-%d'%mid})
            files.append({'media_id':mid,'kind':'photo','description_basis':{},'operator_corrected':False})
        paid[key]=ids
        products.append({'id':key,'revision':1,'category':category,'price_cents':cents,'currency':'USD','assets':assets,
            'source':{'document':'03_Cristina.pdf','pages':[2]},'approval_ref':'fixture:cristina','available':True,
            'requires':{},'caption_instructions':'Accurate caption only.'})
        inventory[key]={'item':{'id':200+i,'label':label,'description_for_ai':label,'price_cents':cents,'kind':'image_set'},
            'source':None,'media_ids':ids,'preview_ids':[],'price_cents':cents,'files':files}
    settings={'creator_key':'cristina','products':products,'catalogue_bindings':{},'sequences':{},
        'operational':{f:'fixture' for f in cfg.creator['required_operational_fields']},'public_facts':{},'fixture':True}
    settings['operational']['assigned_operator']='fixture'
    cfg,pa=configuration(settings)
    fan_state=dict(ctx.base['fan_state']);fan_state['paid_assets_by_product']=paid
    base=deepcopy(ctx.base)
    base.update(pilot=None,instructions=cfg.public_context(),fan_state=fan_state,products={p['id']:p for p in products},
        inventory=inventory,sequences={},operational=settings['operational'],configuration_fingerprint=pa.fingerprint,
        approved_remedies={})
    ctx.base=base;ctx.settings=settings;ctx.cfg=cfg;ctx.pa=pa
    return ctx


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--account',required=True);p.add_argument('--fan',required=True,type=int)
    p.add_argument('--creator',required=True);p.add_argument('--version',required=True,choices=['BEFORE','AFTER'])
    p.add_argument('--out',required=True);p.add_argument('--journal',default=Q.DEFAULT_JOURNAL)
    p.add_argument('--personas',default='all')
    p.add_argument('--dry-run',action='store_true')
    args=p.parse_args(argv)
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    if args.dry_run:
        raise SystemExit('dry-run is not supported here: use qualify --dry-run for the harness and run this only on the real path')
    ctx=Q.real_setup(args,out)
    if args.creator.lower()=='cristina':ctx=fabricate_cristina(ctx)
    chosen=PERSONAS if args.personas=='all' else [PERSONAS[int(i)-1] for i in args.personas.split(',')]
    convs=[];stop=None
    print('%s %s: %d products (%s), model %s'%(args.creator,args.version,len(ctx.base['products']),'FIXTURE' if args.creator.lower()=='cristina' else 'real inventory',ctx.chosen),flush=True)
    try:
        for persona in chosen:
            c=run_conversation(ctx,persona,args.version,args.creator)
            convs.append(c)
            (out/'conversations.json').write_text(json.dumps(convs,indent=1,ensure_ascii=False))
            m=c['metrics']
            print('%-6s fan_turns=%d creator_msgs=%d cards=%d no_reply=%d dash=%d rep_emoji=%d rep_line=%d corr=%d'%(
                persona['name'],m['fan_turns'],m['creator_msgs'],m['cards'],m['no_reply'],m['dash'],m['rep_emoji'],m['rep_line'],m['corrections']),flush=True)
    except SystemExit as exc:
        stop=str(exc.code) if exc.code else 'stopped'
        print('STOPPED:',stop,flush=True)
    tot={k:sum(c['metrics'][k] for c in convs) for k in (convs[0]['metrics'] if convs else {})}
    (out/'summary.json').write_text(json.dumps({'creator':args.creator,'version':args.version,'model':ctx.chosen,
        'products':len(ctx.base['products']),'stopped':stop,'totals':tot},indent=1))
    print('TOTALS',json.dumps(tot),flush=True)


if __name__=='__main__':main()
