import json
from pathlib import Path
import pytest
from operation_config import validate

def fixture_config(root='/tmp/twin-supervisor-fixture', account='of-323358988'):
    return {'version':'twin-supervision.v1','root':root,'tenant':'fixture','account':account,
      'binding':'01612323-b06f-5320-b623-4e032f474a6e','epoch':1,
      'fans':[318022380,601164546],'providerAccount':'acct_7bf8540de5a84f459058490fee26c47f','webhookId':'wh_db5f1a0931514ef9b285c412bede5713',
      'publicUrl':'https://fixture.invalid/onlyfans/webhooks/plan-c','receiverUrl':'http://127.0.0.1:55462',
      'senderUrl':'http://127.0.0.1:55464','databaseContainer':'fixture-pg','databaseRole':'fixture','databaseName':'fixture',
      'supervisorUnit':'fixture-supervisor.service','senderUnit':'fixture-sender.service','senderContainer':'fixture-sender',
      'teamAccounts':[account],'creditStop':300,'initialExternalReserved':0,'reservations':[]}

def test_ten_configurations_keep_state_identity_and_team_membership_separate(tmp_path):
    accounts=['creator-'+str(i) for i in range(10)]
    configs=[]
    for i,account in enumerate(accounts):
        value=fixture_config(str(tmp_path/account),account)
        value.update(providerAccount='acct_'+account,webhookId='wh_'+account,binding='binding-'+account,epoch=i+1,
                     senderUrl='http://127.0.0.1:'+str(56000+i),teamAccounts=accounts)
        configs.append(validate(value))
    assert len({c['root'] for c in configs})==len({c['providerAccount'] for c in configs})==10
    assert all(c['teamAccounts']==accounts for c in configs)

@pytest.mark.parametrize('change',[{'epoch':True},{'root':'relative'},{'publicUrl':'https://x.trycloudflare.com/a'},
 {'senderUrl':'http://192.0.2.1:5'}, {'teamAccounts':['someone-else']},{'creditStop':0},
 {'account':"injected' OR 1=1"}])
def test_config_refuses_ambiguous_or_unsafe_references(change):
    with pytest.raises(ValueError):validate({**fixture_config(),**change})


def test_supervisor_uses_selected_account_and_sums_only_reviewed_team(tmp_path):
    import supervisor
    c=fixture_config(str(tmp_path/'first'),'first')
    c['teamAccounts']=['first','second']
    supervisor.configure(c)
    assert supervisor.STATE==tmp_path/'first'/'supervision-v11'/'budget-state.json'
    assert "s.account_id='first'" in supervisor.SQL
    assert "t.account_id in ('first','second')" in supervisor.SQL
    assert 'scope_id=1' not in supervisor.SQL
    c=fixture_config(str(tmp_path/'second'),'second')
    supervisor.configure(c)
    assert supervisor.STATE==tmp_path/'second'/'supervision-v11'/'budget-state.json'
    assert "s.account_id='second'" in supervisor.SQL


def test_reviewed_shared_threshold_uses_existing_guard():
    from guard_core import credit_estimate,GuardFailure
    data=dict(baseline_total=0,baseline_balance=100,team_total=0,team_balance=100,
              usage_cost=0,scope_reserved=8,external_reserved=1,prior_estimate=0,credit_limit=10)
    assert credit_estimate(**data)==9
    with pytest.raises(GuardFailure,match='credit_early_stop'):
        credit_estimate(**{**data,'scope_reserved':9})
