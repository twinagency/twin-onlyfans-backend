"""Approved-only source retrieval; no model calls or database."""
import json
from moxy_replacement import examples


def history(text):return [{'id':1,'direction':'in','text':text}]


def test_generic_content_questions_are_not_unavailable_or_declined_contexts():
 cases=[('Can you send me a photo?',{'content_request','availability_question'}),('Is that video worth it?',{'content_question'}),('When did I say that?',{'clarification'})]
 labels=[]
 for text,expected in cases:
  refs,weights=examples.select('amelia',history(text))
  assert expected<=weights.keys()
  assert 'honest_unavailable_alternative' not in weights and 'declined_offer' not in weights
  assert all(e['situation'] not in {'honest_unavailable_alternative','declined_offer'} for e in refs)
  labels.append(set(weights))
 assert len({tuple(sorted(x)) for x in labels})==3


def test_genuine_amelia_holdout_is_wholly_absent_from_every_retrieval_mode():
 bank={e['record']:e for e in examples._bank()['examples']}
 assert bank[6]['creator_in_source']=='Amelia' and bank[6]['evaluation_only']
 assert examples.verified(bank[6]) and len(bank[6]['turns'])==7
 for exclude in [False,True]:
  chosen,_=examples.select('amelia',history('not feeling frisky, you look good'),exclude_held_back=exclude)
  assert 6 not in {e['record'] for e in chosen}


def test_complete_amelia_photo_and_ordinary_exchanges_remain_creator_specific():
 bank={e['record']:e for e in examples._bank()['examples']}
 photo,_=examples.select('amelia',history('Can you send me a photo?'))
 assert [e['record'] for e in photo]==[103601]
 assert len(bank[103601]['turns'])==4 and examples.verified(bank[103601])
 assert len(bank[107901]['turns'])==6 and examples.verified(bank[107901])
 assert bank[170]['retrieval_excluded']
 for creator in ['cristina','another_creator']:
  chosen,_=examples.select(creator,history('Can you send me a photo?'))
  assert not {103601,107901}&{e['record'] for e in chosen}
 for exclude in [False,True]:
  chosen,_=examples.select('amelia',history('Can you send me a photo?'),exclude_held_back=exclude)
  assert not {6,70,170}&{e['record'] for e in chosen}


def test_shared_behavior_is_labelled_and_never_claims_amelia_voice_coverage():
 refs=examples.references('amelia',history('I cannot afford that, my bills are overdue'))
 assert refs['examples'] and all(e['reference_role']=='shared_behavior_only' for e in refs['examples'])
 assert refs['creator_voice_reference_count']==0
 assert 'incomplete' in examples._bank()['creator_voice_coverage']['status']


def test_neutral_input_does_not_force_unrelated_voice_examples():
 refs=examples.references('amelia',history('hey'))
 assert refs['examples']==[] and refs['creator_voice_reference_count']==0
