"""Explicit commercial state for the writer, derived only from the snapshot.

sent_unpaid_cards means: sent, locked, not purchased, not proof he viewed it.
Pure and deterministic: no database access. Every field is either evidenced by
the snapshot or null/empty; nothing is inferred. A sent paid card that is not
purchased is locked for the fan and is not proof he viewed its content.
"""

RESTRICTIONS=('ordinary_chat_only','sales_refused','boundary_hold','financial_hardship',
              'no_push','human_owned','safety_hold')


def _card(m):
    return {'message_id':m['id'],'product_id':m.get('product_id'),'price_cents':m.get('price_cents'),
            'sent_at':m.get('at'),'paid':m.get('paid')}


def commercial_state(snap):
    history=snap.get('history') or []
    fan_state=snap.get('fan_state') or {}
    incoming=[m for m in history if m.get('direction')=='in']
    current=incoming[-1] if incoming else None
    cards=[m for m in history if m.get('direction')=='out' and type(m.get('price_cents')) is int and m['price_cents']>0]
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
    if fan_state.get('rejected_products'):
        result['rejected_products']=list(fan_state['rejected_products'])
    # Compact: absent means none/unknown, never guessed.
    return {k:v for k,v in result.items() if v not in (None,[],{})}
