import pytest
from moxy_replacement.configuration import ActionBlocked
from moxy_replacement.input_batch import explicit_updates
from moxy_replacement.observations import apply, observations
from moxy_replacement.runtime import validate_decision
from test_moxy_foundation_authority_regressions import authority_scene, parsed_offer
from test_moxy_integrated_pg import decision


@pytest.mark.parametrize('text',[
    'Only if it is free, send me the first photo.',
    'Tomorrow, send me the first photo.',
    'When I subscribe, send me the first photo.',
    'My friend says, send me the first photo.',
    'Unless I refuse, send me the first photo.',
])
def test_prefix_scope_cannot_disappear_at_comma(text):
    settings,snap,_=authority_scene(text,no_push=True)
    with pytest.raises(ActionBlocked):
        validate_decision(settings,snap,parsed_offer(request=text),planning=True)


@pytest.mark.parametrize('text',[
    'Please send me the first photo.',
    'Okay, please send me the first photo.',
    'Hello, send me the first photo.',
    'I would like to see the first photo.',
    'Send me the first photo too.',
])
def test_plain_affirmative_requests_remain_usable_under_existing_no_push_exception(text):
    settings,snap,_=authority_scene(text,no_push=True)
    assert validate_decision(settings,snap,parsed_offer(request=text),planning=True)['media_ids']==[101]


@pytest.mark.parametrize('text',[
    'If I understand the boundary, can you send me offers?',
    'My friend says I understand the boundary.',
    'Tomorrow, I understand the boundary.',
])
def test_conditional_reported_boundary_resolution_is_not_permission(text):
    _,snap,_=authority_scene(text,boundary_hold=True)
    value=dict(kind='boundary_hold',active=False,message_id=1001,quote=text,product_id=None)
    assert apply(snap['fan_state'],observations(decision(observations=[value]),snap))['boundary_hold'] is True


@pytest.mark.parametrize('text',[
    'I never said my budget is $500.',
    'If my budget is $500, what would you offer?',
    'If I get paid, my budget is $500.',
    'Tomorrow, my budget is $500.',
    'My friend says, my budget is $500.',
    'You said "my budget is $500".',
])
def test_conditional_reported_budget_cannot_raise_existing_limit(text):
    _,snap,_=authority_scene(text,budget_cents=5000)
    assert apply(snap['fan_state'],explicit_updates(snap['pending_fan_messages']))['budget_cents']==5000
    value=dict(kind='budget',active=True,message_id=1001,quote=text,product_id=None,amount_cents=50000)
    assert apply(snap['fan_state'],observations(decision(observations=[value]),snap))['budget_cents']==5000


@pytest.mark.parametrize('text,amount',[
    ('My budget is $50.',5000),('Actually my budget is only $10',1000),
    ('Hello, my budget is $50.',5000),
    ('My budget is $100, actually my budget is $50.',5000),
])
def test_current_affirmative_budget_still_sets_the_last_exact_limit(text,amount):
    _,snap,_=authority_scene(text,budget_cents=100)
    assert apply(snap['fan_state'],explicit_updates(snap['pending_fan_messages']))['budget_cents']==amount


def test_personal_budget_model_annotation_is_supported_without_inventing_wrong_amount():
    text='thats too much, i only have $20'
    _,snap,_=authority_scene(text)
    value=dict(kind='budget',active=True,message_id=1001,quote=text,product_id=None,amount_cents=2000)
    assert apply(snap['fan_state'],observations(decision(observations=[value]),snap))['budget_cents']==2000
    value['amount_cents']=6000
    assert 'budget_cents' not in apply(snap['fan_state'],observations(decision(observations=[value]),snap))


@pytest.mark.parametrize('fact',[
    'A photograph without a toy.',
    'A photograph. No toy is present.',
    'A photograph. A toy is not visible.',
])
def test_negated_asset_feature_does_not_authorize_that_requested_subject(fact):
    text='Send me the toy photo.'
    settings,snap,_=authority_scene(text,no_push=True)
    settings['products'][0]['assets'][0]['facts']=[fact]
    snap['products']['first']['assets'][0]['facts']=[fact]
    with pytest.raises(ActionBlocked):
        validate_decision(settings,snap,parsed_offer(request=text),planning=True)


def test_positive_asset_feature_can_identify_the_requested_subject():
    text='Send me the toy photo.'
    settings,snap,_=authority_scene(text,no_push=True)
    settings['products'][0]['assets'][0]['facts']=['A photograph with a toy.']
    snap['products']['first']['assets'][0]['facts']=['A photograph with a toy.']
    assert validate_decision(settings,snap,parsed_offer(request=text),planning=True)['media_ids']==[101]


@pytest.mark.parametrize('latest',['No thanks.', "Actually, don't do that.", 'How was your day?'])
def test_later_input_must_not_be_ignored_when_reusing_old_restricted_request(latest):
    old='Please send me the first photo.'
    settings,snap,_=authority_scene(old,no_push=True)
    snap['pending_fan_messages'].append({'id':1002,'direction':'in','text':latest})
    snap['history']=list(snap['pending_fan_messages'])
    with pytest.raises(ActionBlocked):
        validate_decision(settings,snap,parsed_offer(request=old),planning=True)


def test_later_affirmation_retains_the_restricted_request():
    old='Please send me the first photo.'
    settings,snap,_=authority_scene(old,no_push=True)
    snap['pending_fan_messages'].append({'id':1002,'direction':'in','text':'Yes, please.'})
    snap['history']=list(snap['pending_fan_messages'])
    assert validate_decision(settings,snap,parsed_offer(request=old),planning=True)['media_ids']==[101]
