"""Populated contextual rollback and configuration preservation on real PostgreSQL."""
import json
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import pytest

from test_onlyfans_foundation_pg import database
from test_onlyfans_configuration_pg import setup, save, TENANT, ACCOUNT
from db.models import AccountAiConfig, OfConversationState
from db.onlyfans_contextual_models import OfContextualOffer
from db.schema import _config, upgrade, check_schema
from onlyfans_runtime.configuration import get_configuration
from onlyfans_runtime.store import utcnow


def test_mode_save_preserves_scope_and_populated_rollback_retains_unknown(database):
    store = setup(database)
    with Session(database[1]) as s:
        before = json.loads(s.get(AccountAiConfig, ACCOUNT).ai_chatter_config_json)
    for index, mode in enumerate(('shadow', 'live', 'off')):
        data = get_configuration(store, TENANT, ACCOUNT)
        save(store, data, {'fasttContextualSalesMode': mode}, key=str(index + 1)*64)
        assert get_configuration(store, TENANT, ACCOUNT)['fasttContextualSalesMode'] == mode
        with Session(database[1]) as s:
            raw = json.loads(s.get(AccountAiConfig, ACCOUNT).ai_chatter_config_json)
            assert raw == {**before, 'fastt_contextual_sales_mode': mode}
            state = s.get(OfConversationState, (ACCOUNT, 101))
            assert (state.mode, state.revision, state.unresolved_attempt_id) == ('no_contact', 7, 'retain-unknown')
    def origin(key='a', mid=10):
        return OfContextualOffer(account_id=ACCOUNT, decision_key=key*64, fan_id=101,
            binding_id='binding-'+ACCOUNT, source_message_id=mid, content_key='c'*64,
            snapshot_json='{"synthetic":"retained"}', attempt_id=123, state='unknown', created_at=utcnow())
    with Session(database[1]) as s, s.begin():
        s.add(origin())
    with pytest.raises(IntegrityError):
        with Session(database[1]) as s, s.begin():
            s.add(origin('b', 11))
    with database[1].begin() as c:
        command.downgrade(_config(c), '0100_manual_free_media')
        assert c.scalar(text('select count(*) from of_contextual_offer')) == 1
        upgrade(c)
        check_schema(c)
        assert c.scalar(text("select state from of_contextual_offer")) == 'unknown'
    with Session(database[1]) as s:
        row = s.scalar(select(OfContextualOffer))
        assert row.attempt_id == 123 and row.snapshot_json == '{"synthetic":"retained"}'
    store.close()
