"""Actual populated 0126-to-0127 migration; fixture authority is not a normal sales qualification."""
import uuid
from datetime import timedelta
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from db.schema import HEAD, _config, check_schema, upgrade
from conversation_engine.commerce import _hash
from onlyfans_runtime.media_delivery import catalog_terms
from onlyfans_runtime.store import utcnow
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT, FAN


def test_populated_legacy_grant_is_preserved_and_lineage_constraints_are_enforced(supervised):
    world, _, _, _, pilot = supervised
    pilot.worker.engine.select_simulated(TENANT, ACCOUNT, FAN)
    with Session(world.database[1]) as s:
        runtime = world.store._runtime(s, TENANT, ACCOUNT, lock=False)
        terms = catalog_terms(s, ACCOUNT, world.item, lock=False)
        old = dict(account_id=ACCOUNT, grant_id=str(uuid.uuid4()), fan_id=FAN, binding_id=runtime.binding_id,
            item_id=world.item, terms_hash=_hash(terms), coverage_ref='vault:migration-fixture',
            expires_at=utcnow()+timedelta(minutes=30), used_intent_id=None, created_at=utcnow())
    columns = list(old)
    insert = 'INSERT INTO of_engine_offer_grant ('+','.join(columns)+') VALUES ('+','.join(':'+key for key in columns)+')'
    with world.database[1].begin() as c:
        command.downgrade(_config(c), '0126_plan_c_ingress')
        check_schema(c, expected='0126_plan_c_ingress')
        c.execute(text(insert), old)
    with world.database[1].begin() as c:upgrade(c)
    with world.database[1].begin() as c:
        check_schema(c)
        assert HEAD == c.scalar(text('SELECT version_num FROM alembic_version')) == '0129_reconnect_receipt'
        saved = c.execute(text('SELECT * FROM of_engine_offer_grant')).mappings().one()
        assert {key:saved[key] for key in old} == old
        assert saved['issuance'] == 0 and saved['predecessor_grant_id'] is None
        assert saved['issuance_reason'] == 'initial' and __import__('json').loads(saved['authorization_json']) == {'kind':'legacy_initial'}
        with pytest.raises(DBAPIError):
            with c.begin_nested():c.execute(text(insert), {**old,'grant_id':str(uuid.uuid4())})
        for field, value in [('issuance',1), ('predecessor_grant_id',str(uuid.uuid4())),
                ('issuance_reason','expired_renewal'), ('authorization_json','{}'), ('expires_at',utcnow())]:
            with pytest.raises(DBAPIError):
                with c.begin_nested():c.execute(text('UPDATE of_engine_offer_grant SET '+field+'=:value'), {'value':value})
        with pytest.raises(DBAPIError):
            with c.begin_nested():c.execute(text('DELETE FROM of_engine_offer_grant'))
        child = {**old, 'grant_id':str(uuid.uuid4()), 'issuance':1,
            'predecessor_grant_id':old['grant_id'], 'issuance_reason':'expired_renewal',
            'authorization_json':'{"kind":"constraint_fixture_only"}'}
        keys = list(child)
        sql = 'INSERT INTO of_engine_offer_grant ('+','.join(keys)+') VALUES ('+','.join(':'+key for key in keys)+')'
        scope = c.begin_nested()
        c.execute(text(sql), child)
        with pytest.raises(DBAPIError):
            with c.begin_nested():c.execute(text(sql), {**child, 'grant_id':str(uuid.uuid4())})
        with pytest.raises(DBAPIError):
            with c.begin_nested():c.execute(text(sql), {**child, 'grant_id':str(uuid.uuid4()), 'issuance':2})
        with pytest.raises(RuntimeError, match='issuance evidence'):
            with c.begin_nested():command.downgrade(_config(c), '0126_plan_c_ingress')
        scope.rollback()  # Never leaves a fabricated authorization in application state.
        assert c.scalar(text('SELECT count(*) FROM of_engine_offer_grant')) == 1
        with c.begin_nested():
            c.execute(text('ALTER TABLE of_engine_offer_grant DROP CONSTRAINT uq_engine_offer_grant_successor'))
            with pytest.raises(__import__('db.schema',fromlist=['SchemaMismatch']).SchemaMismatch):check_schema(c)
        # The outer synthetic database is discarded by its fixture.
