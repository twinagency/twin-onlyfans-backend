"""Moxy's durable single policy owner over the canonical CRM delivery authority.

All network work uses the existing scoped model/transport adapters. No process
startup activates an account, and no failure falls back to another writer.
"""
from __future__ import annotations
import asyncio
from datetime import timedelta
import json
import uuid
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from db.models import (AccountAiConfig, Fan, Message, CatalogItem, VaultItem, ContentOffer,
    OfConversationState, GrokCall)
from db.onlyfans_models import OfWork
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_moxy_models import MoxyAccount, MoxyFan, MoxyEvent, MoxyAudit
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.native_context import canonical, versions
from .configuration import load, load_foundation, CreatorConfiguration, ActionBlocked, InvalidConfiguration, digest
from .authority import ProductAuthority, readiness
from . import writer

EVENTS={'turn','welcome','invitation','scheduled','campaign','purchase','refund','chargeback','fulfilment'}


def selected(s, account):
    return s.get(MoxyAccount, account) is not None


def configuration(settings):
    required={'creator_key','products','sequences','catalogue_bindings','operational'}
    if type(settings) is not dict or not required<=set(settings):raise InvalidConfiguration('Moxy source bindings required')
    cfg=load(settings['creator_key']) if 'creator_document' not in settings else CreatorConfiguration(
        load_foundation(),settings['creator_document'])
    pa=ProductAuthority(cfg,settings['products'],settings['sequences'])
    from .pilot import scope
    scope(settings)
    sequence_rule=cfg.overrides.get('vip_sequence')
    if sequence_rule:
        per_level=sequence_rule['value'].get('ppvs_per_level')
        levels=settings.get('sequence_levels',{})
        for name,steps in pa.sequences.items():
            groups=levels.get(name)
            if not groups or any(len(g)!=per_level for g in groups) or [p for g in groups for p in g]!=steps:
                raise InvalidConfiguration('Source requires complete ordered four-PPV levels')
    return cfg,pa


def enqueue(s, account, fan, kind, key, payload):
    if (kind not in EVENTS or type(fan) is not int or fan<=0 or type(payload) is not dict
            or any(str(k).startswith('_moxy') for k in payload) or len(canonical(payload).encode())>16000):
        raise Refused('invalid_moxy_event')
    identity=digest({'key':key,'fan':fan,'kind':kind})
    prior=s.scalar(select(MoxyEvent).where(MoxyEvent.account_id==account,MoxyEvent.event_key==identity))
    if prior:
        if prior.payload_json!=canonical(payload): raise Refused('moxy_event_key_conflict')
        return prior.event_id
    now=utcnow()
    row=MoxyEvent(account_id=account,event_id=str(uuid.uuid4()),fan_id=fan,event_key=identity,
        kind=kind,payload_json=canonical(payload),status='pending',created_at=now,updated_at=now)
    if kind in {'turn','welcome'}:
        cfg=s.get(AccountAiConfig,account)
        saved=json.loads(cfg.ai_chatter_config_json or '{}') if cfg else {}
        if saved.get('native_reply_pacing_enabled') is True:
            import random
            # Same approved 25..90s triangular reply setting as native_pilot.pacing.
            active=s.scalar(select(MoxyEvent.event_id).where(MoxyEvent.account_id==account,
                MoxyEvent.fan_id==fan,MoxyEvent.status=='done',MoxyEvent.updated_at>=now-timedelta(minutes=5)).limit(1)) is not None
            source=s.get(Message,(account,fan,payload.get('source_message_id'))) if payload.get('source_message_id') else None
            received=source.ingested_at if source is not None and source.ingested_at is not None else now
            if isinstance(received,str):received=__import__('datetime').datetime.fromisoformat(received)
            row.not_before=received+timedelta(seconds=random.triangular(25,90,35 if active else 60))
    s.add(row);s.flush()
    return row.event_id


def _fan(s,account,fan):
    row=s.get(MoxyFan,(account,fan))
    if row is None:
        row=MoxyFan(account_id=account,fan_id=fan,revision=0,facts_json='{}',state_json='{}',updated_at=utcnow())
        s.add(row);s.flush()
    return row


def _inventory_report(s,account,settings,pa):
    """(usable inventory, {product_key: reason}). A product is never dropped silently:
    an asset whose description is pending/failed/unsupported/missing is excluded with
    `media_description_<state>:<media_id>` so readiness and operators can see why."""
    from .media_descriptions import item_state,effective_description,limitations,operator_corrected
    result={};excluded={}
    for key,p in pa.products.items():
        item=s.get(CatalogItem,settings['catalogue_bindings'].get(key))
        if not item or item.account_id!=account or not item.enabled:
            excluded[key]='catalogue_item_missing_or_disabled';continue
        try: ids=json.loads(item.media_ids); previews=json.loads(item.preview_media_ids)
        except (TypeError,ValueError):
            excluded[key]='catalogue_media_unreadable';continue
        if ids!=[a['id'] for a in p['assets']] or item.price_cents!=p['price_cents'] or item.pricing_mode!='fixed':
            excluded[key]='catalogue_binding_mismatch';continue
        if p['approval_ref'].startswith('derived-catalogue:'):
            from db.onlyfans_vault_models import OfVaultProductCreate
            receipt=s.get(OfVaultProductCreate,(account,p['approval_ref'].split(':',1)[1]))
            try:
                recorded=json.loads(receipt.acknowledgement_json) if receipt else {}
                evidence=recorded.get('evidence',{})
                if (recorded.get('itemId')!=item.id or recorded.get('product')!=p
                        or evidence.get('media_ids')!=ids or evidence.get('preview_ids')!=previews
                        or evidence.get('price_cents')!=item.price_cents):
                    raise ValueError()
            except (ValueError,TypeError,KeyError):
                excluded[key]='derived_catalogue_evidence_changed';continue
        files=[];reason=None;facts_by_asset=[]
        for a in p['assets']:
            v=s.get(VaultItem,(account,a['id']))
            if v is not None and v.removed_at is not None:v=None
            if v is None:
                reason='media_description_missing:%s'%a['id'];break
            if v.kind not in {'photo','image','video'}:
                reason='media_kind_unsupported:%s'%a['id'];break
            state=item_state(v)
            if state!='described':
                reason='media_description_%s:%s'%(state,a['id']);break
            # Asset facts are approved on import/change, never derived from an
            # old fan-facing caption. Revisions bind exact current metadata.
            facts=effective_description(v)
            evidence=digest({'media_id':v.media_id,'content_hash':v.content_hash,
                'description':facts,'updated_at':v.updated_at.isoformat() if v.updated_at else None})
            from .pricing import asset_evidence
            references={'asset:'+evidence, 'asset-v2:'+asset_evidence(v)}
            if a['evidence_ref'] not in references or a['facts']!=[facts] or not facts:
                reason='asset_evidence_stale:%s'%a['id'];break
            facts_by_asset.append(facts)
            files.append({'media_id':v.media_id,'kind':v.kind,'content_hash':v.content_hash,
                'updated_at':v.updated_at if a['evidence_ref'].startswith('asset:') else None,'removed_at':None,
                # What the description is based on (sampling limits), so a caption never
                # claims audio, motion or detail the analysis could not have seen.
                'description_basis':limitations(v),'operator_corrected':operator_corrected(v)})
        if reason is None and (not ids or not set(previews) < set(ids)):reason='catalogue_media_invalid'
        if reason is None:
            from .pricing import validate_format, validate_price
            try:
                rule=pa.configuration.resolve('product:'+p['category'], {}).value
                validate_price(rule,p['price_cents'])
                validate_format(rule,[s.get(VaultItem,(account,mid)) for mid in ids if mid not in previews])
            except ActionBlocked as exc:
                reason=str(exc)
        if reason:
            excluded[key]=reason;continue
        shown={n:getattr(item,n) for n in ('id','account_id','script_id','enabled','kind','label',
            'description_for_ai','price_cents','is_free_teaser','updated_at','pricing_mode')}
        # The catalogue's free-text note can predate a re-analysis or operator correction;
        # the model is shown the CURRENT effective asset descriptions instead, so one
        # selected set never carries two disagreeing descriptions.
        shown['description_for_ai']=facts_by_asset[0] if len(facts_by_asset)==1 else '; '.join(
            'item %d: %s'%(i+1,f) for i,f in enumerate(facts_by_asset))
        result[key]={'item':shown,
            'source':None,'media_ids':ids,'preview_ids':previews,'price_cents':p['price_cents'],'files':files}
    return json.loads(canonical(result)),excluded


def _inventory(s,account,settings,pa):
    return _inventory_report(s,account,settings,pa)[0]


def account_readiness(s,account,settings):
    cfg,pa=configuration(settings)
    ready=readiness(cfg,account_bound=True,products=settings['products'],sequences=settings['sequences'],
        operational=settings['operational'],coordination_verified=settings.get('coordination_verified'),pilot=settings.get('pilot'))
    if not settings['operational'].get('assigned_operator'):ready['blockers'].append('handover_owner_missing')
    usable,excluded=_inventory_report(s,account,settings,pa)
    if set(usable)!=set(pa.products):ready['blockers'].append('real_inventory_mapping_incomplete')
    ready['excluded_products']=excluded
    ready['blockers']+=['product_excluded:%s:%s'%(k,r) for k,r in sorted(excluded.items())
        if r.startswith(('media_description_','media_kind_'))]
    policy=s.get(OfDeliveryPolicy,account)
    synthetic=policy is not None and policy.transport_kind=='synthetic' and settings.get('fixture') is True
    if not synthetic and settings.get('production_verification') is not True:ready['blockers'].append('production_verification_missing')
    if s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id==account,
            OfTextIntent.status.in_(('dispatching','unknown'))).limit(1)):
        ready['blockers'].append('moxy_cutover_has_uncertain_delivery')
    ready['ready']=not ready['blockers']
    return ready


def observe_input(s,event,decision):
    """Retain fan constraints even when the writer chooses no outbound message.

    Delivery-dependent claims (invitations and fulfilled obligations) are still
    projected only from a confirmed receipt. Preview never calls this function.
    """
    mf=_fan(s,event.account_id,event.fan_id);state=json.loads(mf.state_json);before=canonical(state)
    from .observations import observations, apply
    state=apply(state,observations(decision,json.loads(event.snapshot_json)),event_id=event.event_id)
    if decision['question'] and any(m['direction']=='in' and decision['question'] in m['text']
            for m in json.loads(event.snapshot_json)['history']):
        obligations=state.setdefault('obligations',[])
        if not any(o.get('question')==decision['question'] and o['status']=='open' for o in obligations):
            obligations.append({'id':event.event_id,'kind':'buying_question','question':decision['question'],
                'status':'open','owner':'moxy','source_ref':'moxy-event:'+event.event_id,'source_event':event.event_id})
    if canonical(state)!=before:
        mf.state_json=canonical(state);mf.revision+=1;mf.updated_at=utcnow()
    if decision.get('permission')=='customs':
        # Owned custom lifecycle (quote, route, owner, deadline, fulfilment): see customs.py.
        settings=json.loads(s.get(MoxyAccount,event.account_id).settings_json)
        from .customs import record_request
        record_request(s,event,decision,configuration(settings)[0],json.loads(event.snapshot_json)['fan_state'],
            settings['operational'].get('assigned_operator'))


def snapshot(s,account,fan,event):
    owner=s.get(MoxyAccount,account)
    settings=json.loads(owner.settings_json)
    shift=settings['operational'].get('graveyard_shift_definition')
    if isinstance(shift,dict) and {'timezone','start_hour','end_hour'}<=set(shift):
        from zoneinfo import ZoneInfo
        local=__import__('datetime').datetime.now(ZoneInfo(shift['timezone']))
        start,end=shift['start_hour'],shift['end_hour']
        settings['operational']['in_graveyard_shift']=(start<=local.hour<end) if start<end else (local.hour>=start or local.hour<end)
    cfg,pa=configuration(settings)
    state=s.get(OfConversationState,(account,fan)); f=s.get(Fan,(account,fan)); mf=_fan(s,account,fan)
    memo=json.loads(mf.state_json); verified=json.loads(mf.facts_json)
    from onlyfans_runtime import operator_cases
    skip,critical,cases=operator_cases.policy_facts(s,account,fan)
    from onlyfans_runtime.commerce import held
    from ownership import owned_media_in_session
    inventory=_inventory(s,account,settings,pa)
    owned=sorted(owned_media_in_session(s,account,fan,limit=10000))
    from .exposure import previously_sent_assets
    exposed,exposure_verified=previously_sent_assets(s,account,fan)
    paid_assets={key:sorted(set(terms['media_ids'])-set(terms['preview_ids'])) for key,terms in inventory.items()}
    offers=s.scalars(select(ContentOffer).where(ContentOffer.account_id==account,ContentOffer.fan_id==fan)).all()
    purchased=[];pending=[]
    for key,terms in inventory.items():
        for offer in offers:
            if offer.item_id!=terms['item']['id']: continue
            message=s.get(Message,(account,fan,offer.offer_message_id)) if offer.offer_message_id else None
            if message is not None and message.is_paid is True and message.purchased_at is not None:
                purchased.append(key)
            elif offer.status=='open':pending.append(key)
    facts={**verified,'human_owned':not state or state.mode!='automated' or state.takeover_state!='none',
        'safety_hold':bool(critical or skip in {'of_restricted','manual_restrict','muted_creator'} or any(scope=='all' for _,scope in cases)),
        'commerce_uncertain':held(s,account,fan),
        'financial_hardship':memo.get('financial_hardship',False) or any(c=='hardship' for c,_ in cases),
        'ordinary_chat_only':memo.get('ordinary_chat_only',False),'sales_refused':memo.get('sales_refused',False),
        'boundary_hold':memo.get('boundary_hold',False),
        'no_push':verified.get('no_push',False),'owned_assets':owned,
        'previously_sent_assets':exposed,'paid_assets_by_product':paid_assets,
        'exposure_history_verified':exposure_verified,'purchased_products':sorted(set(purchased)),
        'pending_products':sorted(set(pending)),'rejected_products':memo.get('rejected_products',[]),
        'budget_cents':memo.get('budget_cents',verified.get('budget_cents')),
        'requested_resend_product':memo.get('requested_resend_product'),
        'ownership_verified':verified.get('ownership_verified',False),
        'purchase_links_verified':verified.get('purchase_links_verified',False)}
    rows=s.scalars(select(Message).where(Message.account_id==account,Message.fan_id==fan,Message.is_unsent.is_(False))
        .order_by(Message.created_at.desc(),Message.message_id.desc()).limit(100)).all()
    from db.models import OfMessageDelivery
    receipts={x.message_id:x.origin for x in s.scalars(select(OfMessageDelivery).where(
        OfMessageDelivery.account_id==account,OfMessageDelivery.fan_id==fan))}
    history=[{'id':r.message_id,'direction':r.direction,'text':r.body,'at':r.created_at,
        'attachment_description':r.image_desc,'media_count':r.media_count,
        'sender':('fan' if r.direction=='in' else 'ai' if receipts.get(r.message_id)=='ai' else
            'human' if r.sent_by_employee_id or receipts.get(r.message_id)=='human' else 'unattributed'),
        'price_cents':r.price_cents} for r in reversed(rows)]
    # The canonical verified memory owner is reused. No creator-private facts
    # from that owner's config/biography are copied into this prompt.
    _,native=versions(s,account,fan)
    # Profile refresh time is polling metadata, not a change to fan evidence.
    # Retain every actual native config/fan/profile value in the delivery fence.
    if native.get('fan') is not None:
        native['fan'].pop('profile_last_synced_at',None)
    memory=native.get('historicalMemory') or {}
    state_view={'revision':state.revision if state else None,'input_revision':state.input_revision if state else None,
        'mode':state.mode if state else None,'takeover':state.takeover_state if state else None}
    result={'fan_id':fan,'pilot':settings.get('pilot'),'account_revision':owner.revision,'binding_id':owner.binding_id,'configuration_fingerprint':pa.fingerprint,
        'instructions':cfg.public_context(),'public_facts':settings.get('public_facts',{}),
        'fan_state':facts,'fan_revision':mf.revision,'control':state_view,'event':{'kind':event.kind,'payload':json.loads(event.payload_json)},
        'history':history,'memory':memory,'invitation':memo.get('invitation'),
        'obligations':memo.get('obligations',[]),'products':{k:pa.products[k] for k in inventory},
        'inventory':inventory,'sequences':pa.sequences,'operational':settings['operational'],
        'approved_remedies':settings.get('approved_remedies',{}),'native_fingerprint':digest(json.loads(canonical(native)))}
    from .campaigns import context as campaign_context
    result['campaign']=campaign_context(s,account,fan,json.loads(event.payload_json))
    return json.loads(canonical(result))


def _norm(text):
    # Quote evidence tolerates only case, whitespace and typographic quote differences.
    return ' '.join(str(text).translate(str.maketrans('\u2018\u2019\u201c\u201d','\'\'""')).lower().split())


COMMERCIAL_ACTIONS={'invite','offer','resend','alternative','continue','correct','fulfil'}


def validate_decision(settings,snap,decision):
    from .pilot import decision as pilot_decision
    pilot_decision(settings,snap,decision)
    cfg,pa=configuration(settings)
    if (set(decision['flags']) & {'hardship','refusal','boundary_reset'}
            and decision['action'] in {'invite','offer','resend','alternative','continue'}):
        raise ActionBlocked('sales_paused_by_current_input')
    campaign=snap.get('campaign')
    if campaign is not None and (not campaign['available'] or campaign['revision']!=snap['event']['payload'].get('campaign_revision')):
        raise ActionBlocked('moxy_campaign_changed')
    facts=dict(snap['fan_state']);action=decision['action'];key=decision['product_id']
    from .observations import observations, apply
    facts=apply(facts,observations(decision,snap))
    if decision['question'] is not None and not any(m['direction']=='in' and _norm(decision['question']) in _norm(m['text']) for m in snap['history']):
        # An unquoted buying question only loses its reminder obligation; it never holds the turn.
        decision['question']=None
    if decision['permission']:
        if cfg.resolve(decision['permission'],facts).value is False: raise ActionBlocked('permission_refused')
    if facts.get('talker_label') is True and facts.get('currently_spending') is False:
        try:gap=cfg.resolve('talker_timing',facts).value
        except ActionBlocked as exc:
            # An absent rule leaves the gap unresolved; it is never guessed.
            raise ActionBlocked('talker_timing_unresolved' if str(exc).startswith('instruction_missing') else str(exc)) from exc
        seconds=gap.get('minimum_reply_gap_seconds') if type(gap) is dict else None
        if type(seconds) is not int or seconds<=0:raise ActionBlocked('talker_timing_unresolved')
        outgoing=[m for m in snap['history'] if m['direction']=='out']
        if outgoing and utcnow()-__import__('datetime').datetime.fromisoformat(outgoing[-1]['at'])<timedelta(seconds=seconds):raise ActionBlocked('creator_reply_gap_setting')
    if facts.get('clock_label') is True:
        if snap['operational'].get('in_graveyard_shift') is None:raise ActionBlocked('graveyard_shift_unknown')
        if snap['operational']['in_graveyard_shift']:raise ActionBlocked('graveyard_clock_hold')
    request_kind=decision.get('request_kind','none')
    quote=decision.get('request_quote')
    if request_kind!='none':
        incoming=[m for m in snap['history'] if m['direction']=='in']
        if not incoming or not quote or _norm(quote) not in _norm(incoming[-1]['text']):
            # Unverifiable request evidence never authorises commerce; a plain
            # reply without a product is still sent, just without request effects.
            if action in COMMERCIAL_ACTIONS or key is not None:raise ActionBlocked('current_request_evidence_missing')
            request_kind='none'
    if request_kind!='none':
        if request_kind in {'direct','accept'}:
            facts['direct_request_product']=key
            facts['ordinary_chat_only']=facts['sales_refused']=False
        if request_kind=='resend':facts['requested_resend_product']=key
    terms=None
    if action in {'invite','offer','resend','alternative','continue','correct','fulfil'}:
        if key not in snap['inventory']:raise ActionBlocked('real_inventory_missing_or_stale')
        if 'clock_label' in facts and facts['clock_label'] is True:
            if snap['operational'].get('in_graveyard_shift') is None: raise ActionBlocked('graveyard_shift_unknown')
            if snap['operational']['in_graveyard_shift']: raise ActionBlocked('graveyard_clock_hold')
        if action in {'correct','fulfil'}:
            approval=snap['approved_remedies'].get(key)
            owed=next((o for o in snap['obligations'] if o['id']==decision['resolve_obligation'] and o.get('product_id')==key and o.get('status')=='open'),None)
            if not approval or (action=='fulfil' and not owed):raise ActionBlocked('remedy_or_obligation_authority_missing')
            if facts['human_owned'] or facts['safety_hold']:raise ActionBlocked('owner_or_safety_hold')
            if approval.get('complimentary') is not True:raise ActionBlocked('remedy_requires_exact_approved_terms')
            terms={**snap['inventory'][key],'price_cents':0}
        else:
            # The model may interpret natural paraphrases as a direct request or
            # assent; the retained product/invitation remains explicit evidence.
            if snap['event']['payload'].get('direct_request_product')==key: facts['direct_request_product']=key
            if snap['event']['payload'].get('requested_resend_product')==key: facts['requested_resend_product']=key
            purpose={'invite':'offer','continue':'continuation'}.get(action,action)
            if action=='offer' and snap.get('invitation'):
                if key!=snap['invitation']['product_id']:raise ActionBlocked('acceptance_product_mismatch')
                pa.accepted_invitation(snap['invitation'],facts)
            else:pa.terms(key,facts,purpose=purpose,sequence=decision['sequence'])
            terms=snap['inventory'][key] if action!='invite' else None
    elif key is not None:raise ActionBlocked('unexpected_product_reference')
    if decision['resolve_obligation']:
        owed=next((o for o in snap['obligations'] if o['id']==decision['resolve_obligation'] and o.get('status')=='open'),None)
        if owed is None or (action not in {'correct','fulfil'} and not (action in {'reply','invite','offer','alternative'} and owed.get('kind')=='buying_question')):
            raise ActionBlocked('obligation_cannot_resolve_without_fulfilment')
    if terms and campaign and campaign['catalogue_item_id'] is not None:
        if (terms['item']['id']!=campaign['catalogue_item_id'] or campaign['terms'] is None
                or terms['price_cents']!=campaign['terms']['price_cents']
                or terms['media_ids']!=campaign['terms']['media_ids']):
            raise ActionBlocked('moxy_campaign_product_changed')
    return terms


class MoxyService:
    def __init__(self,store):
        from onlyfans_runtime.text_delivery import TextAuthority
        self.store=store; self.engine=store.engine; self.authority=TextAuthority(store)

    def configure(self,tenant,account,*,expected_revision,mode,settings,actor):
        if (type(expected_revision) is not int or expected_revision<0 or type(mode) is not str
            or mode not in {'off','preview','on'} or type(actor) is not str or not actor or len(actor)>128):raise Refused('invalid_moxy_configuration')
        cfg,pa=configuration(settings)
        with Session(self.engine) as s,s.begin():
            runtime=self.store._runtime(s,tenant,account)
            owner=s.get(MoxyAccount,account)
            if (owner.revision if owner else 0)!=expected_revision:raise Refused('moxy_revision_conflict')
            if owner and owner.binding_id!=runtime.binding_id:raise Refused('moxy_binding_changed')
            uncertain=s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id==account,
                OfTextIntent.status.in_(('dispatching','unknown'))).limit(1))
            if uncertain and mode!='off':raise Refused('moxy_cutover_has_uncertain_delivery')
            ready=account_readiness(s,account,settings)
            policy=s.get(OfDeliveryPolicy,account)
            if mode=='on' and not ready['ready']:raise Refused('moxy_activation_not_ready:'+','.join(ready['blockers']))
            before={'mode':owner.mode,'revision':owner.revision,'settings':json.loads(owner.settings_json)} if owner else {}
            if owner is None:
                owner=MoxyAccount(account_id=account,binding_id=runtime.binding_id,mode=mode,revision=1,
                    settings_json=canonical(settings),readiness_json=canonical(ready),actor=actor,updated_at=utcnow());s.add(owner)
            else:
                owner.mode=mode;owner.revision+=1;owner.settings_json=canonical(settings)
                owner.readiness_json=canonical(ready);owner.actor=actor;owner.updated_at=utcnow()
            s.flush()
            # Only queued automation is cancelled. Accepted delivery, financial
            # records, manual work and fulfilment evidence survive cutover.
            queued=s.scalars(select(OfTextIntent).where(OfTextIntent.account_id==account,
                OfTextIntent.status=='queued',OfTextIntent.origin!='manual').with_for_update()).all()
            for row in queued:
                row.status='cancelled';row.reason='moxy_owner_changed';policy.queued_count-=1
                if row.moxy_event_id:
                    old=s.get(MoxyEvent,(account,row.moxy_event_id))
                    if old:old.status='stale';old.reason='moxy_owner_changed'
            # Old engagement timers have no Moxy business authority. Preserve
            # their evidence, and detach them through the existing cancellation seam.
            from onlyfans_runtime.launch_engagement.service import invalidate
            invalidate(s,runtime,reason='moxy_owner_changed',actor=actor)
            if mode!='off':
                for event in s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,
                    MoxyEvent.status=='held',MoxyEvent.reason=='moxy_off',MoxyEvent.model_call_id.is_(None))):
                    event.status='pending';event.reason=None
            s.add(MoxyAudit(audit_id=str(uuid.uuid4()),account_id=account,actor=actor,
                before_json=canonical(before),after_json=canonical({'mode':mode,'revision':owner.revision,'settings':settings}),created_at=utcnow()))
            return {'revision':owner.revision,'mode':mode,'readiness':ready}

    def fan_facts(self,tenant,account,fan,facts,*,actor,evidence_ref):
        if not actor or not evidence_ref or type(facts) is not dict:raise Refused('verified_fan_source_required')
        with Session(self.engine) as s,s.begin():
            self.store._runtime(s,tenant,account)
            if not selected(s,account):raise Refused('moxy_owner_required')
            row=_fan(s,account,fan);row.facts_json=canonical({**facts,'evidence_ref':evidence_ref})
            row.revision+=1;row.updated_at=utcnow()

    def add_event(self,tenant,account,fan,kind,key,payload):
        with Session(self.engine) as s,s.begin():
            self.store._runtime(s,tenant,account)
            if not selected(s,account):raise Refused('moxy_owner_required')
            return enqueue(s,account,fan,kind,key,payload)

    def from_claim(self,tenant,claim):
        with Session(self.engine) as s,s.begin():
            self.store._runtime(s,tenant,claim.account_id)
            work=s.get(OfWork,(claim.account_id,claim.fan_id));state=s.get(OfConversationState,(claim.account_id,claim.fan_id))
            if not self.authority._valid_work(claim,work,state,utcnow()):raise Refused('stale_moxy_input')
            outbound=s.scalar(select(Message.message_id).where(Message.account_id==claim.account_id,Message.fan_id==claim.fan_id,Message.direction=='out').limit(1))
            identity=enqueue(s,claim.account_id,claim.fan_id,'turn' if outbound else 'welcome',
                'input:'+str(claim.target_message_id),{'source_message_id':claim.target_message_id})
            work.status='done';work.lease_token=work.lease_until=None
            return identity

    def prepare(self,tenant,account,event_id,model,*,timeout=10):
        from onlyfans_runtime.native_planning import SyntheticNativeModel
        from onlyfans_runtime.provider_model import NativeProviderModel
        if type(model) not in {SyntheticNativeModel,NativeProviderModel}:raise Refused('scoped_moxy_model_required')
        live=type(model) is NativeProviderModel
        if live and model.account_id!=account:raise Refused('moxy_model_scope_mismatch')
        with Session(self.engine) as s,s.begin():
            runtime=self.store._runtime(s,tenant,account);owner=s.get(MoxyAccount,account)
            event=s.get(MoxyEvent,(account,event_id));now=utcnow()
            if event is None:raise Refused('moxy_event_missing')
            if event.status!='pending':return event_id
            if event.not_before and event.not_before>now:return event_id
            if owner.mode=='off':event.status='held';event.reason='moxy_off';return event_id
            policy=s.get(OfDeliveryPolicy,account)
            if policy is None or policy.transport_kind!=('native' if live else 'synthetic'):raise Refused('moxy_model_transport_mode_mismatch')
            busy=s.scalar(select(MoxyEvent.event_id).where(MoxyEvent.account_id==account,MoxyEvent.fan_id==event.fan_id,
                MoxyEvent.event_id!=event_id,MoxyEvent.status.in_(('generating','ready','queued','unknown'))).limit(1))
            if busy:return event_id
            fan_id=event.fan_id
            # Reuse identity, danger and operator-stop detection before any model.
        self.authority._safety_scan(tenant,account,fan_id,'automated')
        with Session(self.engine) as s,s.begin():
            runtime=self.store._runtime(s,tenant,account);owner=s.get(MoxyAccount,account);event=s.get(MoxyEvent,(account,event_id))
            if event.status!='pending':return event_id
            if owner.mode=='off':event.status='held';event.reason='moxy_off';return event_id
            from .pilot import check as pilot_check
            try:pilot_check(json.loads(owner.settings_json),event.fan_id,event.kind)
            except ActionBlocked as exc:
                event.status='held';event.reason=str(exc);return event_id
            busy=s.scalar(select(MoxyEvent.event_id).where(MoxyEvent.account_id==account,MoxyEvent.fan_id==event.fan_id,
                MoxyEvent.event_id!=event_id,MoxyEvent.status.in_(('generating','ready','queued','unknown'))).limit(1))
            if busy:return event_id
            control=s.get(OfConversationState,(account,event.fan_id));policy=s.get(OfDeliveryPolicy,account)
            reason=self.authority._eligibility(s,runtime,policy,control,s.get(Fan,(account,event.fan_id)),'automated',utcnow(),action='chat')
            if reason:
                if reason in {'contact_blocked','case_identity','case_distress','safety_hold'}:
                    from db.onlyfans_case_models import OfOperatorCase
                    assigned=json.loads(owner.settings_json)['operational'].get('assigned_operator')
                    if assigned:
                        for case in s.scalars(select(OfOperatorCase).where(OfOperatorCase.account_id==account,
                            OfOperatorCase.fan_id==event.fan_id,OfOperatorCase.status.in_(('open','acknowledged')))):
                            case.owner=case.owner or assigned
                event.status='held';event.reason=reason;return event_id
            snap=snapshot(s,account,event.fan_id,event)
            source=json.loads(event.payload_json).get('source_message_id')
            if source and event.kind in {'turn','welcome'}:
                incoming=[m for m in snap['history'] if m['direction']=='in']
                if not incoming or incoming[-1]['id']!=source:
                    event.status='stale';event.reason='moxy_input_superseded';return event_id
                location=next((i for i,m in enumerate(snap['history']) if m['id']==source),None)
                if location is None:
                    event.status='held';event.reason='moxy_input_context_missing';return event_id
                if any(m['direction']=='out' and m['sender'] in {'human','unattributed'} for m in snap['history'][location+1:]):
                    event.status='done';event.reason='external_reply_after_input';return event_id
            if len(canonical(snap).encode())>1048576:raise Refused('moxy_snapshot_budget_exceeded')
            prompts=writer.messages(snap)
            if len(canonical(prompts).encode())>81920:raise Refused('moxy_context_budget_exceeded')
            config=s.get(AccountAiConfig,account)
            from automations._common import resolve_model_from_config
            chosen=resolve_model_from_config(config,'ai_chatter',account_id=account,require_explicit=True)
            from llm_client import reserve_scoped_synthetic_call
            if live:model.configure_call(account_id=account,chosen_model=chosen,
                config={c.name:getattr(config,c.name) for c in config.__table__.columns},messages=prompts)
            reserve=model.reserve if live else reserve_scoped_synthetic_call
            call,units,day=reserve(s,account_id=account,fan_id=event.fan_id,model=chosen,messages=prompts,now=utcnow())
            event.model_call_id=call.id;event.reserved_millicents=units;event.budget_day=day
            event.snapshot_json=canonical(snap);event.snapshot_hash=digest(snap)
            event.status='generating';event.lease_until=utcnow()+timedelta(seconds=60);event.updated_at=utcnow()
        result=reason=None
        async def generate():
            async with asyncio.timeout(timeout):return await model.generate(prompts)
        # Keep structurally valid input observations even when the proposed
        # sale is refused. Authority still gates preparation and every send.
        try:result=writer.parse(asyncio.run(generate()),defer_sales_pause=True)
        except (TimeoutError,asyncio.TimeoutError):reason='moxy_model_unknown'
        except Exception as exc:reason=str(exc) if isinstance(exc,ActionBlocked) else 'moxy_draft_refused'
        with Session(self.engine) as s,s.begin():
            self.store._runtime(s,tenant,account);owner=s.get(MoxyAccount,account);event=s.get(MoxyEvent,(account,event_id))
            from llm_client import settle_scoped_synthetic_call
            settle=model.settle if live else settle_scoped_synthetic_call
            cost=settle(s,account_id=account,call_id=event.model_call_id,reserved_millicents=event.reserved_millicents,
                actual_millicents=model.reported_cost_millicents if model.completed else None,now=utcnow())
            event.charged_millicents=cost
            if cost is None:reason='moxy_model_usage_unknown'
            if event.snapshot_hash!=digest(snapshot(s,account,event.fan_id,event)):
                event.status='stale';event.reason='moxy_snapshot_changed';return event_id
            if reason:event.status='unknown' if reason.endswith('unknown') else 'held';event.reason=reason;return event_id
            from .pilot import route_specialist
            result=route_specialist(json.loads(owner.settings_json),result)
            try:
                # Validate evidence before retaining a fan constraint, including no-send turns.
                from .observations import observations
                observations(result,snap)
                if owner.mode!='preview':observe_input(s,event,result)
                validate_decision(json.loads(owner.settings_json),snap,result)
            except (ActionBlocked,InvalidConfiguration) as exc:
                event.status='held';event.reason=str(exc);event.decision_json=canonical(result);return event_id
            event.decision_json=canonical(result)
            if owner.mode=='preview':event.status='preview';return event_id
            # Our own newly recorded input constraints are part of the frozen
            # decision. Future independent input still invalidates this version.
            snap=snapshot(s,account,event.fan_id,event)
            event.snapshot_json=canonical(snap);event.snapshot_hash=digest(snap)
            if result['action']=='handover':
                from onlyfans_runtime.operator_cases import open_case
                category=('identity' if 'identity' in result['flags'] else 'distress' if 'danger' in result['flags']
                    else 'complaint' if 'complaint' in result['flags'] else 'other')
                open_case(s,account,event.fan_id,category,reason='moxy_owned_handover',evidence={'event_id':event_id},origin='detector',scope='all',owner=json.loads(owner.settings_json)['operational'].get('assigned_operator'))
                event.status='done';return event_id
            if result['action']=='none':event.status='done';return event_id
            event.status='ready'
        return event_id

    def queue(self,tenant,account,event_id):
        with Session(self.engine) as s,s.begin():
            self.store._runtime(s,tenant,account);event=s.get(MoxyEvent,(account,event_id))
            if event.status=='queued':return event.intent_id
            if event.status!='ready':raise Refused('moxy_decision_not_ready')
            from .parts import active, index
            d=active(event);snap=json.loads(event.snapshot_json)
            terms=validate_decision(json.loads(s.get(MoxyAccount,account).settings_json),snap,d)
            control=s.get(OfConversationState,(account,event.fan_id));fan=event.fan_id
            body=d['text'];revision=control.revision
            # A proved rejection is retained as its own immutable attempt. A
            # bounded rate-limit retry receives a new idempotency key.
            command_key='moxy:'+event_id+':part:'+str(index(event))
            if event.reason=='provider_rate_limit_retry' and event.intent_id:
                command_key=digest({'moxy_retry_event':event_id,'rejected_intent':event.intent_id,'part':index(event)})
        return self.authority.prepare(tenant,account,fan,command_key=command_key,
            expected_revision=revision,body=body,origin='automated',actor='moxy-owner',moxy_event_id=event_id,
            catalog_item_id=terms['item']['id'] if terms else None,
            quoted_price_cents=terms['price_cents'] if terms else None)

    def step(self,tenant,account,model,*,limit=4):
        if type(limit) is not int or not 1<=limit<=16:raise Refused('invalid_moxy_batch')
        with Session(self.engine) as s,s.begin():
            self.store._runtime(s,tenant,account)
            # A restart never repeats a possibly paid model invocation.
            for e in s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,MoxyEvent.status=='generating',MoxyEvent.lease_until<utcnow())):
                e.status='unknown';e.reason='interrupted_moxy_generation'
            for e in s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,MoxyEvent.status=='queued')):
                intent=s.get(OfTextIntent,(account,e.intent_id))
                if intent and intent.status=='not_accepted':
                    evidence=intent.evidence_ref or ''
                    if evidence.startswith('fastt-auth-failed:'):
                        e.status='held';e.reason='provider_authentication_required'
                    elif evidence.startswith('fastt-rate-limited:'):
                        attempts=s.scalar(select(func.count()).select_from(OfTextIntent).where(
                            OfTextIntent.account_id==account,OfTextIntent.moxy_event_id==e.event_id,
                            OfTextIntent.status=='not_accepted'))
                        policy=s.get(OfDeliveryPolicy,account)
                        if attempts>=3:
                            e.status='held';e.reason='provider_retry_limit_reached'
                        elif json.loads(e.payload_json).get('campaign_id'):
                            e.status='held';e.reason='campaign_provider_recovery_required'
                        elif s.get(MoxyAccount,account).mode!='on':
                            e.status='held';e.reason='moxy_off'
                        elif max((t for t in (policy.provider_cooldown_until,policy.next_dispatch_at)
                                  if t is not None),default=utcnow())>utcnow():
                            e.reason='provider_rate_limited'
                        elif digest(snapshot(s,account,e.fan_id,e))!=e.snapshot_hash:
                            e.status='stale';e.reason='moxy_snapshot_changed'
                        else:
                            e.status='ready';e.reason='provider_rate_limit_retry'
                    else:
                        e.status='stale';e.reason=intent.reason
                elif intent and intent.status in {'cancelled','refused'}:
                    e.status='stale';e.reason=intent.reason
            for e in s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,MoxyEvent.status=='ready')):
                if digest(snapshot(s,account,e.fan_id,e))!=e.snapshot_hash:
                    e.status='stale';e.reason='moxy_snapshot_changed'
            s.flush()
            owner=s.get(MoxyAccount,account)
            surface_campaign_recovery(s,account,owner)
            if owner is not None and owner.mode=='on':
                # Owed fulfilment is Moxy-initiated work; Preview never advances live state.
                from .outreach import produce
                produce(s,account,json.loads(owner.settings_json))
            busy=set(s.scalars(select(MoxyEvent.fan_id).where(MoxyEvent.account_id==account,
                MoxyEvent.status.in_(('generating','queued','unknown')))))
            candidates=s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,
                MoxyEvent.status.in_(('pending','ready')),
                MoxyEvent.fan_id.not_in(busy),
                (MoxyEvent.not_before.is_(None))|(MoxyEvent.not_before<=utcnow()))
                .order_by(MoxyEvent.created_at,MoxyEvent.event_id).limit(256)).all()
            ids=[]
            for e in candidates:
                if e.fan_id in busy:continue
                busy.add(e.fan_id);ids.append(e.event_id)
                if len(ids)==limit:break
        # Isolate mutable invocation/cost state for every model call. Network
        # work for independent fans overlaps; the durable fan/event fences remain.
        from concurrent.futures import ThreadPoolExecutor
        from copy import copy
        def prepare_one(identity):
            invocation=model.fork() if hasattr(model,'fork') else copy(model)
            self.prepare(tenant,account,identity,invocation)
            with Session(self.engine) as s,s.begin():
                self.store._runtime(s,tenant,account)
                e=s.get(MoxyEvent,(account,identity))
                if e.status in {'held','stale','done'}:
                    from .campaigns import retire
                    retire(s,e)
            try:self.queue(tenant,account,identity);return 1
            except Refused:return 0
        with ThreadPoolExecutor(max_workers=min(4,len(ids) or 1)) as pool:
            return sum(pool.map(prepare_one,ids))


def surface_campaign_recovery(s,account,owner):
    """A campaign rejected by the provider becomes an owned, visible operator case.

    The confirmed rejection, its receipt and the Moxy decision stay untouched; nothing is
    re-sent automatically. The case is non-blocking (scope 'offer'), so ordinary chat with
    the fan continues, and it is created once per event even after an operator resolves it.
    """
    from db.onlyfans_case_models import OfOperatorCase
    from onlyfans_runtime.operator_cases import open_case
    assigned=json.loads(owner.settings_json)['operational'].get('assigned_operator') if owner is not None else None
    for e in s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,MoxyEvent.status=='held',
            MoxyEvent.reason=='campaign_provider_recovery_required')).all():
        if s.scalar(select(OfOperatorCase.case_id).where(OfOperatorCase.account_id==account,OfOperatorCase.fan_id==e.fan_id,
                OfOperatorCase.category=='campaign_recovery',OfOperatorCase.evidence_json.like('%'+e.event_id+'%')).limit(1)):
            continue
        open_case(s,account,e.fan_id,'campaign_recovery',reason='campaign_provider_recovery_required',
            evidence={'event_id':e.event_id,'campaign_id':json.loads(e.payload_json).get('campaign_id'),'intent_id':e.intent_id},
            origin='detector',scope='offer',owner=assigned)


def current(s,intent):
    owner=s.get(MoxyAccount,intent.account_id)
    if owner is None:return intent.moxy_event_id is None
    if intent.origin=='manual':return True
    if not intent.moxy_event_id or owner.mode!='on':return False
    event=s.get(MoxyEvent,(intent.account_id,intent.moxy_event_id))
    if not event or event.status!='queued' or event.intent_id!=intent.intent_id or event.fan_id!=intent.fan_id:return False
    payload=json.loads(event.payload_json)
    if payload.get('campaign_id'):
        from .campaigns import validate
        try:validate(s,intent.account_id,intent.fan_id,event,intent_id=intent.intent_id)
        except Refused:return False
    if event.charged_millicents is None:return False
    call=s.get(GrokCall,event.model_call_id)
    if not call or call.status!='done' or call.account_id!=intent.account_id or call.fan_id!=intent.fan_id or call.cost_cents!=event.charged_millicents:return False
    from .parts import active
    decision=active(event)
    if decision['action'] in {'invite','offer','resend','alternative','continue'}:
        from onlyfans_runtime.operator_cases import policy_facts
        if any(scope in {'all','commercial'} for _,scope in policy_facts(s,intent.account_id,intent.fan_id)[2]):return False
    snap=snapshot(s,intent.account_id,intent.fan_id,event)
    return digest(snap)==event.snapshot_hash and decision['text']==intent.body


def delivered(s,intent):
    if not intent.moxy_event_id:return
    e=s.get(MoxyEvent,(intent.account_id,intent.moxy_event_id))
    if e.status=='done':return
    from .parts import active, has_remaining, continuation_unchanged, plan, index
    d=active(e)
    continuation_ok=True
    # A completed single-message reply needs no continuation snapshot. Keep the
    # extra consistency read only for a tail we may actually dispatch.
    if index(e)+1<len(plan(json.loads(e.decision_json))):
        before=json.loads(e.snapshot_json)
        after=snapshot(s,intent.account_id,intent.fan_id,e)
        continuation_ok=continuation_unchanged(before,after,intent.provider_message_id,d)
    payload=json.loads(e.payload_json)
    progress=payload.setdefault('_moxy_delivery',{'receipts':[]})
    progress['receipts'].append({'intent_id':intent.intent_id,'message_id':intent.provider_message_id})
    e.payload_json=canonical(payload)
    mf=_fan(s,intent.account_id,intent.fan_id);state=json.loads(mf.state_json)
    if d['action']=='invite':
        cfg,pa=configuration(json.loads(s.get(MoxyAccount,intent.account_id).settings_json))
        state['invitation']={'product_id':d['product_id'],'fingerprint':pa.fingerprint,'sequence':d['sequence']}
    elif d['action'] in {'offer','continue','resend','alternative'}:
        state.pop('invitation',None)
        if d.get('request_kind') in {'direct','accept'}:
            state['ordinary_chat_only']=state['sales_refused']=False
    if d['question']:
        obligations=state.setdefault('obligations',[])
        if not any(o.get('question')==d['question'] and o['status']=='open' for o in obligations):
            obligations.append({'id':e.event_id,'kind':'buying_question','question':d['question'],'status':'open','owner':'moxy','source_event':e.event_id})
    if d['resolve_obligation']:
        for o in state.get('obligations',[]):
            if o['id']==d['resolve_obligation']:o['status']='fulfilled';o['message_id']=intent.provider_message_id
    mf.state_json=canonical(state);mf.revision+=1;mf.updated_at=utcnow();e.updated_at=utcnow()
    e.status='done'
    if has_remaining(e):
        if not continuation_ok:
            e.status='stale';e.reason='moxy_conversation_interrupted'
        else:
            e.status='ready';e.intent_id=None
            snap=snapshot(s,intent.account_id,intent.fan_id,e)
            e.snapshot_json=canonical(snap);e.snapshot_hash=digest(snap)

