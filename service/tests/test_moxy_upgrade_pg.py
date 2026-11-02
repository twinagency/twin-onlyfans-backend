"""Populated published schemas upgrade without activating Moxy or losing evidence."""
import importlib
import pytest
from sqlalchemy import text,select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_onlyfans_foundation_pg import database,event
from test_onlyfans_text_delivery_pg import configured,prepare,unthrottle,TENANT,ACCOUNT
from test_moxy_integrated_pg import setup
from test_onlyfans_offer_import_upgrade_pg import historical_schema
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.store import utcnow
from db.schema import upgrade,check_schema,HEAD
from db.models import Message
from db.onlyfans_moxy_models import MoxyAudit,MoxyFan

@pytest.mark.parametrize('revision',['0110_recent_history','0106_parallel_history_reads'])
def test_populated_upgrade_preserves_work_receipts_and_exact_purchase(revision,database):
    _,engine=database;store,authority=configured(database)
    accepted=prepare(authority,'accepted')
    assert authority.dispatch(TENANT,ACCOUNT,accepted,SyntheticTextTransport(ACCOUNT))=='accepted'
    unthrottle(engine)
    unknown=prepare(authority,'unknown')
    assert authority.dispatch(TENANT,ACCOUNT,unknown,SyntheticTextTransport(ACCOUNT,outcome='accepted_lost_response'))=='unknown'
    with Session(engine) as s,s.begin():
        msg=s.get(Message,(ACCOUNT,101,9001));msg.is_paid=True;msg.purchased_at=utcnow()
    with engine.begin() as c:
        assert c.scalar(text('select current_database()')).startswith('of_test_')
        historical_schema(c,revision)
        tables=['accounts','fans','messages','of_runtime','of_conversation_state','of_text_intent','of_message_delivery','of_delivery_policy']
        before={t:[dict(r) for r in c.execute(text('SELECT * FROM '+t+' ORDER BY 1,2')).mappings()] for t in tables}
        assert c.scalar(text("SELECT to_regclass('of_moxy_account')")) is None
        upgrade(c);upgrade(c);check_schema(c)
        assert c.scalar(text('SELECT version_num FROM alembic_version'))==HEAD
        for t in tables:
            rows=[dict(r) for r in c.execute(text('SELECT * FROM '+t+' ORDER BY 1,2')).mappings()]
            assert [{k:r[k] for k in old} for r,old in zip(rows,before[t])]==before[t]
        assert c.scalar(text('SELECT count(*) FROM of_moxy_account'))==0
        assert c.scalar(text('SELECT count(*) FROM of_moxy_event'))==0
        assert c.scalar(text("SELECT count(*) FROM pg_class WHERE relname IN ('of_moxy_account','of_moxy_event','of_moxy_fan','of_moxy_audit') AND relrowsecurity"))==4
        assert not c.scalar(text("SELECT EXISTS (SELECT 1 FROM information_schema.role_table_grants WHERE table_name LIKE 'of_moxy_%' AND grantee IN ('PUBLIC','anon','authenticated'))"))
    store.close()


def test_audit_is_immutable_and_disable_preserves_obligations(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    from moxy_replacement.controls import update_fan
    update_fan(store,TENANT,ACCOUNT,101,state={'obligations':[{'id':'owed','status':'open','owner':'fixture','source_ref':'fixture:proof'}]},actor='fixture',evidence_ref='fixture:proof')
    with Session(engine) as s:
        before=s.get(MoxyFan,(ACCOUNT,101)).state_json
        identity=s.scalar(select(MoxyAudit.audit_id))
    for sql in ["UPDATE of_moxy_audit SET actor='changed' WHERE audit_id=:id","DELETE FROM of_moxy_audit WHERE audit_id=:id"]:
        with pytest.raises(DBAPIError,match='append only'):
            with engine.begin() as c:c.execute(text(sql),{'id':identity})
    service.configure(TENANT,ACCOUNT,expected_revision=1,mode='off',settings=settings,actor='fixture')
    with Session(engine) as s:assert s.get(MoxyFan,(ACCOUNT,101)).state_json==before
    from pathlib import Path
    file=next(Path(__file__).resolve().parents[1].joinpath('db/migrations/versions').glob('*moxy*'))
    module=importlib.import_module('db.migrations.versions.'+file.stem)
    with pytest.raises(RuntimeError,match='retain'):
        with engine.begin() as c:
            with Operations.context(MigrationContext.configure(c)):module.downgrade()
    store.close()
