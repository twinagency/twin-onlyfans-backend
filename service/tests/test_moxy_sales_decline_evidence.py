"""A fan declining to buy is evidence for a retained sales refusal; unrelated disinterest is not."""
import pytest
from moxy_replacement.request_evidence import supports_preference

OBS={'kind':'sales_refused','active':True,'message_id':1,'quote':'x','product_id':None}


@pytest.mark.parametrize('text',[
    'I am really not interested in buying anything',
    "i'm not interested in buying",
    "I don't want to buy anything",
    'no more offers please',
    'not interested in any offers',
])
def test_decline_to_buy_supports_a_sales_refusal(text):
    assert supports_preference(OBS,{'id':1,'text':text},{}) is True


@pytest.mark.parametrize('text',[
    'I am not interested in football',
    'ok what is new',
    'if I was rich I would not be interested in buying that',
    'my friend said he is not interested in buying anything',
])
def test_unrelated_or_reported_or_conditional_text_does_not(text):
    assert supports_preference(OBS,{'id':1,'text':text},{}) is False
