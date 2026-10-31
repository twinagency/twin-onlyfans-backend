from datetime import date
import json
import pytest
from onlyfans_runtime.profile_learning.evidence import validate,merge,project,summary,select_memories,digest
from onlyfans_runtime.profile_learning.temporal import resolve,age_on,age_range


def fact(quote,field='event',at='2026-01-01T10:00:00',mid=1,**kw):
    value=kw.pop('value',quote)
    proposal={'field':field,'value':value,'quote':quote,'source':f'1:{mid}',**kw}
    sources={f'1:{mid}':{'text':quote,'at':at,'direction':'in','digest':digest(quote),'fan':1}}
    result=validate({'facts':[proposal]},sources)
    assert len(result)==1
    return result[0]


def test_old_relative_week_uses_original_message_and_known_timezone():
    assert resolve('next week','2026-01-04T20:00:00Z','Australia/Brisbane')==('2026-01-12','2026-01-18','week')
    assert resolve('next week','2026-01-04T20:00:00Z','America/New_York')==('2026-01-05','2026-01-11','week')
    f=fact("I'm going to my brother's wedding next week",date_text='next week',key='brother wedding')
    assert f['event_start']=='2026-01-05' and f['event_end']=='2026-01-11'
    assert project(f,now='2026-10-29')['status']=='expired'


@pytest.mark.parametrize('today,status',[('2026-01-03','upcoming'),('2026-01-10','current'),
    ('2026-01-12','past'),('2026-01-24','past'),('2026-01-25','expired')])
def test_wedding_lifecycle_never_asserts_attendance(today,status):
    f=fact("My brother's wedding is next week",date_text='next week',key='brother wedding')
    p=project(f,now=today)
    assert p['status']==status
    if status=='past': assert p['event_outcome']=='unconfirmed'


def test_cancelled_wedding_not_reactivated_by_an_old_plan():
    planned=fact('My brother wedding is next week',date_text='next week',key='brother wedding')
    cancelled=fact('My brother wedding was cancelled',at='2026-01-03',mid=2,key='brother wedding',operation='cancel')
    f=merge([planned],[cancelled])[0]
    assert project(f,now='2026-01-04')['status']=='cancelled'
    assert not select_memories([f],now='2026-01-04')
    assert select_memories([f],'brother wedding',now='2026-01-04')[0]['status']=='cancelled'


def test_postponement_replaces_dates_and_unresolved_postponement_stays_uncertain():
    old=fact('My brother wedding is next week',date_text='next week',key='brother wedding')
    new=fact('My brother wedding postponed to 2026-02-15',at='2026-01-03',mid=2,
             date_text='2026-02-15',key='brother wedding',operation='postpone')
    f=merge([old],[new])[0]
    assert project(f,now='2026-02-01')['status']=='upcoming'
    assert f['event_end']=='2026-02-15' and f['history'][0]['status']=='superseded'
    unknown=fact('My brother wedding postponed',at='2026-01-04',mid=3,key='brother wedding',operation='postpone')
    f=merge([f],[unknown])[0]
    assert f['event_end'] is None and project(f,now='2026-01-05')['status']=='uncertain'
    assert project(f,now='2026-02-05')['status']=='expired'


def test_wedding_attendance_vs_lasting_marriage():
    event=fact('I am attending the wedding tomorrow',date_text='tomorrow',key='wedding')
    married=fact('I got married',field='relationship',kind='changing',operation='update')
    assert project(event,now='2026-02-01')['status']=='expired'
    assert project(married,now='2026-02-01')['status']=='current'


def test_unresolvable_event_review_does_not_stay_upcoming_forever():
    f=fact('I might visit a wedding sometime',date_text='sometime',key='wedding')
    assert f['event_start'] is None
    assert project(f,now='2026-01-02')['status']=='uncertain'
    assert project(f,now='2026-02-01')['status']=='expired'


@pytest.mark.parametrize('born,today,expected',[('2000-10-29','2026-10-28',25),
    ('2000-10-29','2026-10-29',26),('2000-10-29','2026-10-30',26),
    ('2000-02-29','2027-02-28',26),('2000-02-29','2027-03-01',27),
    ('2000-02-29','2028-02-29',28)])
def test_birthdays_and_leap_years(born,today,expected):
    assert age_on(born,today)==expected


def test_age_statement_has_no_invented_birthday():
    f=fact("I'm 26",field='age',value='26')
    assert age_range(26,'2026-01-01','2026-01-01')==(26,26)
    p=project(f,now='2026-10-29')
    assert not p['age_exact'] and 'uncertain' in p['display_value']
    assert 'birth' not in f and 'event_start' in f and f['event_start'] is None


def test_fan_age_from_question_and_short_answer_keeps_context():
    result={'facts':[{'field':'age','value':'26','source':'1:2','quote':'26',
                     'context_sources':['q'],'context_quotes':['How old are you?']}]}
    sources={'q':{'text':'How old are you?','at':'2026-01-01','direction':'out','digest':'q','fan':1,'message_source':'1:1','context_only':True},
             '1:2':{'text':'26','at':'2026-01-01','direction':'in','digest':'a','fan':1}}
    assert validate(result,sources)[0]['context_evidence'][0]['source']=='1:1'
    result['facts'][0].pop('context_sources');result['facts'][0].pop('context_quotes')
    assert not validate(result,sources)


def test_clear_location_update_supersedes_but_ambiguity_stays_uncertain():
    a=fact('I live in Brisbane',field='location',mid=1)
    b=fact('I live in Sydney',field='location',at='2026-02-01',mid=2)
    assert merge([a],[b])[0]['status']=='uncertain'
    c=fact('I moved to Sydney',field='location',at='2026-02-01',mid=3,operation='update')
    f=merge([a],[c])[0]
    assert f['value']=='I moved to Sydney' and f['history'][0]['status']=='superseded'
    assert project(f,now='2026-10-29')['potentially_stale']


def test_job_correction_requires_clear_evidence():
    old=fact('I am a plumber',field='occupation')
    bad=fact('I am a builder',field='occupation',at='2026-02-01',mid=2,operation='correct')
    assert bad['operation']=='assert' and merge([old],[bad])[0]['status']=='uncertain'
    corrected=fact('Actually I am a builder',field='occupation',at='2026-02-01',mid=3,operation='correct')
    assert merge([old],[corrected])[0]['history'][0]['value']=='I am a plumber'


def test_requests_do_not_expire_and_only_acknowledged_receipt_fulfills():
    request=fact('Please send my wedding photo',field='request',key='wedding photo')
    assert project(request,now='2030-01-01')['status']=='current'
    done=fact('I received the wedding photo',field='request',at='2026-02-01',mid=2,key='wedding photo',operation='fulfill')
    f=merge([request],[done])[0]
    assert project(f,now='2030-01-01')['status']=='past'
    assert not select_memories([f],now='2030-01-01')


def test_repeated_mentions_merge_without_profile_growth():
    observations=[fact('I love tennis',field='interests',at=f'2026-01-{i:02}',mid=i) for i in range(1,30)]
    result=merge([],observations)
    assert len(result)==1 and len(result[0]['evidence'])==29
    assert len(summary(result).split())<35


def test_name_and_occupation_survive_temporary_chatter():
    name=fact('My name is John',field='name',value='John')
    job=fact('I am a plumber',field='occupation')
    chatter=[fact(f'My weekend event {i}',mid=i+10,key=f'weekend event {i}') for i in range(200)]
    result=merge([name,job],chatter)
    assert len(result)==202
    selected=select_memories(result,now='2026-10-29')
    assert any(f['field']=='name' for f in selected)
    assert any(f['field']=='occupation' for f in selected)
    assert len(summary(result,now='2026-10-29').split())<=250


def test_expired_topic_is_retrievable_but_not_in_ordinary_reply_context():
    old=fact('My brother wedding tomorrow',date_text='tomorrow',key='brother wedding')
    assert select_memories([old],now='2026-10-29')==[]
    assert select_memories([old],'How was my brother wedding?',now='2026-10-29')[0]['status']=='expired'
    assert 'wedding' not in summary([old],now='2026-10-29')


def test_topic_importance_and_hard_bytes():
    facts=[fact(f'I enjoy interest {i}',field='interests',key=f'interest {i}',mid=i+1) for i in range(30)]
    result=select_memories(facts,'interest 25',limit=10,byte_budget=1400)
    assert len(json.dumps(result,ensure_ascii=False).encode())<=1400
    assert any('25' in f['value'] for f in result)


def test_manual_memory_survives_automatic_correction():
    old=fact('My name is John',field='name',value='John');old.update(manual=True,protected=True)
    new=fact('Actually my name is Bob',field='name',value='Bob',mid=2,operation='correct')
    assert merge([old],[new])[0]['value']=='John'


def test_subscription_or_spend_claims_are_not_supported_fields():
    sources={'1:1':{'text':'I spent $500','at':'2026-01-01','direction':'in','digest':'x'}}
    assert not validate({'facts':[{'field':'spend','value':'$500','quote':'I spent $500','source':'1:1'}]},sources)


def test_short_cancellation_uses_retained_question_without_inventing_an_event():
    sources={'q':{'text':'Is your brother wedding next week still on?','at':'2026-01-01','direction':'out','digest':'q','fan':1,'message_source':'1:1','context_only':True},
             '1:2':{'text':'It was cancelled','at':'2026-01-03','direction':'in','digest':'a','fan':1}}
    result=validate({'facts':[{'field':'event','value':'cancelled','source':'1:2','quote':'It was cancelled',
        'key':'brother wedding','operation':'cancel','context_sources':['q'],
        'context_quotes':['Is your brother wedding next week still on?']}]},sources)
    assert len(result)==1 and result[0]['operation']=='cancel'


def test_age_restatement_refreshes_age_without_inventing_birthday():
    first=fact("I'm 26",field='age',value='26')
    repeated=fact("I'm 26",field='age',value='26',mid=2,at='2026-10-29')
    memory=merge([first],[repeated])[0]
    assert project(memory,now='2026-10-29')['age_exact']
    assert len(memory['evidence'])==2
