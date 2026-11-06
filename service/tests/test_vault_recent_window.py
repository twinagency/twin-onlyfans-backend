from datetime import datetime, timedelta
import json
import pytest
import vault_import_policy as policy


def rows(kind,n,start=1):return [{'id':start+i,'type':kind} for i in range(n)]


def test_policy_is_exact_and_invalid_or_changed_progress_is_refused():
    assert policy.CAPS=={'photo':1500,'video':700}
    state=policy.start(datetime(2026,11,6))
    assert policy.load(policy.dump(state))==state
    state['policy']['photos']=1600
    with pytest.raises(ValueError):policy.dump(state)


def test_each_kind_has_its_own_hard_page_boundary():
    now=datetime(2026,11,6);state=policy.start(now)
    state['photo']['offset']=1499
    state,read=policy.next_read(state,now,24)
    assert read=={'kind':'photo','offset':1499,'limit':1,'head':False,'started':now.isoformat()}
    state=policy.advance(state,read,rows('photo',1),True,[False],'a'*64,now,24)
    assert state['photo']['done']
    state['video']['offset']=699
    state,read=policy.next_read(state,now,24)
    assert read['kind']=='video' and read['limit']==1
    state=policy.advance(state,read,rows('video',1),True,[False],'b'*64,now,24)
    assert policy.complete(state)


def test_new_heads_are_checked_while_initial_import_remains_in_progress():
    now=datetime(2026,11,6);state=policy.start(now)
    state['photo']['offset']=240
    state,read=policy.next_read(state,now+timedelta(minutes=5),6)
    assert read['head'] and read['offset']==0
    state=policy.advance(state,read,rows('photo',6),True,[True]*6,'a'*64,now,6)
    state,read=policy.next_read(state,now+timedelta(minutes=5),6)
    assert read['kind']=='video' and read['head']
    state=policy.advance(state,read,rows('video',6),True,[True]*6,'b'*64,now+timedelta(minutes=5),6)
    assert state['head'] is None and state['photo']['offset']==240
    assert not policy.complete(state)


def test_refresh_overlaps_known_items_and_does_not_stop_on_a_single_old_item():
    now=datetime(2026,11,6);state=policy.start(now,'head')
    state,read=policy.next_read(state,now,6)
    state=policy.advance(state,read,rows('photo',6),True,[False]*5+[True],'a'*64,now,6)
    assert not state['photo']['done']
    state,read=policy.next_read(state,now,6)
    state=policy.advance(state,read,[],False,[],'b'*64,now,6)
    state,read=policy.next_read(state,now,6)
    state=policy.advance(state,read,rows('photo',6,7),True,[True]*6,'c'*64,now,6)
    assert policy.complete(state)


@pytest.mark.parametrize('problem',['wrong_kind','too_many','repeated'])
def test_bad_provider_page_cannot_advance_progress(problem):
    now=datetime(2026,11,6);state=policy.start(now)
    state,read=policy.next_read(state,now,6)
    items=rows('video' if problem=='wrong_kind' else 'photo',7 if problem=='too_many' else 6)
    if problem=='repeated':state['photo']['hash']='a'*64
    with pytest.raises(ValueError):policy.advance(state,read,items,True,[False]*len(items),'a'*64,now,6)


@pytest.mark.parametrize('kind',['photo','video'])
def test_native_request_uses_type_and_recent_descending_order(monkeypatch,kind):
    from urllib.parse import urlsplit,parse_qs
    from test_onlyfans_provider_adapters import credential
    from onlyfans_runtime.provider_connector import NativeProviderConnector
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    calls=[]
    def call(self,method,path,body=b''):
        calls.append((method,path))
        return b'{"id":7001}' if path.endswith('/users/me') else b'{"list":[],"hasMore":false}'
    monkeypatch.setattr(NativeProviderConnector,'_call',call)
    NativeProviderConnector(credential()).vault_page(offset=699,limit=1,kind=kind)
    assert calls[0]==('GET','/api2/v2/users/me')
    query=parse_qs(urlsplit(calls[1][1]).query)
    assert query=={'limit':['1'],'offset':['699'],'sort':['desc'],'field':['recent'],
        'list':['all'],'type':[kind]}
