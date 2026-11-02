"""Widen only the known pacing constraint; retain populated timing evidence."""
import pytest
from sqlalchemy import text

from test_onlyfans_foundation_pg import database
from native_pilot import pacing_schema
from twin_bridge import store


def test_populated_legacy_pacing_upgrade_is_idempotent_and_keeps_evidence(database):
    url, engine = database
    target = store.target_from_url(url)
    with engine.begin() as con:
        con.exec_driver_sql('CREATE TABLE native_pilots(pilot_id TEXT PRIMARY KEY)')
        con.exec_driver_sql(pacing_schema.DDL.replace('<=180', '<=90'))
        con.exec_driver_sql(pacing_schema.INDEX)
        con.exec_driver_sql("""INSERT INTO native_reply_pacing VALUES
            ('retained','account',1,'generation',42,'first-input','provider-input',
             'original-target',65,0,'waiting',2,'input','created','updated')""")
        before = dict(con.execute(text('SELECT * FROM native_reply_pacing')).mappings().one())
    pacing_schema.upgrade(target)
    pacing_schema.upgrade(target)
    with engine.begin() as con:
        assert dict(con.execute(text('SELECT * FROM native_reply_pacing')).mappings().one()) == before
        con.exec_driver_sql('UPDATE native_reply_pacing SET delay_seconds=180')
    with pytest.raises(Exception, match='native_reply_pacing_delay_bounds'):
        with engine.begin() as con:
            con.exec_driver_sql('UPDATE native_reply_pacing SET delay_seconds=181')
    pacing_schema.downgrade(target)
    with engine.connect() as con:
        assert con.scalar(text('SELECT count(*) FROM native_reply_pacing')) == 1


def test_unknown_pacing_constraint_is_refused_without_altering_rows(database):
    url, engine = database
    with engine.begin() as con:
        con.exec_driver_sql('CREATE TABLE native_pilots(pilot_id TEXT PRIMARY KEY)')
        con.exec_driver_sql(pacing_schema.DDL.replace('<=180', '<=100'))
    with pytest.raises(RuntimeError, match='unrecognized-native-pacing'):
        pacing_schema.upgrade(store.target_from_url(url))
    with engine.connect() as con:
        assert '<= (100)' in con.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='native_reply_pacing'::regclass AND conname='native_reply_pacing_delay_bounds'"))
