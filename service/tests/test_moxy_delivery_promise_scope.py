"""'I can send' is a possibility; 'I will send' / 'sending' is a delivery promise needing an action."""
import pytest
from moxy_replacement.grounding import commitment_problem
SNAP={'card_state':[],'history':[],'event':{'payload':{}},'pending_fan_messages':[]}
REPLY={'action':'reply','product_id':None}

@pytest.mark.parametrize('text',['ha nice try, i can send you something though','i could send you something later if you like'])
def test_capability_is_not_a_delivery_promise(text):
    assert commitment_problem(SNAP,REPLY,[text]) is None

@pytest.mark.parametrize('text',["ill send you a pic now","i will send it over","i'm gonna send you something","sending it now"])
def test_commitments_still_need_a_media_action(text):
    assert commitment_problem(SNAP,REPLY,[text]) == 'delivery_promise_without_action'

def test_media_action_may_carry_the_promise():
    assert commitment_problem(SNAP,{'action':'offer','product_id':'p1'},["ill send you a pic now"]) is None
