"""Genuine native dialect shape and confirmed unread undo; no provider calls."""
from copy import deepcopy
from datetime import datetime,timedelta
import json
from pathlib import Path
import pytest
from native_pilot.recovery import _matches,proof_hash
from unread_preservation import preserve_unread,UnreadPreservationError

STAMP=datetime(2026,11,4,13,56,6)
INTENT={'account_id':'of-312988353','fan_id':318022380,'body':'Saved exact text.','content':None,'created_at':STAMP.isoformat()}

def native():
    return {'id':11928289638212,'fromUser':{'id':312988353},'chatUserId':318022380,'text':'Saved exact text.',
        'createdAt':(STAMP+timedelta(seconds=9)).isoformat()+'Z','media':[],'mediaCount':0,'price':0,'previews':[],'lockedText':False,'responseType':'message'}

def test_native_chat_recipient_is_exact_proof_without_rewriting_provider_fields():
    raw=native();before=deepcopy(raw)
    assert _matches(raw,INTENT).message_id==raw['id']
    assert len(proof_hash(raw,INTENT))==64 and raw==before
    assert 'toUser' not in raw and 'withUser' not in raw

@pytest.mark.parametrize('case',['wrong_chat','chat_bool','chat_string','missing_chat','wrong_to','null_to','bool_to','wrong_with','null_with','wrong_sender','text','price','media','previews','locked','too_old','too_new'])
def test_every_supplied_identity_and_frozen_term_must_agree(case):
    raw=native()
    if case=='wrong_chat':raw['chatUserId']=318022381
    elif case=='chat_bool':raw['chatUserId']=True
    elif case=='chat_string':raw['chatUserId']='318022380'
    elif case=='missing_chat':del raw['chatUserId']
    elif case=='wrong_to':raw['toUser']={'id':318022381}
    elif case=='null_to':raw['toUser']=None
    elif case=='bool_to':raw['toUser']={'id':True}
    elif case=='wrong_with':raw['withUser']={'id':318022381}
    elif case=='null_with':raw['withUser']=None
    elif case=='wrong_sender':raw['fromUser']['id']=312988354
    elif case=='text':raw['text']='Different text.'
    elif case=='price':raw['price']=20
    elif case=='media':raw['media']=[{'id':12,'type':'photo'}];raw['mediaCount']=1
    elif case=='previews':raw['previews']=[12]
    elif case=='locked':raw['lockedText']=True
    elif case=='too_old':raw['createdAt']=(STAMP-timedelta(seconds=6)).isoformat()+'Z'
    elif case=='too_new':raw['createdAt']=(STAMP+timedelta(minutes=6)).isoformat()+'Z'
    with pytest.raises(Exception):_matches(raw,INTENT)


def test_all_supplied_recipients_can_agree_and_legacy_shape_remains_valid():
    raw=native();raw.update(toUser={'id':318022380},withUser={'id':318022380})
    assert _matches(raw,INTENT).fan_id==318022380
    del raw['chatUserId'];assert _matches(raw,INTENT).fan_id==318022380

@pytest.mark.parametrize('mode',['already_unread','http_success_but_read','lost_response_restored','wrong_chat'])
def test_pending_undo_requires_actual_scoped_state_before_marker_resolution(tmp_path,monkeypatch,mode):
    monkeypatch.setenv('ONLYFANS_UNREAD_JOURNAL_DIR',str(tmp_path));marker=tmp_path/'of-test-7.pending';marker.write_text('pending\n')
    state={'unread':mode=='already_unread'};calls=[]
    def snap():return {'withUser':{'id':8 if mode=='wrong_chat' else 7},'unreadMessagesCount':int(state['unread'])}
    def restore(fan):
        assert fan==7;calls.append('restore')
        if mode=='lost_response_restored':state['unread']=True;raise TimeoutError('secret proxy must not be exposed')
        return {'success':True}
    def read():calls.append('read');return 'no network'
    kwargs=dict(account_id='of-test',fan_id=7,snapshot=snap,confirm=snap,read=read,restore=restore)
    if mode in ['http_success_but_read','wrong_chat']:
        with pytest.raises(UnreadPreservationError):preserve_unread(**kwargs)
        assert marker.exists() and 'read' not in calls
    else:
        assert preserve_unread(**kwargs)=='no network';assert not marker.exists()
        if mode=='already_unread':assert calls==['read']
        else:assert calls.count('restore')==1
