"""Moxy's durable single policy owner over the canonical CRM delivery authority.

All network work uses the existing scoped model/transport adapters. No process
startup activates an account, and no failure falls back to another writer.
"""
from __future__ import annotations
import asyncio
from datetime import timedelta
import json
import uuid
from sqlalchemy import select, func, case
from sqlalchemy.orm import Session
from db.models import (AccountAiConfig, Fan, Message, CatalogItem, VaultItem, ContentOffer,
    OfConversationState, GrokCall, GrokDailyCost)
from db.onlyfans_models import OfWork
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_moxy_models import MoxyAccount, MoxyFan, MoxyEvent, MoxyAudit
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.native_context import canonical, versions
from .configuration import load, load_foundation, CreatorConfiguration, ActionBlocked, InvalidConfiguration, digest
from .authority import ProductAuthority, readiness
from .parts import plan as _plan, reused_wording, repeated_habit
from . import writer
from .diagnostics import record as diagnose

EVENTS={'turn','welcome','invitation','scheduled','campaign','purchase','refund','chargeback','fulfilment'}

# Artificial reply pause (seconds). The floor keeps a reply from ever being instant; the
# ceiling keeps a bad setting from silently stalling conversations.
REPLY_DELAY_DEFAULTS={'MOXY_REPLY_DELAY_ACTIVE':(8.0,25.0,12.0),'MOXY_REPLY_DELAY_IDLE':(15.0,40.0,25.0)}
REPLY_DELAY_FLOOR=5.0
REPLY_DELAY_CEILING=300.0


def reply_delay_bounds(active):
    """(low, high, mode) for random.triangular. Env value is "low,high,mode"; anything malformed,
    below the 5 s floor, above 300 s or out of order falls back to the default (never raises)."""
    import os
    name='MOXY_REPLY_DELAY_ACTIVE' if active else 'MOXY_REPLY_DELAY_IDLE'
    default=REPLY_DELAY_DEFAULTS[name]
    raw=os.environ.get(name)
    if not raw:return default
    try:
        low,high,mode=(float(part) for part in raw.split(','))
    except ValueError:
        return default
    if not (REPLY_DELAY_FLOOR<=low<=mode<=high<=REPLY_DELAY_CEILING):return default
    return low,high,mode


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
            # Human-like pause, counted from the moment the fan message was ingested.
            # Defaults triangular(8,25,12) in a live conversation, (15,40,25) otherwise;
            # tunable with MOXY_REPLY_DELAY_ACTIVE / MOXY_REPLY_DELAY_IDLE ("low,high,mode").
            active=s.scalar(select(MoxyEvent.event_id).where(MoxyEvent.account_id==account,
                MoxyEvent.fan_id==fan,MoxyEvent.status=='done',MoxyEvent.updated_at>=now-timedelta(minutes=5)).limit(1)) is not None
            source=s.get(Message,(account,fan,payload.get('source_message_id'))) if payload.get('source_message_id') else None
            received=source.ingested_at if source is not None and source.ingested_at is not None else now
            if isinstance(received,str):received=__import__('datetime').datetime.fromisoformat(received)
            low,high,mode=reply_delay_bounds(active)
            row.not_before=received+timedelta(seconds=random.triangular(low,high,mode))
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
                'description_basis':limitations(v,include_freshness=True),'operator_corrected':operator_corrected(v)})
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
    from .conversation import pending_messages
    if decision['question'] and any(m['direction']=='in' and decision['question'] in m['text']
            for m in json.loads(event.snapshot_json)['history']+pending_messages(json.loads(event.snapshot_json))):
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


def _card_facts(r,inventory,offers):
    """Paid status and (only when evidenced) product of an outgoing priced card.

    Paid is true only with is_paid and purchased_at (the same test used for
    purchased_products), false for an explicit unpaid PPV, otherwise unknown.
    The product comes from the recorded offer for that message, else from a
    unique match of the sent media against a product's paid assets.
    """
    if r.direction!='out' or not (r.price_cents or 0)>0: return {}
    paid=True if (r.is_paid is True and r.purchased_at is not None) else False if r.is_paid is False else None
    facts={'paid':paid}
    keys=[k for k,t in inventory.items() for o in offers if o.offer_message_id==r.message_id and o.item_id==t['item']['id']]
    if not keys:
        try: sent=set(json.loads(r.media_ids or '[]'))
        except (TypeError,ValueError): sent=set()
        keys=[k for k,t in inventory.items() if sent and sent==set(t['media_ids'])-set(t['preview_ids'])]
    if len(set(keys))==1: facts['product_id']=keys[0]
    return facts


def offer_eligibility(pa,facts,payload):
    """Per product: 'ok' when ProductAuthority would allow a new offer to this fan now, else the blocking reason.

    Read-only projection of the same authority used at validation. In a bounded correction the
    refused product is reported as not offerable so the writer cannot pick it again; it stays in
    history and the products map so the writer can still answer questions about it.
    """
    rejected=payload.get('rejected') if isinstance(payload,dict) else None
    refused=rejected.get('product_id') if isinstance(rejected,dict) else None
    result={}
    for key in pa.products:
        if key==refused:
            result[key]='refused_in_this_turn';continue
        memberships=[name for name,steps in pa.sequences.items() if key in steps] or [None]
        reason=None
        for sequence in memberships:
            try:
                pa.terms(key,facts,purpose='offer',sequence=sequence);reason='ok';break
            except ActionBlocked as exc:
                reason=reason or str(exc)
        result[key]=reason
    return result


def _reply_retirement_at(s, account, fan):
    # Configuration audits use canonical JSON with sorted keys. Only an explicit
    # discard command establishes a cutoff; toggles and policy edits do not.
    cutoff = s.scalar(select(func.max(MoxyAudit.created_at)).where(
        MoxyAudit.account_id == account,
        MoxyAudit.after_json.startswith('{"discard_obsolete_replies":true,', autoescape=True),
    ))
    from onlyfans_runtime.send_recovery import RETIRED_UNCONFIRMED_TEXT
    retired_send = s.scalar(select(func.max(OfTextIntent.wire_started_at)).join(
        MoxyEvent, (MoxyEvent.account_id == OfTextIntent.account_id)
        & (MoxyEvent.intent_id == OfTextIntent.intent_id),
    ).where(MoxyEvent.account_id == account, MoxyEvent.fan_id == fan,
            MoxyEvent.reason == RETIRED_UNCONFIRMED_TEXT))
    return max((stamp for stamp in (cutoff, retired_send) if stamp is not None), default=None)


def snapshot(s,account,fan,event,*,ignore_outgoing_ids=()):
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
    try:owned=sorted(owned_media_in_session(s,account,fan,limit=10000))
    except ValueError as exc:raise Refused('ownership evidence unavailable') from exc
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
        **({'constraint_evidence':memo['constraint_evidence']} if memo.get('constraint_evidence') else {}),
        'previously_sent_assets':exposed,'paid_assets_by_product':paid_assets,
        'exposure_history_verified':exposure_verified,'purchased_products':sorted(set(purchased)),
        'pending_products':sorted(set(pending)),'rejected_products':memo.get('rejected_products',[]),
        'budget_cents':memo.get('budget_cents',verified.get('budget_cents')),
        'requested_resend_product':memo.get('requested_resend_product'),
        'ownership_verified':verified.get('ownership_verified',False),
        'purchase_links_verified':verified.get('purchase_links_verified',False)}
    offerable=offer_eligibility(pa,facts,json.loads(event.payload_json))
    rows=s.scalars(select(Message).where(Message.account_id==account,Message.fan_id==fan,Message.is_unsent.is_(False))
        .order_by(Message.created_at.desc(),Message.message_id.desc()).limit(100)).all()
    # Card state is evidence, not a sliding conversation window. Preserve old cards
    # separately, so a recent unrelated locked card cannot overwrite a paid item.
    card_rows=s.scalars(select(Message).where(Message.account_id==account,Message.fan_id==fan,
        Message.is_unsent.is_(False),Message.direction=='out',Message.price_cents>0)
        .order_by(Message.created_at,Message.message_id)).all()
    card_state=[{'id':r.message_id,'direction':'out','text':r.body or '','at':r.created_at,
        'price_cents':r.price_cents,**_card_facts(r,inventory,offers)} for r in card_rows]
    from db.models import OfMessageDelivery
    receipts={x.message_id:x.origin for x in s.scalars(select(OfMessageDelivery).where(
        OfMessageDelivery.account_id==account,OfMessageDelivery.fan_id==fan))}
    history=[{'id':r.message_id,'direction':r.direction,'text':r.body or '','at':r.created_at,
        'attachment_description':r.image_desc,'media_count':r.media_count,
        'sender':('fan' if r.direction=='in' else 'ai' if receipts.get(r.message_id)=='ai' else
            'human' if r.sent_by_employee_id or receipts.get(r.message_id)=='human' else 'unattributed'),
        'price_cents':r.price_cents,**_card_facts(r,inventory,offers)} for r in reversed(rows)]
    # Older invitations predate receipt identity in fan state. Recover that
    # identity from the recorded Moxy plan/receipts, never from caption wording.
    invitation=memo.get('invitation')
    if isinstance(invitation,dict) and 'message_id' not in invitation:
        for prior in s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,MoxyEvent.fan_id==fan,
                MoxyEvent.status=='done',MoxyEvent.decision_json.is_not(None)).order_by(MoxyEvent.updated_at.desc()).limit(100)):
            proposal=json.loads(prior.decision_json)
            progress=json.loads(prior.payload_json).get('_moxy_delivery',{}).get('receipts',[])
            if proposal.get('action')=='invite' and proposal.get('product_id')==invitation.get('product_id') and progress:
                invitation={**invitation,'message_id':progress[-1]['message_id']};break
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
    from .conversation import LEGACY_TECHNICAL_FALLBACK
    # A later incoming message fences obsolete generation, but does not answer
    # earlier messages in the same batch. A legacy technical fallback is not an answer.
    own_receipts={v['message_id'] for v in json.loads(event.payload_json).get('_moxy_delivery',{}).get('receipts',[])}|set(ignore_outgoing_ids)
    # Only a receipted conversational turn acknowledges an incoming batch.
    # Campaigns, unsolicited promotions and unknown outgoing text do not.
    completed=s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,MoxyEvent.fan_id==fan,
        MoxyEvent.event_id!=event.event_id,MoxyEvent.kind.in_(('turn','welcome')),MoxyEvent.status=='done')
        .order_by(MoxyEvent.updated_at.desc())).all()
    # Pair progress with the exact delivered input boundary. Searching older
    # records for a nonempty annotation revives questions already superseded.
    answered=[]
    for previous in completed:
        payload=json.loads(previous.payload_json)
        receipts=payload.get('_moxy_delivery',{}).get('receipts',[])
        if not receipts or any(v['message_id'] in own_receipts for v in receipts):continue
        if json.loads(previous.decision_json or '{}').get('reason')=='moxy_validated_technical_fallback':continue
        mid=payload.get('source_message_id')
        message=s.get(Message,(account,fan,mid)) if type(mid) is int else None
        if message is not None:answered.append((message,previous,payload))
    latest=max(answered,key=lambda v:(v[0].created_at,v[0].message_id,v[1].updated_at)) if answered else None
    boundary=latest[0] if latest else None
    annotation=latest[2].get('_moxy_conversation_progress') if latest else None
    progress_known=(type(annotation) is dict and annotation.get('version')==1
        and type(annotation.get('unanswered_input_ids')) is list
        and all(type(i) is int and i>0 for i in annotation['unanswered_input_ids']))
    retained=annotation['unanswered_input_ids'] if progress_known else []
    from db.onlyfans_models import OfIngress,OfRuntime
    from sqlalchemy import or_,and_
    runtime=s.get(OfRuntime,account)
    # History imports remain context. Only applied live ingress, explicit source
    # identity, or a delivered plan's retained IDs create current pending work.
    live=select(OfIngress.message_id).where(OfIngress.account_id==account,OfIngress.fan_id==fan,
        OfIngress.binding_id==owner.binding_id,OfIngress.status=='applied',OfIngress.source!='import')
    current=and_(Message.message_id.in_(live),Message.created_at>=runtime.activated_at)
    source=json.loads(event.payload_json).get('source_message_id')
    if type(source) is int:current=or_(current,Message.message_id==source)
    if boundary is not None:
        current=and_(current,or_(Message.created_at>boundary.created_at,
            and_(Message.created_at==boundary.created_at,Message.message_id>boundary.message_id)))
    batch_query=select(Message).where(Message.account_id==account,Message.fan_id==fan,
        Message.direction=='in',Message.is_unsent.is_(False),or_(current,Message.message_id.in_(retained)))
    # The transcript and fulfilment obligations survive retirement. Only the
    # active input batch (including retained IDs) is fenced by the cutover.
    retired_at = _reply_retirement_at(s, account, fan)
    if retired_at is not None:
        batch_query = batch_query.where(Message.created_at >= retired_at)
    batch_rows=list(s.scalars(batch_query.order_by(Message.created_at,Message.message_id).limit(129)))
    if len(batch_rows)>128:raise ActionBlocked('moxy_input_batch_limit')
    batch=[{'id':m.message_id,'direction':'in','sender':'fan','text':m.body or '', 'at':m.created_at,
        'attachment_description':m.image_desc,'media_count':m.media_count} for m in batch_rows]
    from .input_batch import explicit_updates
    from .observations import apply as apply_batch
    facts=apply_batch(facts,explicit_updates(batch))
    offerable=offer_eligibility(pa,facts,json.loads(event.payload_json))
    result={'pending_fan_messages':batch,'history_scope':{'kind':'rolling_window','limit':100,
        'relationship_start_known':False,'progress_annotation_known':progress_known,
        **({'reply_retirement_at':retired_at} if retired_at is not None else {})},'fan_id':fan,'pilot':settings.get('pilot'),'account_revision':owner.revision,'binding_id':owner.binding_id,'configuration_fingerprint':pa.fingerprint,
        'instructions':cfg.public_context(),'public_facts':settings.get('public_facts',{}),
        'fan_state':facts,'fan_revision':mf.revision,'control':state_view,'event':{'kind':event.kind,'payload':json.loads(event.payload_json)},
        'history':history,'card_state':card_state,'memory':memory,'invitation':invitation,
        'obligations':memo.get('obligations',[]),'products':{k:pa.products[k] for k in inventory},
        'offer_eligibility':{k:offerable.get(k,'product_mapping_missing') for k in inventory},
        'inventory':inventory,'sequences':pa.sequences,'operational':settings['operational'],
        'approved_remedies':settings.get('approved_remedies',{}),'native_fingerprint':digest(json.loads(canonical(native)))}
    # His usable name goes to the writer; with none it is simply absent and never asked for.
    name=usable_fan_name(native.get('fan'),memory)
    if name:result['fan_name']=name
    from .campaigns import context as campaign_context
    result['campaign']=campaign_context(s,account,fan,json.loads(event.payload_json))
    result=json.loads(canonical(result))
    from .state import commercial_state
    result['commercial_state']=commercial_state(result)
    return result


def _norm(text):
    # Quote evidence tolerates only case, whitespace and typographic quote differences.
    return ' '.join(str(text).translate(str.maketrans('\u2018\u2019\u201c\u201d','\'\'""')).lower().split())


COMMERCIAL_ACTIONS={'invite','offer','resend','alternative','continue','correct','fulfil'}

# Validation failures about the chosen product or its current eligibility, where a different
# eligible product or a text-only answer may still be right. Everything else (stop controls,
# human ownership, safety, identity, timing, campaign and remedy authority, config) is final.
RECOVERABLE_REASONS={'media_previously_sent_requires_explicit_resend','acceptance_product_mismatch','real_inventory_missing_or_stale',
    'product_unavailable','product_mapping_missing','current_request_evidence_missing','request_quote_required',
    'unexpected_product_reference','obligation_cannot_resolve_without_fulfilment','creator_content_prohibited',
    'exact_purchase_links_missing','sequence_mapping_missing','no_push','sales_state_held:ordinary_chat_only',
    'sales_state_held:sales_refused','sales_state_held:boundary_hold','commerce_needs_reconciliation','ownership_evidence_missing',
    'already_owned_use_existing_access','product_previously_rejected','resend_not_requested','continuation_sequence_missing',
    'sequence_context_missing','sequence_step_not_current','price_exceeds_budget','offer_already_pending',
    'invitation_stale_or_ambiguous','product_terms_unresolved','price_below_authorised_minimum',
    'price_differs_from_authorised_fixed','product_media_format_mismatch','product_photo_count_mismatch'}
RECOVERABLE_REASONS.add('product_invitation_missing_identity')
RECOVERABLE_PREFIXES=('condition_not_met:','condition_unknown:')
RECOVERABLE_REASONS|={'pilot_specialist_handover_required','repeated_recent_line','repeated_habit','invented_policy_claim',
    'repeated_recent_phrase','content_status_wording','asks_known_name','known_name_not_used','unsupported_profile_visibility_claim','unsupported_creator_routine','unsupported_asset_gaze','verified_ownership_ignored'}
FACTUAL_REASONS={'unsupported_fan_attribution','unsupported_relationship_history','customer_facing_technical_fallback',
    'unsupported_creator_routine','unsupported_asset_gaze','content_status_wording','verified_ownership_ignored',
    'known_name_not_used','unsupported_profile_visibility_claim'}
FACTUAL_REASONS|={'unsupported_catalogue_prohibition','unsupported_price_units','unsupported_card_access_denial','sexting_permission_unverified','unsupported_asset_absence'}
FACTUAL_REASONS|={'price_claim_mismatch','price_claim_unbound','price_question_unanswered'}
FACTUAL_REASONS|={'invalid_conversation_progress','unsupported_roleplay_boundary'}
FACTUAL_REASONS|={'unsupported_remedy_commitment','unsupported_service_commitment','delivery_promise_without_action',
    'sent_claim_without_receipt','unapproved_discount_claim','unapproved_real_world_commitment',
    'unsupported_card_content_claim'}
REQUEST_EVIDENCE_REASONS={'current_request_authority_unverified','current_request_evidence_missing','request_quote_required','resend_not_requested','invitation_stale_or_ambiguous'}
FACTUAL_REASONS|=REQUEST_EVIDENCE_REASONS
RECOVERABLE_REASONS|=FACTUAL_REASONS|{'product_request_mismatch'}

# Unusable output for the same input gets one bounded correction before internal operator attention.
FORMAT_REASONS={'invalid_model_json','invalid_model_decision','invalid_message_plan','invalid_message_part',
    'message_plan_media_mismatch','message_plan_text_mismatch','empty_model_reply','duplicate_model_key',
    'invalid_request_evidence','unsupported_output_markup_or_link','handover_must_not_have_messages',
    'handover_must_not_reply','untruthful_identity_or_instruction_leak','owned_handover_required','invalid_conversation_progress'}


def recoverable(reason):
    return reason in RECOVERABLE_REASONS or reason.startswith(RECOVERABLE_PREFIXES)


import re as _policy_re
INVENTED_POLICY=_policy_re.compile(
    r"\bno\s+(any\s+)?free(bies| stuff| pics?| samples?)?\b|\bfree(bies| stuff| samples?)?\s+(isn'?t|aren'?t|is\s+not|are\s+not|is\s+never)\b"
    r"|\bdon'?t\s+(really\s+)?do\s+(any\s+)?free|\bnot\s+how\s+i\s+(work|roll|operate)\b|\bnever\s+(send|give|do)\s+free",_policy_re.I)


# Wording failures: the product decision may be fine, only the words are refused. A correction for these
# rewrites the wording and keeps an otherwise valid offer; every other recoverable reason is a product failure.
WORDING_REASONS={'repeated_recent_line','repeated_habit','repeated_recent_phrase','invented_policy_claim','content_status_wording','asks_known_name',
    'known_name_not_used','unsupported_profile_visibility_claim','unsupported_creator_routine','unsupported_asset_gaze','verified_ownership_ignored'}

_VIEWED_CLAIM=_policy_re.compile(
    r"\byou(?:'ve|\s+have)?\s+(?:already\s+)?(?:opened|unlocked|seen|watched|viewed|checked\s+out|looked\s+at|peeked)\b"
    r"|\bsince\s+you\s+(?:opened|unlocked|saw|watched|viewed)\b"
    r"|\b(?:hope|glad|happy|love|loved)\s+(?:that\s+)?you\s+(?:enjoyed|liked|loved|opened|unlocked|saw|watched)\b"
    r"|\bdid\s+you\s+(?:like|love|enjoy|open|unlock|see|watch|view)\s+(?:it|that|this|them|the\s+\w+|my\s+\w+)"
    r"|\bwhat\s+did\s+you\s+think\s+(?:of|about)\s+(?:it|that|this|them|the\s+\w+|my\s+\w+)"
    r"|\byou\s+(?:already\s+)?(?:own|have)\s+(?:it|that|this|them)\b",_policy_re.I)
_ASKS_NAME=_policy_re.compile(
    r"\bwhat(?:'?s|s|\s+is)?\s+(?:your|ur)\s+name\b|\bwhat\s+(?:should|can|do)\s+i\s+call\s+you\b|\bwhat\s+do\s+you\s+go\s+by\b"
    r"|\bwho\s+(?:am\s+i\s+talking\s+to|are\s+you)\b|\bremind\s+me\s+(?:of\s+)?your\s+name\b",_policy_re.I)
_UNUSABLE_NAMES={'guest','user','fan','anonymous','unknown','daddy','sir','man','dude','null','none','nan','na'}
_UNUSABLE_CHARS=_policy_re.compile(r"[@/\\_]|https?:|www\.|\.com",_policy_re.I)
_NAME_LOOKUP=_policy_re.compile(r"\bwhat(?:'?s|\s+is)\s+my\s+name\b|\b(?:remember|know)\s+my\s+name\b",_policy_re.I)
_PROFILE_VISIBILITY=_policy_re.compile(
    r"\b(?:can'?t|cannot|don'?t|doesn'?t|unable\s+to)\s+(?:see|read|show)(?:\s+\w+){0,6}\s+names?\b"
    r"|\b(?:my\s+side|it)\s+(?:just\s+)?(?:shows?\s+me\s+(?:a\s+)?number|doesn'?t\s+show\s+me\s+names)",_policy_re.I)


def clean_name(value):
    """A fan's first name when the value reads as a name; None for handles, ids, initials and labels."""
    if type(value) is not str or _UNUSABLE_CHARS.search(value):return None
    cleaned=_policy_re.sub(r"[^\w\s'.\-]",' ',value.replace('_',' '))
    if _policy_re.search(r"\d",cleaned):return None            # Redddd1, bigobj7, u12345
    words=[w.strip(".-'") for w in cleaned.split()]
    words=[w for w in words if w]
    if not words:return None
    first=words[0]
    if len(first)<2 or len(first)>20 or (first.isupper() and len(first)<=3) or first.lower() in _UNUSABLE_NAMES:return None
    if not all(c.isalpha() or c in "'-" for c in first):return None
    return first.capitalize() if first.islower() or first.isupper() else first


def usable_fan_name(fan,memory):
    """Best usable name: verified memory, a name he chose, a real name, then his profile display name."""
    facts=(memory or {}).get('facts') or {}
    remembered=facts.get('real_name') if isinstance(facts,dict) else None
    candidates=[remembered.get('value') if isinstance(remembered,dict) else None]
    if fan:
        candidates+=[fan.get('fan_chosen_nickname'),fan.get('real_name') if fan.get('is_name_real') is not False else None,
            fan.get('of_display_name')]
    for value in candidates:
        name=clean_name(value)
        if name:return name
    return None


def content_status_violation(snap,texts):
    """Ground a viewing/ownership reference in that item's evidence, never a keyword in fan text.

    Questions about an identified paid item remain allowed. A fan's own report is
    conversational evidence only, never verified access or a purchase projection.
    """
    history=snap.get('history') or []
    cards=snap.get('card_state')
    if cards is None:
        cards=[m for m in history if m.get('direction')=='out' and (m.get('price_cents') or 0)>0]
    if not cards:return False
    def references(text):
        found=[]
        for card in cards:
            pid=card.get('product_id')
            # Explicit product/card ids are strongest; a distinctive caption can
            # identify a historical card even when it no longer has a catalogue binding.
            labels=[str(card['id'])] if card.get('id') is not None else []
            if pid:labels.append(pid.replace('_',' '))
            caption=(card.get('text') or '').strip()
            if len(caption)>=6:labels.append(caption)
            if any(_policy_re.search(r'(?<!\w)'+_policy_re.escape(label)+r'(?!\w)',text,_policy_re.I)
                   for label in labels):found.append(card)
        return found
    incoming=[m.get('text') or '' for m in history if m.get('direction')=='in']
    for text in texts:
        if not _VIEWED_CLAIM.search(text):continue
        preview_available=any(m.get('direction')=='out' and m.get('media_count',0)>0
            and not (m.get('price_cents') or 0)>0 for m in history)
        preview_available=preview_available or any(snap.get('inventory',{}).get(c.get('product_id'),{}).get('preview_ids')
            for c in cards)
        # A question about an accessible free preview does not assert that the
        # separate locked/full item was bought or watched.
        if (preview_available and _policy_re.search(r'\bdid\s+you\s+(?:like|enjoy|see|watch).*\bpreview\b',text,_policy_re.I)
                and not _policy_re.search(r'\b(?:full|paid|unlocked|bought|own)\b',text,_policy_re.I)):
            continue
        targets=references(text) or (references(incoming[-1]) if incoming else [])
        if not targets and _policy_re.search(r'\b(?:earlier|previous|first)\b',text,_policy_re.I):
            targets=cards[:1]
        if not targets:targets=cards[-1:]
        reported=incoming[-1] if incoming else ''
        positive_report=_policy_re.search(r"\bi(?:'ve|\s+have)?\s+(?:just\s+|already\s+)?(?:opened|unlocked|watched|viewed|bought)\s+(?:it|that|this)\b",reported,_policy_re.I)
        negated=_policy_re.search(r"\b(?:not|never|haven'?t|hasn'?t|didn'?t|cannot|can'?t)\b",reported,_policy_re.I)
        assertion=_policy_re.search(r"\byou(?:'ve|\s+have)?\s+(?:already\s+)?(?:opened|unlocked|own|have|watched|seen|viewed)\b|\bsince\s+you\b",text,_policy_re.I)
        # Acknowledge an explicit report about this card without promoting that
        # report to a verified purchase or an assertion of ownership/opening.
        reported_targets=references(reported) or cards[-1:]
        if (positive_report and not negated and not assertion
                and {c.get('id') for c in targets}=={c.get('id') for c in reported_targets}):continue
        # A paid receipt establishes access, not proof of watching. The existing
        # guard allows conversational questions about purchased media.
        if any(card.get('paid') is not True for card in targets):return True
    return False


def _recent_outgoing_for_repetition(snap):
    payload = snap.get('event', {}).get('payload', {})
    # A later bubble must not be compared to another bubble from its own send.
    own = {receipt.get('message_id') for receipt in payload.get('_moxy_delivery', {}).get('receipts', [])}
    return [m['text'] for m in snap['history'] if m['direction'] == 'out' and m.get('text')
            and m.get('id') not in own][-12:]


def wording_problem(snap,decision):
    """The wording reason that refuses this decision's text, or None. Product checks run before this."""
    parts=_plan(decision)
    texts=[p['text'] for p in parts]
    from .conversation import history_problem
    problem=history_problem(snap,texts)
    if problem:return problem
    from .grounding import current_record_problem
    problem=current_record_problem(snap,texts)
    if problem:return problem
    from .grounding import commitment_problem
    problem=commitment_problem(snap,decision,texts)
    if problem:return problem
    from .product_grounding import product_problem
    problem=product_problem(snap,decision,texts)
    if problem:return problem
    from .grounding import unsupported_routine,unsupported_asset_gaze
    if unsupported_routine(snap,texts):return 'unsupported_creator_routine'
    if unsupported_asset_gaze(snap,decision,texts):return 'unsupported_asset_gaze'
    if decision['action'] not in {'correct','fulfil'} and content_status_violation(snap,texts):return 'content_status_wording'
    recent=_recent_outgoing_for_repetition(snap)
    from .parts import repetition_texts
    looping=repetition_texts(snap,decision)
    norm_recent={_norm(t) for t in recent[-12:]}
    if any(len(_norm(t))>=12 and _norm(t) in norm_recent for t in looping):
        # Sending the same line again reads as a bot; one bounded correction asks for a fresh reply.
        return 'repeated_recent_line'
    if reused_wording([{'text':t} for t in looping],recent):return 'repeated_recent_phrase'
    # The same pet name or emoji on most recent lines reads as a script; the one correction asks for a different one.
    if repeated_habit(looping,recent,_latest_fan_text(snap)):return 'repeated_habit'
    for index,text in enumerate(looping):
        earlier=looping[:index]
        if (len(_norm(text))>=12 and any(_norm(text)==_norm(old) for old in earlier)) or reused_wording([{'text':text}],earlier):
            return 'repeated_recent_phrase'
    # The captured Amelia thread repeated the same short laugh on successive
    # creator turns; one isolated laugh remains ordinary conversation.
    laugh=lambda text:bool(_policy_re.match(r'^\s*ha(?:ha)*\b',text,_policy_re.I))
    if len(recent)>=3 and all(laugh(t) for t in recent[-3:]) and looping and laugh(looping[0]):
        return 'repeated_recent_phrase'
    if any(INVENTED_POLICY.search(t) for t in texts):
        # No source states a blanket free-content policy; inventing one is refused once, then a person decides.
        return 'invented_policy_claim'
    if snap.get('fan_name') and any(_ASKS_NAME.search(t) for t in texts):return 'asks_known_name'
    if (snap.get('fan_name') and _NAME_LOOKUP.search(_latest_fan_text(snap))
            and not any(_policy_re.search(r'(?<!\w)'+_policy_re.escape(snap['fan_name'])+r'(?!\w)',t,_policy_re.I) for t in texts)):
        return 'known_name_not_used'
    from .grounding import unbound_product_invitation,ignores_verified_ownership
    if ignores_verified_ownership(snap,texts):return 'verified_ownership_ignored'
    if unbound_product_invitation(snap,decision,texts):return 'product_invitation_missing_identity'
    if any(_PROFILE_VISIBILITY.search(t) for t in texts):return 'unsupported_profile_visibility_claim'
    if (_NAME_LOOKUP.search(_latest_fan_text(snap)) and any(_policy_re.search(
            r"\b(?:can'?t|cannot|don'?t|doesn'?t|unable\s+to)\s+(?:actually\s+)?(?:see|read|access)\b",t,_policy_re.I)
            for t in texts)):
        return 'unsupported_profile_visibility_claim'
    return None


def _latest_fan_text(snap):
    incoming=[m for m in snap.get('history',[]) if m['direction']=='in']
    return (incoming[-1].get('text') or '')[:400] if incoming else ''


def correction_brief(reason,result,snap):
    """What the single bounded retry is told: factual, wording or product repair.

    Factual repair retracts unsupported claims using speaker/asset evidence.
    Wording/factual: the valid offer is NOT marked rejected (product_id stays
    null, the product stays offerable) and is handed back in `keep`. product: the chosen product is
    refused and can not be picked again. Both keep the fan's actual message in view.
    """
    from .conversation import pending_messages
    brief={'reason':reason[:120],'fan_message':_latest_fan_text(snap),
        'fan_messages':pending_messages(snap)}
    if reason in FACTUAL_REASONS:
        brief['evidence']=[{k:m.get(k) for k in ('id','direction','sender','text')} for m in snap.get('history',[])[-20:]]
        brief['guidance']='Correct the factual error, not just the wording. Withdraw the unsupported claim plainly, answer all pending questions/corrections, and do not invent an explanation. Only fan messages support what he said; the rolling window never establishes when the relationship began.'
    if reason=='unsupported_roleplay_boundary':
        from .conversation import unverified_history_claims
        brief['unsupported_prior_claims'] = [item for item in unverified_history_claims(snap)
                                             if item['problem'] == reason]
        brief['guidance'] = ('The earlier roleplay refusal is not a creator rule. If asked about it, correct it briefly. '
                             'Answer the actual message under the current conversation permission. '
                             'Do not turn this into a media refusal or imply real-world access.')
    if reason=='unsupported_fan_attribution':
        from .conversation import speaker_evidence
        brief['speaker_evidence']=speaker_evidence(snap,[p['text'] for p in _plan(result)])
        brief['guidance']='The rejected fan attribution has no matching fan message. Check speaker_evidence: the relevant line is in outgoing creator text, not incoming fan text. Plainly acknowledge whose statement it was and answer the challenge. Do not claim he said a different version, and do not invent another person, night, memory or technical problem as an explanation.'
    if reason=='unsupported_asset_gaze':
        brief['asset_facts']=snap.get('products',{}).get(result.get('product_id'),{}).get('assets',[])
        brief['guidance']='Remove the unsupported gaze claim and use only the selected asset facts. Do not repeat a declined request or ask for acceptance again when the fan already requested this content.'
    if reason in {'known_name_not_used','asks_known_name','unsupported_profile_visibility_claim'}:
        brief['known_name']=snap.get('fan_name')
    if reason=='unsupported_creator_routine':
        brief['public_facts']=snap.get('public_facts',{})
        brief['guidance']='Use approved public facts where they answer the actual question. Missing facts do not establish a privacy preference: invent neither a routine nor a privacy refusal. Preserve genuine source restrictions; leave an unanswerable factual question for human review.'
    if reason=='verified_ownership_ignored':
        brief['verified_last_offer']=(snap.get('commercial_state') or {}).get('last_offer')
        brief['guidance']='The exact last card is already in verified_purchases. Answer its ownership question directly; no unverified-payment inference or further check is needed.'
    if reason in {'media_previously_sent_requires_explicit_resend','resend_not_requested'}:
        brief['existing_cards']=[{**{k:c.get(k) for k in ('id','product_id','at','paid','price_cents')},
            'historical_caption_claims':c.get('text'),
            'verified_assets':snap.get('products',{}).get(c.get('product_id'),{}).get('assets',[])}
            for c in snap.get('card_state',[]) if c.get('product_id')==result.get('product_id')]
        brief['new_card_product_ids']=[k for k,v in snap.get('offer_eligibility',{}).items() if v=='ok' and k!=result.get('product_id')]
        brief['guidance']='This item was already sent. The current request does not establish an explicit request to RESEND that same card. Never manufacture that permission, choose the refused product again, or claim it was purchased/opened. Answer about the card already waiting without sending it, or choose a different relevant offerable product. Its original sent_at/at is evidence, not permission to guess a relative local time such as earlier tonight. Do not invent when it was sent. This turn has no authority to send another copy, but that is an internal action check: do not tell him I cannot resend or invent a blanket creator/platform ban. The verified_assets of that exact existing card are the only content authority; historical_caption_claims are what was said, not proof of its pose or contents. Do not borrow another product description. He asked to receive a photo, not for a new description of an old one: when discussing the existing card, answer that actual request without adding unasked-for pose details. A different fresh eligible offer may instead fulfil the request using its own facts. A later explicit resend request can still be revalidated.'
    if reason=='unsupported_asset_absence':
        brief['asset_facts']=[a for p in snap.get('products',{}).values() for a in p.get('assets',[])]
        brief['guidance']='Describe only the approved asset facts. Do not add negative claims such as no toys unless the facts explicitly establish them. Do not specialize a generic garment as a particular clothing type without evidence. Answer the old-card question without another card or implying purchase.'
    if reason=='sexting_permission_unverified':
        from .configuration import permission_context
        brief['permission_status']=permission_context(snap.get('instructions',{}),snap.get('fan_state',{}),'sexting')
        brief['guidance']='Answer both the factual correction and the flirt. Retract the unsupported relationship date without inventing a start. The resolved source permission is false: no new spend or VIP membership exists. Replace the sexual-act description with a different non-explicit interaction; do not describe manual genital contact/arousal or reuse it in euphemism. Keep warmth and creator voice, with no invented boundary, technical excuse or unmapped VIP pitch. Recheck otherwise eligible product terms.'
    if reason=='unsupported_price_units':
        brief['guidance']='The draft used internal cents as customer-facing money. Use verified price_cents divided by100 for dollars and only current offerable prices. Do not invent a cheaper item or discount.'
        brief['verified_offerable_prices']=(snap.get('commercial_state') or {}).get('cheapest_offerable_cents_by_category',{})
    if reason in {'price_claim_mismatch','price_claim_unbound'}:
        brief['guidance']='Bind each monetary claim to the exact selected item or identified existing card, or remove the unsupported claim. Use the supplied current terms for a new card and the recorded receipt terms for a historical card. Do not call a card the cheapest when a cheaper eligible item is shown. A fan budget or a clearly attributed historical quotation is not a new offer price. Preserve the valid item and approved amount; never change the card price to fit your wording.'
        brief['selected_terms']=snap.get('inventory',{}).get(result.get('product_id'))
        brief['verified_cards']=snap.get('card_state',[])
        brief['verified_offerable_prices']=(snap.get('commercial_state') or {}).get('cheapest_offerable_cents_by_category',{})
    if reason=='unsupported_card_access_denial':
        brief['guidance']='The referenced durable card has a positive price and is not a free welcome message. Do not claim nothing needs opening or unlocking. Use its exact verified product mapping; if its content is unknown, handover with no fan text for operator review rather than invent a description.'
        brief['verified_cards']=snap.get('card_state',[])
    if reason=='product_request_mismatch':
        brief['guidance']='The selected item does not match the actual requested body/content or format. Choose a genuinely matching eligible item; if none is mapped, answer honestly about what you can send now. An alternative must explicitly say it is different and accurately describe that different item, never call a booty/twerk item the closest breast content.'
    if reason=='unsupported_catalogue_prohibition':
        brief['guidance']='Missing pilot inventory is not a creator-wide prohibition. Withdraw the invented boundary. State only which verified items you can send right now, while retaining actual source creator prohibitions and conditions.'
    if reason=='product_invitation_missing_identity':
        brief['guidance']='Your text invites a specific catalogue item. Use invite and its actual product_id, without a media card, so acceptance retains its identity.'
    if reason=='repeated_recent_phrase':
        from .parts import repetition_texts, shared_wording_fragment
        earlier = _recent_outgoing_for_repetition(snap)
        matches = []
        for line in repetition_texts(snap, result):
            for previous in earlier:
                fragment = shared_wording_fragment(line, previous)
                if fragment and fragment not in matches:
                    matches.append(fragment)
            earlier.append(line)
        brief['repeated_fragments'] = [fragment[:160] for fragment in matches[:4]]
        brief['guidance'] = ('Avoid the phrases in repeated_fragments from earlier replies, including any moved out '
                             'of normal history as unsupported claims. Answer the current turn in fresh words '
                             'within the same verified permissions and facts.')
    if reason=='repeated_habit':
        brief['guidance']='Your last replies used the same pet name or emoji on every line. Answer the same fan message again in your own voice but with a different pet name or emoji, or none this time. Keep every price, fact and offer exactly as it was, and still state any price or quantity he asked for.'
    if reason=='price_question_unanswered':
        brief['guidance']='He asked what an item costs. State that item\'s exact verified price in your reply, in your own voice; pointing to the card or its display does not answer him. Keep every other fact and offer as it was. Only if you cannot tell which item he means, ask which one instead of guessing a number.'
    if reason in REQUEST_EVIDENCE_REASONS:
        brief['guidance']='Repair the request evidence using the actual current fan message. Missing or unsupported evidence does not ban the item. Keep the proposed product, action and approved terms if current evidence supports them; otherwise answer or clarify in text with no product or media. Do not switch products, invent consent, clear a restriction or claim a general inability to sell.'
    if reason=='invalid_conversation_progress':
        brief['guidance']='Use only IDs from the pending fan messages for unanswered_input_ids. Include only questions or corrections not answered by the whole reply; use an empty list when none remain.'
    from .request_evidence import authorizes_request
    resend_repair=(reason=='media_previously_sent_requires_explicit_resend'
        and result.get('product_id') is not None
        and (authorizes_request(snap,result['product_id'],'resend',result.get('request_quote'))
             or authorizes_request(snap,result['product_id'],'resend')))
    if resend_repair:
        brief['guidance']='The current request uniquely identifies this previously sent item. Repair the action as a resend with the exact current request quote and original product and terms. Revalidate purchase ownership, creator permission, price and delivery state; otherwise clarify without media.'
    if reason in WORDING_REASONS|FACTUAL_REASONS or resend_repair:
        brief.update(kind='factual' if reason in FACTUAL_REASONS or resend_repair else 'wording',action=None,product_id=None,
            avoid=[p['text'][:300] for p in _plan(result)][:4])
        if result.get('product_id') is not None and result['action'] in COMMERCIAL_ACTIONS:
            brief['keep']={'action':'resend' if resend_repair else result['action'],'product_id':result['product_id'],'sequence':result.get('sequence')}
            item=snap.get('inventory',{}).get(result['product_id'],{})
            if all(k in item for k in ('price_cents','media_ids','preview_ids')):
                brief['keep_terms']={k:item[k] for k in ('price_cents','media_ids','preview_ids')}
    else:
        brief.update(kind='product',action=result['action'],product_id=result['product_id'])
        # Product eligibility is checked before wording. The one corrective
        # invocation must see both defects; fixing the item cannot spend a
        # second retry merely to discover an already-evident repeated pitch.
        brief['avoid']=[p['text'][:300] for p in _plan(result)][:4]
        secondary=wording_problem(snap,result)
        if secondary:brief['secondary_rejection']=secondary
    return brief


def validate_decision(settings,snap,decision,*,planning=False,part_index=None):
    # One immutable plan supplies meaning, product claims and prices. Only its
    # current emission supplies wire effects. A text preamble/tail must never
    # become an unrelated product-less decision, nor send the card a second time.
    claim_decision=decision
    if part_index is not None:
        from .parts import emission
        decision=emission(decision,part_index)
    from .pilot import decision as pilot_decision
    pilot_decision(settings,snap,decision)
    rejected=snap.get('event',{}).get('payload',{}).get('rejected')
    if type(rejected) is dict and rejected.get('product_id') is not None and decision['product_id']==rejected['product_id']:
        # A bounded correction may pick another product or answer in text, never the refused one again.
        raise ActionBlocked('correction_repeated_rejected_choice')
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
    from .conversation import pending_messages
    if 'unanswered_input_ids' in decision:
        allowed={m['id'] for m in pending_messages(snap) if m.get('direction')=='in'}
        if any(i not in allowed for i in decision['unanswered_input_ids']):
            raise ActionBlocked('invalid_conversation_progress')
    if decision['question'] is not None and not any(m['direction']=='in' and _norm(decision['question']) in _norm(m['text']) for m in snap['history']+pending_messages(snap)):
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
    if action=='resend' and request_kind!='resend':
        raise ActionBlocked('resend_not_requested')
    if request_kind!='none':
        from .request_evidence import request_source
        if not quote or request_source(snap,quote) is None:
            # Unverifiable request evidence never authorises commerce; a plain
            # reply without a product is still sent, just without request effects.
            if action in COMMERCIAL_ACTIONS or key is not None:
                raise ActionBlocked('request_quote_required' if not quote else 'current_request_evidence_missing')
            request_kind='none'
    if request_kind!='none':
        if request_kind in {'direct','accept'} and key is not None:
            from .request_evidence import authorizes_request
            if (facts.get('no_push') is not False or facts.get('ordinary_chat_only') is not False
                    or facts.get('sales_refused') is not False):
                if not authorizes_request(snap,key,request_kind,quote):
                    raise ActionBlocked('current_request_authority_unverified')
            facts['direct_request_product']=key
            facts['ordinary_chat_only']=facts['sales_refused']=False
        if request_kind=='resend' and action in COMMERCIAL_ACTIONS:
            from .request_evidence import authorizes_request
            if not authorizes_request(snap,key,'resend',quote):
                raise ActionBlocked('current_request_authority_unverified')
            facts['requested_resend_product']=key
            # A specific request permits this one response without erasing the
            # fan's stored preference against unsolicited offers.
            facts['ordinary_chat_only']=facts['sales_refused']=False
    from .input_batch import cancelled,current_requests
    batch=pending_messages(snap)
    if action in COMMERCIAL_ACTIONS and any(cancelled(m) for m in batch):
        if not quote or not any(_norm(quote) in _norm(m['text']) for m in current_requests(batch)):
            raise ActionBlocked('purchase_request_cancelled')
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
            if snap['event']['payload'].get('direct_request_product')==key:
                from .request_evidence import authorizes_request
                if not authorizes_request(snap,key,'direct'):
                    raise ActionBlocked('current_request_authority_unverified')
                facts['direct_request_product']=key
            if snap['event']['payload'].get('requested_resend_product')==key: facts['requested_resend_product']=key
            purpose={'invite':'offer','continue':'continuation'}.get(action,action)
            if (action=='continue' and decision['sequence'] is None
                    and not any(key in steps for steps in pa.sequences.values())):
                # A follow-up sale from standalone inventory is an ordinary offer; it never needs a sequence.
                purpose='offer'
            if action=='offer' and snap.get('invitation'):
                if request_kind=='direct':
                    from .request_evidence import authorizes_request
                    if not authorizes_request(snap,key,'direct',quote):
                        raise ActionBlocked('current_request_authority_unverified')
                    pa.terms(key,facts,purpose=purpose,sequence=decision['sequence'])
                else:
                    if key!=snap['invitation']['product_id']:raise ActionBlocked('acceptance_product_mismatch')
                    from .request_evidence import current_invitation_acceptance
                    if not current_invitation_acceptance(snap,key,quote):raise ActionBlocked('invitation_stale_or_ambiguous')
                    pa.accepted_invitation(snap['invitation'],facts)
            else:pa.terms(key,facts,purpose=purpose,sequence=decision['sequence'])
            terms=snap['inventory'][key] if action!='invite' else None
    elif key is not None:raise ActionBlocked('unexpected_product_reference')
    from .price_assertions import price_problem
    monetary_problem=price_problem(snap,claim_decision,terms)
    if monetary_problem:raise ActionBlocked(monetary_problem)
    from .price_assertions import price_question_problem
    unanswered=price_question_problem(snap,claim_decision,terms)
    if unanswered:raise ActionBlocked(unanswered)
    if decision['resolve_obligation']:
        owed=next((o for o in snap['obligations'] if o['id']==decision['resolve_obligation'] and o.get('status')=='open'),None)
        if owed is None or (action not in {'correct','fulfil'} and not (action in {'reply','invite','offer','alternative'} and owed.get('kind')=='buying_question')):
            raise ActionBlocked('obligation_cannot_resolve_without_fulfilment')
    if terms and campaign and campaign['catalogue_item_id'] is not None:
        if (terms['item']['id']!=campaign['catalogue_item_id'] or campaign['terms'] is None
                or terms['price_cents']!=campaign['terms']['price_cents']
                or terms['media_ids']!=campaign['terms']['media_ids']):
            raise ActionBlocked('moxy_campaign_product_changed')
    clarification=(type(rejected) is dict and rejected.get('reason') in REQUEST_EVIDENCE_REASONS
        and action in {'reply','none','handover'} and key is None)
    if planning and not clarification and type(rejected) is dict and rejected.get('kind') in {'wording','factual'} and rejected.get('keep'):
        if any(decision.get(k)!=v for k,v in rejected['keep'].items()):
            raise ActionBlocked('correction_changed_valid_plan')
        item=snap['inventory'].get(decision.get('product_id'),{})
        if any(item.get(k)!=v for k,v in rejected.get('keep_terms',{}).items()):
            raise ActionBlocked('correction_product_terms_changed')
    from .grounding import explicit_sexting
    if explicit_sexting(snap,[p['text'] for p in _plan(decision)]):
        try:sexting_allowed=cfg.resolve('sexting',facts).value is True
        except ActionBlocked:sexting_allowed=False
        if not sexting_allowed:raise ActionBlocked('sexting_permission_unverified')
    # Wording is judged last: a product failure always takes precedence over a wording failure.
    problem=wording_problem(snap,claim_decision)
    if problem:raise ActionBlocked(problem)
    return terms


def _validate_draft(settings, snap, decision):
    """Validate, then remove only a repeated decorative face from a plain reply."""
    try:
        validate_decision(settings, snap, decision, planning=True)
    except ActionBlocked as exc:
        if (str(exc) != 'repeated_habit' or decision['action'] != 'reply'
                or decision['product_id'] is not None or decision.get('permission') is not None):
            raise
        from .conversation import pending_messages, quotes
        from .parts import repetition_texts
        # An emoji being quoted or asked about is part of the answer.
        if any(_policy_re.search(r"\b(?:emoji|emoticon|reaction|smirk|smiley)\b", m.get('text', ''), _policy_re.I)
               for m in pending_messages(snap)):
            raise
        token = repeated_habit(repetition_texts(snap, decision), _recent_outgoing_for_repetition(snap),
                               _latest_fan_text(snap))
        if token is None or len(token) != 1 or not 0x1F600 <= ord(token) <= 0x1F64F:
            raise
        standalone = _policy_re.compile(r"(?<!\S)" + _policy_re.escape(token) + r"(?:\ufe0f)?(?: +|(?=\s|$))")
        parts = []
        changed = False
        for part in _plan(decision):
            text = part['text']
            if quotes(text) or _policy_re.search(
                    r"\b(?:no|not|never|cannot|off limits|stay covered)\b|\b\w+n['\u2019]t\b", text, _policy_re.I):
                # Keep refusal or quoted wording with the regular correction path.
                raise
            trimmed, count = standalone.subn('', text)
            if not trimmed.strip():
                raise
            changed |= bool(count)
            parts.append({**part, 'text': trimmed.strip() if count else text})
        if not changed:
            raise
        repaired = {**decision, 'text': parts[0]['text'], 'messages': parts}
        validate_decision(settings, snap, repaired, planning=True)
        return repaired
    return decision


class MoxyService:
    def __init__(self,store):
        from onlyfans_runtime.text_delivery import TextAuthority
        self.store=store; self.engine=store.engine; self.authority=TextAuthority(store)

    def configure(self,tenant,account,*,expected_revision,mode,settings,actor,discard_obsolete_replies=False):
        if (type(expected_revision) is not int or expected_revision<0 or type(mode) is not str
            or mode not in {'off','preview','on'} or type(actor) is not str or not actor or len(actor)>128
                or type(discard_obsolete_replies) is not bool):raise Refused('invalid_moxy_configuration')
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
            if discard_obsolete_replies:
                # A new owner/configuration revision also retires unqueued reply
                # drafts and pre-cutover history waits. Otherwise the next worker
                # pass can revive an old input before the supervised fan speaks.
                # Uncertain/generating work, paid obligations and all receipts/costs
                # remain with their existing recovery owners.
                obsolete=s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,
                    MoxyEvent.kind.in_(('turn','welcome')),
                    (MoxyEvent.status.in_(('pending','ready')))|
                    ((MoxyEvent.status=='held')&(MoxyEvent.reason=='history_not_current')&
                     MoxyEvent.model_call_id.is_(None)))).all()
                for event in obsolete:
                    event.status='stale';event.reason='moxy_owner_changed';event.updated_at=utcnow()
            # Old engagement timers have no Moxy business authority. Preserve
            # their evidence, and detach them through the existing cancellation seam.
            from onlyfans_runtime.launch_engagement.service import invalidate
            invalidate(s,runtime,reason='moxy_owner_changed',actor=actor)
            if mode!='off':
                for event in s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,
                    MoxyEvent.status=='held',MoxyEvent.reason=='moxy_off',MoxyEvent.model_call_id.is_(None))):
                    event.status='pending';event.reason=None
            s.add(MoxyAudit(audit_id=str(uuid.uuid4()),account_id=account,actor=actor,
                before_json=canonical(before),after_json=canonical({'mode':mode,'revision':owner.revision,'settings':settings,
                    **({'discard_obsolete_replies':True} if discard_obsolete_replies else {})}),created_at=utcnow()))
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
            if reason=='history_not_current':
                # Keep the durable event resumable beyond the old five-minute cutoff.
                # Age-based backoff survives restarts without mutating its input payload.
                age=max(0,(utcnow()-event.created_at).total_seconds())
                source=json.loads(event.payload_json).get('source_message_id')
                latest=s.scalar(select(Message.message_id).where(Message.account_id==account,
                    Message.fan_id==event.fan_id,Message.direction=='in',Message.is_unsent.is_(False))
                    .order_by(Message.created_at.desc(),Message.message_id.desc()).limit(1))
                if source and latest!=source:
                    event.status='stale';event.reason='moxy_input_superseded';return event_id
                event.reason='history_not_current'
                event.not_before=utcnow()+timedelta(seconds=min(60,3*2**min(5,int(age//60))));event.updated_at=utcnow()
                diagnose(s,account,'history_wait',event=event,elapsed_seconds=int(age),retry_at=event.not_before)
                return event_id
            if reason:
                if reason in {'contact_blocked','case_identity','case_distress','safety_hold'}:
                    from db.onlyfans_case_models import OfOperatorCase
                    assigned=json.loads(owner.settings_json)['operational'].get('assigned_operator')
                    if assigned:
                        for case in s.scalars(select(OfOperatorCase).where(OfOperatorCase.account_id==account,
                            OfOperatorCase.fan_id==event.fan_id,OfOperatorCase.status.in_(('open','acknowledged')))):
                            case.owner=case.owner or assigned
                event.status='held';event.reason=reason;return event_id
            try:snap=snapshot(s,account,event.fan_id,event)
            except Refused as exc:_evidence_hold(s,account,event,exc);return event_id
            source=json.loads(event.payload_json).get('source_message_id')
            if source and event.kind in {'turn','welcome'}:
                incoming=[m for m in snap['history'] if m['direction']=='in']
                if not incoming or incoming[-1]['id']!=source:
                    event.status='stale';event.reason='moxy_input_superseded';return event_id
                location=next((i for i,m in enumerate(snap['history']) if m['id']==source),None)
                if location is None:
                    event.status='held';event.reason='moxy_input_context_missing';return event_id
                if ('reply_retirement_at' in snap['history_scope']
                        and incoming[-1]['at'] < snap['history_scope']['reply_retirement_at']):
                    event.status='stale';event.reason='moxy_input_retired';return event_id
                if any(m['direction']=='out' and m['sender'] in {'human','unattributed'} for m in snap['history'][location+1:]):
                    event.status='done';event.reason='external_reply_after_input';return event_id
            try:
                if len(canonical(snap).encode())>1048576:raise Refused('moxy_snapshot_budget_exceeded')
                prompts=writer.messages(snap)
                if len(canonical(prompts).encode())>131072:raise Refused('moxy_context_budget_exceeded')
            except Exception as exc:
                reviewed_rule_failure=(isinstance(exc,ActionBlocked) and _policy_re.fullmatch(
                    r'writer_rule_review_required:[A-Za-z0-9_:.-]{1,80}',str(exc)))
                event.status='held';event.reason=(str(exc) if isinstance(exc,Refused) or reviewed_rule_failure else 'moxy_prompt_build_failed')[:128]
                diagnose(s,account,'context_failure',event=event,error_type=type(exc).__name__)
                self._technical_attention(s,owner,event)
                return event_id
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
            call_id=call.id;reserved=units
        result=reason=None
        async def generate():
            async with asyncio.timeout(timeout):return await model.generate(prompts)
        # Keep structurally valid input observations even when the proposed
        # sale is refused. Authority still gates preparation and every send.
        try:
            raw=asyncio.run(generate())
            # Commit evidence BEFORE parsing: malformed output and a worker crash
            # after generation must both be diagnosable and recoverable.
            with Session(self.engine) as s,s.begin():
                saved=s.get(MoxyEvent,(account,event_id))
                diagnose(s,account,'provider_result',event=saved,raw=raw,
                    actual_millicents=model.reported_cost_millicents if model.completed else None,
                    tokens_in=getattr(model,'tokens_in',None),tokens_out=getattr(model,'tokens_out',None),
                    cache_hit_tokens=getattr(model,'cache_hit_tokens',None),
                    configuration_fingerprint=snap.get('configuration_fingerprint'))
            result=writer.parse(raw,defer_sales_pause=True,require_progress=True)
        except (TimeoutError,asyncio.TimeoutError):reason='moxy_model_unknown'
        except Exception as exc:reason=str(exc) if isinstance(exc,ActionBlocked) else 'moxy_draft_refused'
        with Session(self.engine) as s,s.begin():
            self.store._runtime(s,tenant,account);owner=s.get(MoxyAccount,account);event=s.get(MoxyEvent,(account,event_id))
            from llm_client import settle_scoped_synthetic_call
            settle=model.settle if live else settle_scoped_synthetic_call
            cost=settle(s,account_id=account,call_id=call_id,reserved_millicents=reserved,
                actual_millicents=model.reported_cost_millicents if model.completed else None,now=utcnow())
            # Recovery may already have fenced this invocation. Settle its cost
            # evidence, but a late result cannot revive or deliver the old event.
            if event.status!='generating' or event.model_call_id!=call_id:
                diagnose(s,account,'late_generation_fenced',event=event,completed_call_id=call_id)
                return event_id
            event.charged_millicents=cost
            if cost is None:reason='moxy_model_usage_unknown'
            if event.snapshot_hash!=digest(snapshot(s,account,event.fan_id,event)):
                event.status='stale';event.reason='moxy_snapshot_changed';return event_id
            if reason:
                self._generation_failure(s,owner,event,snap,reason)
                return event_id
            self._finish_decision(s,owner,event,snap,result)
        return event_id

    def _generation_failure(self,s,owner,event,snap,reason):
        """Same disposition before/after restart; never retry uncertain usage."""
        event.status='unknown' if reason.endswith('unknown') else 'held'
        event.reason=reason[:128];event.lease_until=None
        payload=json.loads(event.payload_json)
        if event.status=='held' and owner.mode=='on':
            if payload.get('correction_of'):
                self._correction_exhausted(s,owner,event,reason)
            elif (event.kind in {'turn','welcome'} and payload.get('source_message_id')
                    and reason in FORMAT_REASONS):
                enqueue(s,event.account_id,event.fan_id,event.kind,'correction:'+event.event_id,
                    {'source_message_id':payload['source_message_id'],'correction_of':event.event_id,
                     'rejected':{'reason':reason[:120],'kind':'format','action':None,'product_id':None,
                                 'fan_message':_latest_fan_text(snap)}})
            else:
                self._technical_attention(s,owner,event)
        diagnose(s,event.account_id,'generation_outcome',event=event)

    def _finish_decision(self,s,owner,event,snap,result):
        account,event_id=event.account_id,event.event_id
        from .pilot import route_specialist
        result=route_specialist(json.loads(owner.settings_json),result)
        try:
            # Validate evidence before retaining a fan constraint, including no-send turns.
            from .observations import observations
            observations(result,snap)
            if owner.mode!='preview':observe_input(s,event,result)
            result = _validate_draft(json.loads(owner.settings_json), snap, result)
        except (ActionBlocked,InvalidConfiguration) as exc:
            event.status='held';event.reason=str(exc);event.decision_json=canonical(result)
            payload=json.loads(event.payload_json)
            if payload.get('correction_of'):
                if owner.mode=='on':
                    self._correction_exhausted(s,owner,event,str(exc))
            elif (isinstance(exc,ActionBlocked) and owner.mode=='on' and event.kind in {'turn','welcome'} and payload.get('source_message_id')
                    and recoverable(str(exc)) and (result['action'] in COMMERCIAL_ACTIONS or result['product_id'] is not None
                        or str(exc) in WORDING_REASONS|FACTUAL_REASONS or str(exc)=='product_invitation_missing_identity')):
                # One bounded corrective planning attempt through the normal worker path (model reservation,
                # spending limits, stale-input and eligibility checks all apply again). The rejected choice stays held.
                enqueue(s,account,event.fan_id,event.kind,'correction:'+event_id,
                    {'source_message_id':payload['source_message_id'],'correction_of':event_id,
                     'rejected':correction_brief(str(exc),result,snap)})
            diagnose(s,account,'validation_outcome',event=event,validation_reason=str(exc))
            return event_id
        event.decision_json=canonical(result)
        if owner.mode=='preview':event.status='preview';return event_id
        # Our own newly recorded input constraints are part of the frozen
        # decision. Future independent input still invalidates this version.
        snap=snapshot(s,account,event.fan_id,event)
        event.snapshot_json=canonical(snap);event.snapshot_hash=digest(snap)
        if result['action']=='handover':
            from .grounding import unverified_card_question
            if (not set(result['flags']) & {'identity','danger'} and
                    (result.get('reason','').partition(':')[0]=='creator_fact_unavailable'
                     or (result.get('reason','').partition(':')[0]=='card_context_unavailable' and unverified_card_question(snap)))):
                event.reason=result.get('reason','').partition(':')[0]
                self._technical_attention(s,owner,event)
                event.status='done'
                diagnose(s,account,'human_review_required',event=event)
                return event_id
            from onlyfans_runtime.operator_cases import open_case
            category=('identity' if 'identity' in result['flags'] else 'distress' if 'danger' in result['flags']
                else 'complaint' if 'complaint' in result['flags'] else 'other')
            open_case(s,account,event.fan_id,category,reason='moxy_owned_handover',evidence={'event_id':event_id},origin='detector',scope='all',owner=json.loads(owner.settings_json)['operational'].get('assigned_operator'))
            event.status='done';return event_id
        if result['action']=='none':event.status='done';return event_id
        event.status='ready';event.reason=None;event.lease_until=None
        diagnose(s,account,'decision_ready',event=event)

    def _invalidated(self,s,account,e):
        """Stale on a changed snapshot; a refused snapshot holds only this event (G01-02)."""
        try:
            if digest(snapshot(s,account,e.fan_id,e))==e.snapshot_hash:return False
            e.status='stale';e.reason='moxy_snapshot_changed'
        except Refused as exc:_evidence_hold(s,account,e,exc)
        return True

    @staticmethod
    def _technical_attention(s,owner,event):
        from onlyfans_runtime.operator_cases import open_case
        payload=json.loads(event.payload_json)
        first=s.get(MoxyEvent,(event.account_id,payload['correction_of'])) if payload.get('correction_of') else None
        case,created=open_case(s,event.account_id,event.fan_id,'moxy_technical',reason='moxy_technical_attention',
            evidence={'event_id':event.event_id,'model_call_id':event.model_call_id,'reason':event.reason,
                'correction_of':payload.get('correction_of'),'rejected_reason':first.reason if first is not None else None,
                'original_rejection':first.reason if first is not None else event.reason,
                'correction_rejection':event.reason if first is not None else None,
                'recovery_rejection':None,'recovery_attempted':False},
            origin='detector',scope='offer',
            owner=json.loads(owner.settings_json)['operational'].get('assigned_operator'))
        detail=json.loads(case.evidence_json)
        failures=detail.setdefault('recovery_failures',[])
        if not any(f['event_id']==event.event_id for f in failures):
            failures.append({'event_id':event.event_id,'source_message_id':payload.get('source_message_id'),
                'original_rejection':first.reason if first is not None else None,'correction_rejection':event.reason})
        detail['recovery_failures']=failures[-8:];case.evidence_json=canonical(detail)[:4000]
        case.updated_at=utcnow();case.owner=case.owner or json.loads(owner.settings_json)['operational'].get('assigned_operator')
        payload['recovery_outcome']={'status':'human_review_required','case_id':case.case_id,'assigned_operator':case.owner}
        event.payload_json=canonical(payload)
        return case.case_id

    def _correction_exhausted(self,s,owner,event,reason):
        # Exhaustion is internal only: retain the rejected decision and charge,
        # open a scoped operator case, and never substitute customer-facing text.
        event.status='held';event.reason=reason[:128];event.lease_until=None
        self._technical_attention(s,owner,event)
        payload=json.loads(event.payload_json)
        first=s.get(MoxyEvent,(event.account_id,payload['correction_of'])) if payload.get('correction_of') else None
        diagnose(s,event.account_id,'correction_exhausted',event=event,
            original_rejection=first.reason if first is not None else None,
            correction_rejection=reason,recovery_rejection=None,recovery_attempted=False)

    def _recover_generations(self,s,account):
        """Fence expired calls, reuse persisted completions, otherwise surface an operator outcome.

        Unknown accounting keeps its reservation. There is no blind regeneration,
        and delivery uncertainty continues through the existing intent reconciler.
        The caller holds the existing account lock, serialising competing workers.
        """
        from .diagnostics import completion
        owner=s.get(MoxyAccount,account)
        rows=s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,
            (MoxyEvent.status=='unknown')|
            ((MoxyEvent.status=='generating')&(MoxyEvent.lease_until<utcnow()))|
            ((MoxyEvent.status=='held')&(MoxyEvent.reason=='moxy_generation_reconciliation_required')))
            .order_by(case((MoxyEvent.status=='held',1),else_=0),MoxyEvent.updated_at).limit(32)).all()
        for event in rows:
            event.lease_until=None
            if event.intent_id:
                # An existing intent, especially after one accepted bubble, is
                # never a licence to generate/send a replacement.
                event.status='held';event.reason='moxy_delivery_reconciliation_required'
                self._technical_attention(s,owner,event)
                diagnose(s,account,'delivery_reconciliation_required',event=event)
                continue
            call=s.scalar(select(GrokCall).where(GrokCall.id==event.model_call_id).with_for_update())
            proof=completion(s,event)
            if call is not None and proof and proof.get('raw_complete'):
                units=proof.get('actual_millicents')
                # Reconcile ONLY the immutable completion's recorded cost against
                # the original reservation/day/provider, without replaying the call.
                if (call.status in {'pending','unknown'} and type(units) is int and 0<=units<=100000000
                        and call.account_id==account and call.fan_id==event.fan_id
                        and call.cost_cents==event.reserved_millicents):
                    ledger=s.scalar(select(GrokDailyCost).where(GrokDailyCost.account_id==account,
                        GrokDailyCost.provider==call.provider,GrokDailyCost.day==call.called_at.strftime('%Y-%m-%d'))
                        .with_for_update())
                    if ledger is not None and ledger.cost_cents>=event.reserved_millicents:
                        ledger.cost_cents+=units-event.reserved_millicents;ledger.updated_at=utcnow()
                        call.cost_cents=units;call.status='done'
                        for field in ('tokens_in','tokens_out','cache_hit_tokens'):
                            value=proof.get(field)
                            if type(value) is int and value>=0:setattr(call,field,value)
                if call.status=='done' and call.account_id==account and call.fan_id==event.fan_id:
                    event.charged_millicents=call.cost_cents
                    if proof.get('status')!='generating':
                        # A result persisted after recovery fenced the invocation
                        # can settle accounting, but cannot acquire send authority.
                        event.status='held';event.reason='moxy_late_generation_fenced'
                        diagnose(s,account,'late_completion_cost_reconciled',event=event)
                        continue
                    try:fresh=snapshot(s,account,event.fan_id,event)
                    except Refused as exc:_evidence_hold(s,account,event,exc);continue
                    if event.snapshot_hash!=digest(fresh):
                        event.status='stale';event.reason='moxy_snapshot_changed'
                    else:
                        try:result=writer.parse(proof['raw'],defer_sales_pause=True,require_progress=True)
                        except ActionBlocked as exc:
                            self._generation_failure(s,owner,event,fresh,str(exc))
                        else:self._finish_decision(s,owner,event,fresh,result)
                    diagnose(s,account,'generation_recovered',event=event)
                    continue
            # Unknown usage is not released or charged twice to clear the event.
            if call is not None and call.status=='pending':call.status='unknown'
            first=event.reason!='moxy_generation_reconciliation_required'
            event.status='held';event.reason='moxy_generation_reconciliation_required'
            event.updated_at=utcnow()
            if first:
                self._technical_attention(s,owner,event)
                diagnose(s,account,'generation_reconciliation_required',event=event,
                    reservation_retained=event.reserved_millicents)

    def queue(self,tenant,account,event_id):
        with Session(self.engine) as s,s.begin():
            self.store._runtime(s,tenant,account);event=s.get(MoxyEvent,(account,event_id))
            if event.status=='queued':return event.intent_id
            if event.status!='ready':raise Refused('moxy_decision_not_ready')
            from .parts import active, index
            d=active(event);snap=json.loads(event.snapshot_json)
            terms=validate_decision(json.loads(s.get(MoxyAccount,account).settings_json),snap,
                json.loads(event.decision_json),part_index=index(event))
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
            # Upgrade already-stranded history waits from the old five-minute
            # policy without touching intentional holds or uncertain delivery.
            for e in s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,
                    MoxyEvent.status=='held',MoxyEvent.reason=='history_not_current',
                    MoxyEvent.model_call_id.is_(None))):
                e.status='pending';e.not_before=utcnow()
            self._recover_generations(s,account)
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
                        elif not self._invalidated(s,account,e):
                            e.status='ready';e.reason='provider_rate_limit_retry'
                    else:
                        e.status='stale';e.reason=intent.reason
                elif intent and intent.status in {'cancelled','refused'}:
                    e.status='stale';e.reason=intent.reason
            for e in s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account,MoxyEvent.status=='ready')):
                self._invalidated(s,account,e)
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
            try:
                self.prepare(tenant,account,identity,invocation)
                self.queue(tenant,account,identity)
                return 1
            except Refused:
                return 0
            except (ActionBlocked,InvalidConfiguration) as exc:
                # A deterministic preparation rejection must leave the ready
                # queue. Never alter an admitted/unknown intent in this handler.
                with Session(self.engine) as s,s.begin():
                    self.store._runtime(s,tenant,account)
                    e=s.get(MoxyEvent,(account,identity))
                    if e.status in {'pending','ready'}:
                        e.status='held';e.reason=str(exc)[:128];e.lease_until=None
                        self._technical_attention(s,s.get(MoxyAccount,account),e)
                        diagnose(s,account,'preparation_refused',event=e)
                return 0
            finally:
                self._retire_finished(tenant,account,identity)

        with ThreadPoolExecutor(max_workers=min(4,len(ids) or 1)) as pool:
            return sum(pool.map(prepare_one,ids))

    def _retire_finished(self,tenant,account,identity):
        with Session(self.engine) as s,s.begin():
            self.store._runtime(s,tenant,account)
            e=s.get(MoxyEvent,(account,identity))
            if e.status in {'held','stale','done'}:
                from .campaigns import retire
                retire(s,e)


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


def _evidence_hold(s,account,event,exc):
    """Refused snapshot (unreadable ownership evidence): hold this fan's event; other fans proceed.
    Surfaced like a prompt-build failure (diagnostic + the fan's moxy_technical operator case)."""
    event.status='held';event.reason=str(exc)[:128]
    diagnose(s,account,'context_failure',event=event,error_type=type(exc).__name__)
    owner=s.get(MoxyAccount,account)
    if owner is not None:MoxyService._technical_attention(s,owner,event)


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
    try:snap=snapshot(s,intent.account_id,intent.fan_id,event)
    except Refused:return False  # unreadable evidence: not current (intent is cancelled; FLG3-01 follow-up)
    return digest(snap)==event.snapshot_hash and decision['text']==intent.body


def delivered(s,intent):
    if not intent.moxy_event_id:return
    e=s.get(MoxyEvent,(intent.account_id,intent.moxy_event_id))
    from onlyfans_runtime.send_recovery import RETIRED_UNCONFIRMED_TEXT
    if e.status=='done' or e.reason==RETIRED_UNCONFIRMED_TEXT:return
    from .parts import active, has_remaining, continuation_unchanged, plan, index
    d=active(e)
    continuation_ok=True;refused=None
    # A completed single-message reply needs no continuation snapshot. Keep the
    # extra consistency read only for a tail we may actually dispatch.
    if index(e)+1<len(plan(json.loads(e.decision_json))):
        before=json.loads(e.snapshot_json)
        try:
            after=snapshot(s,intent.account_id,intent.fan_id,e,ignore_outgoing_ids=(intent.provider_message_id,))
            continuation_ok=continuation_unchanged(before,after,intent.provider_message_id,d,account_id=intent.account_id,session=s)
        except Refused as exc:continuation_ok=False;refused=exc  # the landed receipt is still recorded below
    payload=json.loads(e.payload_json)
    progress=payload.setdefault('_moxy_delivery',{'receipts':[]})
    progress['receipts'].append({'intent_id':intent.intent_id,'message_id':intent.provider_message_id})
    e.payload_json=canonical(payload)
    mf=_fan(s,intent.account_id,intent.fan_id);state=json.loads(mf.state_json)
    if d['action']=='invite':
        cfg,pa=configuration(json.loads(s.get(MoxyAccount,intent.account_id).settings_json))
        state['invitation']={'product_id':d['product_id'],'fingerprint':pa.fingerprint,'sequence':d['sequence'],
            'message_id':intent.provider_message_id}
    elif d['action'] in {'offer','continue','resend','alternative'}:
        state.pop('invitation',None)
        # An explicitly requested item is a scoped exception, not permission
        # to pitch later items. Only verified reversal observations change the
        # durable preference; receiving this card does not do so.
    if d['question']:
        obligations=state.setdefault('obligations',[])
        if not any(o.get('question')==d['question'] and o['status']=='open' for o in obligations):
            obligations.append({'id':e.event_id,'kind':'buying_question','question':d['question'],'status':'open','owner':'moxy','source_event':e.event_id})
    if d['resolve_obligation']:
        for o in state.get('obligations',[]):
            if o['id']==d['resolve_obligation']:o['status']='fulfilled';o['message_id']=intent.provider_message_id
    mf.state_json=canonical(state);mf.revision+=1;mf.updated_at=utcnow();e.updated_at=utcnow()
    e.status='done'
    remaining=has_remaining(e)
    if not remaining:
        original=json.loads(e.decision_json)
        if 'unanswered_input_ids' in original:
            payload['_moxy_conversation_progress']={'version':1,'unanswered_input_ids':original['unanswered_input_ids']}
            e.payload_json=canonical(payload)
    if remaining:
        if refused is not None:e.intent_id=None;_evidence_hold(s,intent.account_id,e,refused)
        elif not continuation_ok:
            e.status='stale';e.reason='moxy_conversation_interrupted'
        else:
            e.status='ready';e.intent_id=None
            try:snap=snapshot(s,intent.account_id,intent.fan_id,e)
            except Refused as exc:_evidence_hold(s,intent.account_id,e,exc)
            else:e.snapshot_json=canonical(snap);e.snapshot_hash=digest(snap)

