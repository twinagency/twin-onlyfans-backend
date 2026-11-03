import json
from moxy_replacement.state import commercial_state


def snap():
    return {
        'history':[
            {'id':1,'direction':'in','text':'hi','at':'t1','media_count':0,'price_cents':0},
            {'id':2,'direction':'out','text':'look','at':'t2','media_count':1,'price_cents':0},
            {'id':3,'direction':'out','text':'locked one','at':'t3','media_count':1,'price_cents':7500,'paid':False,'product_id':'first'},
            {'id':4,'direction':'out','text':'bought one','at':'t4','media_count':1,'price_cents':5000,'paid':True,'product_id':'second'},
            {'id':5,'direction':'out','text':'unknown','at':'t5','media_count':1,'price_cents':2500,'paid':None},
            {'id':6,'direction':'in','text':'what is in the first one?','at':'t6','media_count':0,'price_cents':0},
        ],
        'fan_state':{'purchased_products':['second'],'owned_assets':[7102],'ordinary_chat_only':True,'no_push':False,
            'human_owned':False,'constraint_evidence':{'ordinary_chat_only':{'message_id':1,'quote':'just chat','kind':'x'}}},
        'invitation':{'product_id':'first','fingerprint':'abc','sequence':'vault'},
        'obligations':[{'id':'e1','kind':'buying_question','question':'how much?','status':'open'},
                       {'id':'e2','kind':'custom','status':'done'}],
    }


def test_commercial_state_distinguishes_unpaid_paid_and_free():
    cs=commercial_state(snap())
    assert cs['current_input']=={'id':6,'text':'what is in the first one?'}
    assert cs['last_invitation']=={'product_id':'first','sequence':'vault'}
    assert cs['last_offer']=={'message_id':5,'product_id':None,'price_cents':2500,'sent_at':'t5','paid':None}
    assert cs['sent_unpaid_cards']==[{'message_id':3,'product_id':'first','price_cents':7500,'sent_at':'t3'}]
    assert cs['verified_purchases']==['second'] and cs['owned_assets']==[7102]
    assert cs['free_previews_sent']==[2]
    assert cs['open_obligations']==[{'id':'e1','kind':'buying_question','question':'how much?'}]
    assert cs['active_restrictions']==[{'flag':'ordinary_chat_only','evidence':{'message_id':1,'quote':'just chat'}}]
    assert json.loads(json.dumps(cs))==cs and commercial_state(snap())==cs


def test_paid_last_offer_and_empty_snapshot():
    s=snap();s['history']=s['history'][:4]
    cs=commercial_state(s)
    assert cs['last_offer']['paid'] is True and cs['sent_unpaid_cards'][0]['message_id']==3
    empty=commercial_state({})
    assert empty=={}
