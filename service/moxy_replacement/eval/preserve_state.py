"""Preserve-state/no-send extension of qualify, using its writer, validator and scoped model call.

Recorded snapshots are never relaxed. Continuous cases are explicitly counterfactual
branches. No transport can run, and all model accounting lives in owned disposable PG.
"""
import copy,json,os,uuid,hashlib
from contextlib import ExitStack
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import create_engine,select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from db.models import AccountAiConfig,GrokCall
from db.schema import bootstrap
from .. import writer,examples
from ..runtime import configuration,validate_decision,correction_brief,WORDING_REASONS,FORMAT_REASONS,recoverable
from ..parts import plan
from ..configuration import canonical,load
from . import qualify

HOLDOUTS={6,45,89}  # #6 is Amelia; #45 is Cristina. No holdout source enters reference retrieval.


def settings_for(snap,order=None):
    products=[snap['products'][key] for key in order] if order else list(snap['products'].values())
    return {'creator_key':snap['instructions']['creator_key'],'products':products,
        'sequences':copy.deepcopy(snap['sequences']),
        'catalogue_bindings':{k:v['item']['id'] for k,v in snap['inventory'].items()},
        'operational':copy.deepcopy(snap['operational']),'pilot':copy.deepcopy(snap.get('pilot')),
        'approved_remedies':copy.deepcopy(snap.get('approved_remedies',{})),
        'public_facts':copy.deepcopy(snap.get('public_facts',{}))}


def forbid_delivery(*args,**kwargs):
    raise AssertionError('Live or synthetic delivery is forbidden in preserve-state model replay')


def source_cases(bundle):
    result=[]
    for source in bundle['snapshots']:
        snap=copy.deepcopy(source['snapshot'])
        assert snap['history'][-1]['direction']=='in','Future replies in recorded context'
        mode='recorded-context'
        if source.get('neutral_probe_input'):
            snap['history'][-1]['text']=source['neutral_probe_input']
            from ..state import commercial_state
            snap['commercial_state']=commercial_state(snap)
            mode='recorded-state-neutral-counterfactual'
        order=bundle.get('orders',{}).get(source['account'],{}).get('product_order')
        settings=settings_for(snap,order)
        match=configuration(settings)[1].fingerprint==snap['configuration_fingerprint']
        if not match:raise RuntimeError('Cannot reconstruct original product/rule authority exactly')
        result.append({'id':'recorded-'+source['event_id'],'mode':mode,'snapshot':snap,'settings':settings,
            'source':{k:v for k,v in source.items() if k!='snapshot'},'fingerprint_matches':match})
    return result


def holdout_case(record,base):
    source=next(e for e in examples._bank()['examples'] if e['record']==record)
    creator='amelia' if record==6 else 'cristina'
    lines=examples._lines(source)
    # Use the fan's final ordinary-chat input, stopping BEFORE the recorded human response.
    position=2 if record==6 else 10
    selected=lines[:position+1]
    snap=copy.deepcopy(base)
    cfg=load(creator)
    snap.update(instructions=cfg.public_context(),products={},inventory={},sequences={},offer_eligibility={},
        approved_remedies={},invitation=None,obligations=[],campaign=None,pilot=None,
        public_facts={},fan_name=None,memory={},card_state=[],
        history=[{'id':i+1,'direction':'in' if role=='fan' else 'out','text':text,
            'sender':'fan' if role=='fan' else 'human','at':'2026-11-01T00:00:00',
            'price_cents':0,'media_count':0} for i,(role,text) in enumerate(selected)])
    # This is an unseen-exchange probe, NOT a reconstructed live fan. No product,
    # purchase or specialist permission is fabricated for it.
    snap['fan_state']={**{k:False for k in ('human_owned','safety_hold','financial_hardship','ordinary_chat_only',
        'sales_refused','boundary_hold','no_push','ownership_verified','purchase_links_verified')},
        'commerce_uncertain':True,'owned_assets':[],'purchased_products':[],'pending_products':[],
        'rejected_products':[],'previously_sent_assets':[],'paid_assets_by_product':{},'budget_cents':None,
        'requested_resend_product':None}
    snap['event']={'kind':'turn','payload':{'source_message_id':snap['history'][-1]['id']}}
    from ..state import commercial_state
    snap['commercial_state']=commercial_state(snap)
    return {'id':f'unseen-record-{record}','mode':'unseen-approved-exchange-probe','snapshot':snap,
        'settings':settings_for(snap),'source':{'record':record,'creator':creator,
        'limitation':'Historical creator/fan purchase and control state unavailable. Text-only generalisation probe.'}}


def run(args):
    bundle=json.loads(Path(args.preserve_state_bundle).read_text())
    imported=json.loads(Path(args.budget_summary).read_text())
    if not imported or any(item['unknown'] for item in imported):raise SystemExit('Prior budget unavailable or uncertain')
    budget=qualify.Budget(args.journal,globs=[])
    budget.imported=[{'actual':item['actual'] if i==0 else 0} for item in imported for i in range(item['calls'])]
    budget.imported_files=[item['path'] for item in imported]
    url=make_url(os.environ['ONLYFANS_TEST_PG_URL'])
    if url.host!='127.0.0.1' or url.port!=55436 or url.database!='postgres':
        raise SystemExit('Replay requires its owned disposable PostgreSQL server on 127.0.0.1:55436')
    admin=create_engine(url,isolation_level='AUTOCOMMIT')
    name='moxy_replay_'+uuid.uuid4().hex
    with admin.connect() as c:c.exec_driver_sql(f'CREATE DATABASE "{name}"')
    replay_url=url.set(database=name)
    engine=create_engine(replay_url)
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    results=[]
    try:
        with engine.begin() as c:bootstrap(c)
        # Only model reservation infrastructure is seeded. Writer/validator state
        # comes from the immutable source snapshot, never the integrated fixture.
        from test_onlyfans_foundation_pg import seed
        source=bundle['snapshots'][0]
        store=seed(str(replay_url),engine,account=source['account'],tenant='repair-replay',fan=source['fan'])
        account=bundle['accounts'][source['account']]
        with Session(engine) as s,s.begin():
            s.add(AccountAiConfig(account_id=source['account'],model=account['chosen'],
                daily_cost_cap_cents=account['config']['daily_cost_cap_cents']))
        os.environ['MOXY_TEST_MODEL_API_KEY']=bundle['credential']['apiKey']
        credential={'apiKey':os.environ['MOXY_TEST_MODEL_API_KEY'],'modelId':bundle['credential']['modelId']}
        expiry=datetime.fromisoformat(bundle['credential']['expires']).replace(tzinfo=timezone.utc)
        ctx=SimpleNamespace(account=source['account'],fan=source['fan'],credential=credential,expires=expiry,
            config=account['config'],chosen=account['chosen'],engine=engine,budget=budget)
        cases=source_cases(bundle)
        base=cases[0]['snapshot']
        probes=[holdout_case(6,base),holdout_case(45,base)]
        wanted=set(args.only.split(',')) if args.only else None
        if wanted:
            cases=[c for c in cases if c['id'] in wanted]
            probes=[c for c in probes if c['id'] in wanted]
        bank=examples._bank()
        filtered=copy.deepcopy(bank);filtered['examples']=[e for e in filtered['examples'] if e['record'] not in HOLDOUTS]
        from onlyfans_runtime.text_delivery import TextAuthority,SyntheticTextTransport
        from onlyfans_runtime.loopback_transport import LoopbackTextTransport
        with ExitStack() as stack:
            stack.enter_context(patch.object(examples,'_bank',return_value=filtered))
            stack.enter_context(patch.object(TextAuthority,'prepare',forbid_delivery))
            stack.enter_context(patch.object(TextAuthority,'dispatch',forbid_delivery))
            stack.enter_context(patch.object(SyntheticTextTransport,'send',forbid_delivery))
            stack.enter_context(patch.object(LoopbackTextTransport,'send',forbid_delivery))
            for case in cases+probes:
                rec=run_one(ctx,case,out,filtered)
                results.append(rec);flush(out,results,imported,budget)
                if rec['actual_millicents'] is None:raise SystemExit('Unknown replay usage; no retry')
            # Continuous replay branches from the model's OWN preceding reply.
            # They deliberately do not inject a recorded human response as the model.
            for prior in list(results[:2]):
                if not prior.get('decision') or prior.get('error'):continue
                if wanted and 'continuous-'+prior['id'] not in wanted:continue
                original=next(c for c in cases if c['id']==prior['id'])
                snap=copy.deepcopy(original['snapshot'])
                for i,part in enumerate(plan(prior['decision'])):
                    snap['history'].append({'id':900000+i,'direction':'out','sender':'ai','text':part['text'],
                        'at':'2026-11-05T00:00:00','price_cents':0,'media_count':0})
                # A plain clarification assesses recovery without adding purchase facts or permissions.
                fan='No, I meant what do you do for work. Can you answer that question?'
                snap['history'].append({'id':900100,'direction':'in','sender':'fan','text':fan,
                    'at':'2026-11-05T00:00:01','price_cents':0,'media_count':0})
                snap['event']={'kind':'turn','payload':{'source_message_id':900100}}
                from ..state import commercial_state
                snap['commercial_state']=commercial_state(snap)
                case={'id':'continuous-'+prior['id'],'mode':'continuous-counterfactual','snapshot':snap,
                    'settings':original['settings'],'source':{'previous_replay':prior['id'],
                        'state_preserved_except':'model reply and next explicitly synthetic clarification'}}
                rec=run_one(ctx,case,out,filtered);results.append(rec);flush(out,results,imported,budget)
                if rec['actual_millicents'] is None:raise SystemExit('Unknown replay usage; no retry')
        store.close()
    finally:
        engine.dispose()
        with admin.connect() as c:c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()
    print(json.dumps({'actual_model_calls':sum(len(r['attempts']) for r in results),'usage_unknown':sum(r['actual_millicents'] is None for r in results),
        'validation_errors':sum(bool(r['error']) for r in results),'out':str(out)}))
    return 0


def run_one(ctx,case,out,filtered):
    snap=case['snapshot'];prompts=writer.messages(snap)
    selected,_=examples.select(snap['instructions']['creator_key'],snap['history'],snap['fan_state'])
    assert not HOLDOUTS.intersection(e['record'] for e in selected)
    folder=out/case['id'];folder.mkdir()
    (folder/'snapshot.json').write_text(json.dumps(snap,indent=2),encoding='utf-8')
    (folder/'prompt.json').write_text(json.dumps(prompts,indent=2),encoding='utf-8')
    attempts=[]
    for attempt in range(2):
        raw,actual,error=qualify.real_call(ctx,prompts,case['id'],attempt)
        parsed=None;reason=None
        if raw:
            try:
                parsed=writer.parse(raw,defer_sales_pause=True)
                validate_decision(case['settings'],snap,parsed,planning=True)
            except Exception as exc:error=type(exc).__name__+':'+str(exc);reason=str(exc)
        entry=ctx.budget.entries[-1]
        with Session(ctx.engine) as s:
            call=s.get(GrokCall,entry['call_id'])
            receipt={k:getattr(call,k) for k in ('id','model','provider','endpoint','status','was_dry_run','cost_cents',
                'tokens_in','tokens_out','cache_hit_tokens')}
        attempts.append({'raw_output':raw,'error':error,'actual_millicents':actual,'call_receipt':receipt})
        if attempt or actual is None or not (reason in FORMAT_REASONS or (reason is not None and recoverable(reason))):break
        brief=correction_brief(reason,parsed,snap) if parsed else {'kind':'format','reason':reason,'action':None,'product_id':None}
        snap=copy.deepcopy(snap)
        snap['event']['payload'].update(correction_of='replay-attempt-0',rejected=brief)
        prompts=writer.messages(snap)
        (folder/'correction-prompt.json').write_text(json.dumps(prompts,indent=2),encoding='utf-8')
    result={k:v for k,v in case.items() if k not in {'snapshot','settings'}}
    result.update(raw_output=raw,decision=parsed,error=error,actual_millicents=actual,call_receipt=receipt,attempts=attempts,
        snapshot_sha256=hashlib.sha256(canonical(snap).encode()).hexdigest(),
        selected_records=[e['record'] for e in selected],excluded_records=sorted(HOLDOUTS),
        sampling={'temperature':.7,'max_tokens':4096,'chosen_model':ctx.chosen,'reasoning_effort':ctx.config.get('reasoning_effort')},
        transport_disabled=True,quality_verdict='REQUIRES_REVIEW')
    (folder/'transcript.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


def flush(out,results,imported,budget):
    (out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    count,cost=budget.totals()
    (out/'provenance.json').write_text(json.dumps({'budget_sources':imported,'cumulative_calls':count,
        'cumulative_units':cost,'caps':{'calls':450,'units':30000},'production_journals_modified':False,
        'local_journal_addendum':str(budget.path),'limitations':[
            'Historical snapshots lack earlier cards outside their captured history window.',
            'No historical Cristina pilot Moxy event snapshots were available.',
            'Model replay exercises writer/parser/validator. Durable delivery is separately exercised by PostgreSQL regressions.']},indent=2))
