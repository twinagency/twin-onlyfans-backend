"""Sparse pending-message discovery and a populated additive upgrade."""
import json
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database, seed
from db.models import Message
from db.onlyfans_models import OfRuntime
from db.onlyfans_content_sync_progress_models import OfContentSyncSource as Source
from db.schema import _config, check_schema, upgrade, PURCHASE_REPLY
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.content_sync import _fold_query

def messages(account, count):
    at=utcnow()
    return [dict(account_id=account,fan_id=101,message_id=100000+i,direction="out",
        body="Synthetic historical item",media_ids="[11]",media_count=1,price_cents=100,
        created_at=at,ingested_at=at,is_unsent=False) for i in range(count)]

def test_sparse_discovery_uses_pending_index_and_requeues_updates_atomically(database):
    store=seed(*database);account="acc-a"
    try:
        with Session(database[1]) as session,session.begin():
            session.bulk_insert_mappings(Message,messages(account,20000))
            session.execute(text("""UPDATE of_content_sync_source SET folded_at=now(),
                folded_message_media_json='[11]',folded_message_price_cents=100,
                folded_message_unsent=false,fold_pending=false"""))
            session.execute(text("UPDATE messages SET price_cents=200 WHERE message_id=100001"))
        with database[1].begin() as conn:
            conn.execute(text("ANALYZE messages"))
            conn.execute(text("ANALYZE of_content_sync_source"))
        with Session(database[1]) as session:
            runtime=session.get(OfRuntime,account)
            query=_fold_query(runtime,None,None).order_by(Message.message_id,Message.fan_id).limit(1000)
            result=session.execute(query).all()
            assert [row.message_id for row in result]==[100001]
            compiled=query.compile(dialect=database[1].dialect,compile_kwargs={"render_postcompile":True})
            plan=session.connection().exec_driver_sql("EXPLAIN (ANALYZE, FORMAT JSON) "+str(compiled),compiled.params).scalar()
            assert "ix_of_content_sync_source_fold_pending" in json.dumps(plan), "sparse discovery scanned historical receipts"
            assert plan[0]["Execution Time"]<500, plan
        with Session(database[1]) as session,session.begin():
            source=session.get(Source,(account,"binding-"+account,"message",100001))
            source.fold_pending=False
        with Session(database[1]) as session:
            session.execute(text("UPDATE messages SET is_unsent=true WHERE message_id=100001"))
            assert session.get(Source,(account,"binding-"+account,"message",100001)).fold_pending
            session.rollback()
        with Session(database[1]) as session:
            assert not session.get(Source,(account,"binding-"+account,"message",100001)).fold_pending
        with Session(database[1]) as session,session.begin():
            session.execute(text("UPDATE messages SET ingested_at=now() WHERE message_id=100001"))
        with Session(database[1]) as session:
            runtime=session.get(OfRuntime,account)
            dirty=session.execute(_fold_query(runtime,None,None)).all()
            assert [row.message_id for row in dirty]==[100001], "unchanged reingestion must be drainable"
    finally:store.close()

def test_populated_94_upgrade_queues_only_unfolded_or_changed_sources(database):
    store=seed(*database);account="acc-a"
    store.close()
    with Session(database[1]) as session,session.begin():
        session.bulk_insert_mappings(Message,messages(account,3))
        session.execute(text("""UPDATE of_content_sync_source SET folded_at=now(),
            folded_message_media_json='[11]',folded_message_price_cents=100,
            folded_message_unsent=false,fold_pending=false"""))
    with database[1].begin() as conn:
        # Reconstruct precisely the old shape in this disposable database.
        from onlyfans_upgrade_fixture import restore_pre95_content_vault_shape
        restore_pre95_content_vault_shape(conn)
        command.stamp(_config(conn),PURCHASE_REPLY)
        conn.exec_driver_sql("DELETE FROM of_content_sync_source WHERE source_id=100001")
        conn.exec_driver_sql("UPDATE messages SET price_cents=0 WHERE message_id=100002")
        before=conn.execute(text("SELECT enabled,binding_id FROM of_runtime")).all()
        upgrade(conn)
        check_schema(conn)
        assert conn.execute(text("SELECT enabled,binding_id FROM of_runtime")).all()==before
        pending=dict(conn.execute(text("SELECT source_id,fold_pending FROM of_content_sync_source")).all())
        assert pending=={100000:False,100001:True,100002:True}
        assert conn.scalar(text("SELECT count(*) FROM transactions"))==0
        assert conn.scalar(text("SELECT count(*) FROM of_text_intent"))==0
        conn.exec_driver_sql("UPDATE messages SET price_cents=300 WHERE message_id=100000")
        assert conn.scalar(text("SELECT fold_pending FROM of_content_sync_source WHERE source_id=100000"))
