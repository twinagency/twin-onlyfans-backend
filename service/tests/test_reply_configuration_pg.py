"""Actual PostgreSQL revision/idempotency/field-isolation checks for both opt-ins."""
import json

import pytest
from sqlalchemy.orm import Session
from test_onlyfans_configuration_pg import database, setup, save, ACCOUNT
from db.models import AccountAiConfig
from onlyfans_runtime.configuration import get_configuration, InvalidConfiguration
from onlyfans_runtime.store import Refused
from test_onlyfans_text_delivery_pg import TENANT


def test_independent_field_save_preserves_contextual_and_other_settings(database):
    store=setup(database)
    with Session(database[1]) as s,s.begin():
        row=s.get(AccountAiConfig,ACCOUNT)
        values=json.loads(row.ai_chatter_config_json)
        values.update(fastt_contextual_sales_mode='shadow',native_reply_format_enabled=True)
        row.ai_chatter_config_json=json.dumps(values)
    before=get_configuration(store,TENANT,ACCOUNT)
    assert before['nativeReplyPacingEnabled'] is False
    assert before['nativeReplyFormatEnabled'] is True
    result=save(store,before,{'nativeReplyPacingEnabled':True})
    assert save(store,before,{'nativeReplyPacingEnabled':True})==result
    with Session(database[1]) as s:
        settings=json.loads(s.get(AccountAiConfig,ACCOUNT).ai_chatter_config_json)
        assert settings['fastt_contextual_sales_mode']=='shadow'
        assert settings['native_reply_format_enabled'] is True
        assert settings['native_reply_pacing_enabled'] is True
        assert settings['private']=='retained'
    with pytest.raises(Refused,match='revision conflict'):
        save(store,before,{'nativeReplyFormatEnabled':False},key='c'*64)


@pytest.mark.parametrize('key',['nativeReplyPacingEnabled','nativeReplyFormatEnabled'])
@pytest.mark.parametrize('value',[None,0,1,'true','false',[],{}])
def test_invalid_writes_are_rejected(database,key,value):
    store=setup(database)
    before=get_configuration(store,TENANT,ACCOUNT)
    with pytest.raises(InvalidConfiguration):save(store,before,{key:value})
    assert get_configuration(store,TENANT,ACCOUNT)['configurationRevision']==before['configurationRevision']
