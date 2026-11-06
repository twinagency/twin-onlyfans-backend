"""Explicit commercial state for the writer, derived only from the snapshot.

sent_unpaid_cards means: sent, locked, not purchased, not proof he viewed it.
Pure and deterministic: no database access. Every field is either evidenced by
the snapshot or null/empty; nothing is inferred. A sent paid card that is not
purchased is locked for the fan and is not proof he viewed its content.
"""

RESTRICTIONS=('ordinary_chat_only','sales_refused','boundary_hold','financial_hardship',
              'no_push','human_owned','safety_hold')


import re
CARD_REFERENCE=re.compile(r'\b(?:card|message)\s*(?:number\s*|#\s*)?(\d+)\b',re.I)
_ORDINAL=re.compile(r'\b(first|second|third|fourth|fifth|last|latest)\s+(?:one|card|message)\b',re.I)


def referenced_card(snapshot,text,*,allow_current=True):
    """Resolve an explicit discussion reference, never select a new product.

    Price and content claims share this owner. Ambiguous or unknown identities
    stay unresolved; a selected new item must not substitute for an old card.
    """
    cards=sorted((c for c in snapshot.get('card_state',[]) if type(c.get('id')) is int),
                 key=lambda c:(str(c.get('at') or ''),c['id']))
    by_id={str(c['id']):c for c in cards}
    def explicit(value):
        ids=CARD_REFERENCE.findall(value)
        if ids:return True,by_id.get(ids[0]) if len(set(ids))==1 else None
        ordinals=_ORDINAL.findall(value)
        if ordinals:
            if len(set(o.lower() for o in ordinals))!=1:return True,None
            number={'first':0,'second':1,'third':2,'fourth':3,'fifth':4,'last':-1,'latest':-1}[ordinals[0].lower()]
            return True,cards[number] if cards and number<len(cards) else None
        return False,None
    present,card=explicit(text)
    if present or not allow_current:return card
    from .conversation import pending_messages
    incoming=pending_messages(snapshot)
    latest=incoming[-1].get('text','') if incoming else ''
    present,card=explicit(latest)
    if present:return card
    if len(cards)==1 and re.search(r'\b(?:card|that|it|this)\b',latest,re.I):return cards[0]
    return None


def _card(m):
    cents=m.get('price_cents')
    return {'card_kind':'priced_card','price_display':f'${cents//100}.{cents%100:02d}' if type(cents) is int else None,
            'message_id':m['id'],'product_id':m.get('product_id'),'price_cents':m.get('price_cents'),
            'sent_at':m.get('at'),'paid':m.get('paid')}


def commercial_state(snap):
    history=snap.get('history') or []
    fan_state=snap.get('fan_state') or {}
    incoming=[m for m in history if m.get('direction')=='in']
    current=incoming[-1] if incoming else None
    # A supplied durable projection is authoritative, including an empty one.
    # The rolling window cannot erase an old card or overwrite its payment state.
    source=snap['card_state'] if 'card_state' in snap else history
    by_id={}
    for m in source or []:
        if (m.get('direction','out')!='out' or type(m.get('id')) is not int
                or type(m.get('price_cents')) is not int or m['price_cents']<=0):continue
        by_id[m['id']]=m
    cards=sorted(by_id.values(),key=lambda m:(str(m.get('at') or ''),m['id']))
    previews=[m['id'] for m in history if m.get('direction')=='out' and m.get('media_count')
              and not (type(m.get('price_cents')) is int and m['price_cents']>0)]
    invitation=snap.get('invitation')
    evidence=fan_state.get('constraint_evidence') or {}
    restrictions=[]
    for flag in RESTRICTIONS:
        if fan_state.get(flag) is True:
            item={'flag':flag}
            ev=evidence.get(flag)
            if isinstance(ev,dict):
                item['evidence']={k:ev[k] for k in ('message_id','quote') if k in ev}
            restrictions.append(item)
    obligations=[]
    for o in snap.get('obligations') or []:
        if isinstance(o,dict) and o.get('status')=='open':
            item={'id':o.get('id'),'kind':o.get('kind')}
            for k in ('question','product_id'):
                if o.get(k) is not None: item[k]=o[k]
            obligations.append(item)
    result={
        'current_input':{'id':current['id'],'text':current.get('text')} if current else None,
        'last_invitation':({'product_id':invitation.get('product_id'),'sequence':invitation.get('sequence')}
                           if isinstance(invitation,dict) else None),
        'last_offer':_card(cards[-1]) if cards else None,
        'sent_unpaid_cards':[{k:v for k,v in _card(m).items() if k!='paid'} for m in cards if m.get('paid') is False],
        'verified_purchases':list(fan_state.get('purchased_products') or []),
        'owned_assets':list(fan_state.get('owned_assets') or []),
        'free_previews_sent':previews,
        'open_obligations':obligations,
        'active_restrictions':restrictions,
    }
    sent=set(fan_state.get('previously_sent_assets') or [])
    by_product=fan_state.get('paid_assets_by_product') or {}
    already=sorted(k for k,assets in by_product.items() if sent and sent & set(assets or []))
    if already:
        # Paid media of these products was already sent to this fan (card or exposure): not to be offered again unless he asks for a resend.
        result['previously_sent_products']=already
    eligibility=snap.get('offer_eligibility') or {}
    products=snap.get('products') or {}
    offerable=[k for k,v in eligibility.items() if v=='ok' and k in products]
    if eligibility:
        cheapest={}
        for k in offerable:
            p=products[k];cat=p.get('category')
            if type(p.get('price_cents')) is int and (cat not in cheapest or p['price_cents']<cheapest[cat]):cheapest[cat]=p['price_cents']
        if cheapest:
            # Lowest price among products he can actually be offered now, per category; not an account-wide minimum.
            result['cheapest_offerable_cents_by_category']=cheapest
        owned=sorted(k for k,v in eligibility.items() if v=='already_owned_use_existing_access')
        if owned:result['owned_products']=owned
    if type(fan_state.get('budget_cents')) is int:
        result['stated_budget_cents']=fan_state['budget_cents']
    if fan_state.get('rejected_products'):
        result['rejected_products']=list(fan_state['rejected_products'])
    # Compact: absent means none/unknown, never guessed.
    return {k:v for k,v in result.items() if v not in (None,[],{})}
