from test_onlyfans_bulk_controls_pg import *
def test_initial_default_attention_is_operator_activatable(database):
    store=seed(*database)
    add_fans(database[1],[(301,'paused',0,'none',True,False),(302,'paused',1,'none',True,False)])
    result=apply(store,'resume_automation',items([301,302],0),'d'*64)
    assert result[0]['outcome']=='recorded'
    assert result[1]['reason']=='revision_changed'
    with Session(database[1]) as s:
        state=s.get(OfConversationState,(ACCOUNT,301))
        assert state.mode=='automated' and state.requires_attention
    assert apply(store,'resume_automation',items([302],1),'e'*64)[0]['reason']=='human_review'
    store.close()
