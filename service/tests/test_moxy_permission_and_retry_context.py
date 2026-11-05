"""Source condition and duplicate-card retry context; no provider calls."""
from copy import deepcopy
import pytest
from moxy_replacement.configuration import load, permission_context, ActionBlocked
from moxy_replacement.catalogue_context import project

@pytest.mark.parametrize('creator,facts',[
 ('amelia',{}),('amelia',{'spender':False,'vip':False}),
 ('amelia',{'spender':True}),('amelia',{'spender':True,'vip':False}),
 ('amelia',{'spender':False,'vip':True}),('amelia',{'spender':True,'vip':True}),
 ('amelia',{'spender':1,'vip':True}),('cristina',{}),
])
def test_writer_permission_matches_source_resolution_without_mutating_purchase_state(creator,facts):
 cfg=load(creator);before=deepcopy(facts)
 try:expected=cfg.resolve('sexting',facts).value is True;reason=None
 except ActionBlocked as exc:expected=False;reason=str(exc)
 resolved=permission_context(cfg.public_context(),facts,'sexting')
 assert resolved['allowed'] is expected
 if reason:assert resolved['reason']==reason
 assert facts==before
 assert 'purchased_products' not in facts and 'owned_assets' not in facts


def scene(kind='product'):
 products={};inventory={}
 for i,key in enumerate(['old','fresh']):
  products[key]={'category':'nude_booty','price_cents':7500,'currency':'USD','available':True,
   'requires':{},'source':{'document':'01_Amelia.pdf','pages':[2]},'caption_instructions':'Use verified facts.',
   'assets':[{'id':100+i,'facts':['A verified photograph '+key]}]}
  inventory[key]={'media_ids':[100+i],'preview_ids':[], 'item':{'id':10+i,'label':key},
   'files':[{'media_id':100+i,'kind':'photo','description_basis':{'frames':1}}]}
 return {'products':products,'inventory':inventory,'instructions':{},
  'offer_eligibility':{'old':'media_previously_sent_requires_explicit_resend','fresh':'ok'},
  'event':{'payload':{'rejected':{'kind':kind,'product_id':'old' if kind=='product' else None}}},
  'card_state':[{'id':9001,'product_id':'old','price_cents':7500,'paid':False}],
  'fan_state':{'purchased_products':[],'owned_assets':[],'pending_products':['old']},'history':[]}


def test_duplicate_choice_moves_to_discussion_context_without_losing_card_or_asset_evidence():
 snap=scene();before=deepcopy(snap);out=project(snap)
 assert snap==before and out['new_card_product_ids']==['fresh']
 assert 'old' not in out['products'] and 'fresh' in out['products']
 old=out['rejected_product_context']
 assert old['product_id']=='old' and old['media_ids']==[100] and old['price_cents']==7500
 assert old['assets'][0]['facts']==['A verified photograph old']
 assert out['card_state']==before['card_state'] and out['fan_state']==before['fan_state']


def test_factual_retry_keeps_valid_selected_product_in_context():
 snap=scene('factual');snap['offer_eligibility']['old']='ok'
 snap['event']['payload']['rejected']['keep']={'action':'offer','product_id':'old','sequence':None}
 out=project(snap)
 assert set(out['products'])=={'old','fresh'}
 assert 'rejected_product_context' not in out
 assert out['products']['old']['media_ids']==[100]
 assert out['event']['payload']['rejected']['keep']['product_id']=='old'


@pytest.mark.parametrize('text,question,secondary',[
 ("this is the closest I've got",'Send me a photo.','repeated_recent_phrase'),
 ('Another approved angle.','Send me a photo.',None),
 ('It costs $75.','Please repeat that price.',None),
])
def test_product_retry_includes_independent_wording_problem_in_its_single_correction(text,question,secondary):
 from moxy_replacement.runtime import correction_brief
 from test_moxy_integrated_pg import decision
 snap=scene();snap['history']=[{'id':1,'direction':'out','text':"Here, this is the closest I've got. It costs $75."},
  {'id':2,'direction':'in','text':question}]
 snap['pending_fan_messages']=[snap['history'][-1]]
 snap['event']['payload']={}
 d=decision('offer',text,'old',messages=[{'text':text,'media':True}])
 before=deepcopy(snap)
 brief=correction_brief('media_previously_sent_requires_explicit_resend',d,snap)
 assert brief['reason']=='media_previously_sent_requires_explicit_resend'
 assert brief.get('secondary_rejection')==secondary
 assert brief['kind']=='product' and brief['product_id']=='old' and 'keep' not in brief
 assert brief['avoid']==[text]
 assert brief['existing_cards'][0]['id']==9001
 assert brief['existing_cards'][0]['paid'] is False
 assert brief['existing_cards'][0]['verified_assets']==snap['products']['old']['assets']
 assert 'text' not in brief['existing_cards'][0]
 assert snap==before


@pytest.mark.parametrize('facts,override,allowed', [
    ({}, None, False),
    ({'spender': False, 'vip': False}, None, False),
    ({'spender': True, 'vip': True}, None, True),
    ({}, True, True),
    ({'spender': False, 'vip': False}, True, True),
    ({'spender': True, 'vip': True}, False, False),
])
def test_writer_uses_only_resolved_conversation_permission_branch(facts, override, allowed):
    import json
    from moxy_replacement.configuration import CreatorConfiguration, load_foundation
    from moxy_replacement.writer import messages

    creator = deepcopy(load('amelia').creator)
    if override is not None:
        rule = next(rule for rule in creator['rules'] if rule['key'] == 'sexting')
        rule['value'] = override
        rule['requires'] = {}
        rule['instruction'] = 'Operator-approved fixture conversation permission.'
    configuration = CreatorConfiguration(load_foundation(), creator)
    snap = scene()
    snap['event']['payload'] = {}
    snap['instructions'] = configuration.public_context()
    snap['fan_state'].update(facts)
    snap['history'] = [
        {'id': 1, 'direction': 'out', 'text': 'That topic is off the table for me.'},
        {'id': 2, 'direction': 'in', 'text': 'Why did you say that?'},
    ]
    snap['pending_fan_messages'] = [snap['history'][-1]]
    before = deepcopy(snap)

    prompt = messages(snap)
    system = prompt[0]['content']
    payload = json.loads(prompt[1]['content'])
    resolved = permission_context(snap['instructions'], snap['fan_state'], 'sexting')
    assert resolved['allowed'] is allowed
    assert payload['permission_status']['sexting'] == resolved
    assert ('The resolved conversation permission is allowed.' in system) is allowed
    assert ('The resolved conversation permission is unavailable for this fan.' in system) is not allowed
    assert 'Do not describe genital contact' not in system
    assert 'waive the sexting restriction' not in system
    assert 'Outgoing history, saved excerpts and writing examples record what was said' in system
    assert 'Preserve a boundary when a current rule supports it' in system
    assert payload['history'] == before['history']
    assert payload['pending_fan_messages'] == before['pending_fan_messages']
    assert snap == before
    current_rule = next(rule for rule in snap['instructions']['rules'] if rule['key'] == 'sexting')
    # Full authority stays in the snapshot. Unknown override text is preserved
    # in the scoped user view; reviewed forms use the same resolved permission.
    assert snap['instructions']['rules'] == before['instructions']['rules']
    view = payload['creator_rules']
    if view['dispositions']['sexting'] == 'unreviewed_preserved':
        assert current_rule['instruction'] in json.dumps(view)
    else:
        assert view['dispositions']['sexting'] == 'resolved_conversation_permission'
