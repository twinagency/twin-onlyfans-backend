"""Validated outputs from actual-model rejected drafts, with negative controls."""
import json
import pytest
from moxy_replacement.configuration import ActionBlocked
from moxy_replacement.runtime import validate_decision
from moxy_replacement.writer import parse
from test_moxy_integrated_pg import decision
from test_moxy_foundation_authority_regressions import authority_scene

@pytest.mark.parametrize('text',[
 "You didn't, that was me. I said I'd pass out if I stayed awake all night.",
 "You didn't, my mistake. I wrote I would pass out if I stayed awake all night.",
])
def test_unquoted_own_words_are_evidence_not_conversational_looping(text):
    settings,snap,_=authority_scene('When did I say that?')
    snap['history']=[{'id':70,'direction':'out','text':'I would pass out if I stayed awake all night.'},
                     {'id':71,'direction':'in','text':'You should get some rest then.'},
                     {'id':72,'direction':'out','text':'You said you would pass out on me.'},*snap['history']]
    assert validate_decision(settings,snap,parse(json.dumps(decision(text=text))),planning=True) is None


def test_correct_quotation_does_not_exempt_an_unrelated_repeated_pitch():
    settings,snap,_=authority_scene('When did I say that?')
    snap['history']=[{'id':70,'direction':'out','text':'I would pass out if I stayed awake all night.'},
        {'id':72,'direction':'out','text':'Come closer and let me make your evening special.'},*snap['history']]
    text="I said I'd pass out if I stayed awake all night. Come closer and let me make your evening special."
    with pytest.raises(ActionBlocked,match='repeated_recent'):
        validate_decision(settings,snap,parse(json.dumps(decision(text=text))),planning=True)

@pytest.mark.parametrize('other_price,allowed',[(7500,True),(8500,False)])
def test_same_item_resends_preserve_price_discussion_but_conflicting_terms_do_not(other_price,allowed):
    settings,snap,_=authority_scene('Can you make it cheaper?')
    snap['card_state']=[{'id':77,'product_id':'first','price_cents':7500,'paid':False},
                        {'id':78,'product_id':'first','price_cents':other_price,'paid':False}]
    text="I can't drop the price on that one, it's set at $75.00. If that doesn't work right now, we can keep talking."
    d=parse(json.dumps(decision(text=text)))
    if allowed:assert validate_decision(settings,snap,d,planning=True) is None
    else:
        with pytest.raises(ActionBlocked,match='price_claim_unbound'):
            validate_decision(settings,snap,d,planning=True)
