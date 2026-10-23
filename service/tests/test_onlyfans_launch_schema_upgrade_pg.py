"""Final additive migration preserves populated combined-launch evidence."""
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed
from test_onlyfans_offer_import_upgrade_pg import historical_schema
from test_onlyfans_text_delivery_pg import ACCOUNT
from db.models import AccountAiConfig, Message
from db.onlyfans_models import OfRuntime
from db.onlyfans_offer_import_models import OfContentSyncAccount
from db.schema import LAUNCH, HEAD, check_schema, upgrade
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory


def test_populated_combined_launch_upgrades_once_and_retains_every_old_column(database, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    store = seed(*database)
    with Session(database[1]) as session, session.begin():
        session.add(AccountAiConfig(account_id=ACCOUNT, persona="Reviewed synthetic tone", daily_cost_cap_cents=123))
        session.add(Message(account_id=ACCOUNT, fan_id=101, message_id=1, direction="in",
            body="I live in Perth and I work as a chef.", created_at=utcnow()))
        session.add(OfContentSyncAccount(account_id=ACCOUNT,
            binding_id=session.get(OfRuntime, ACCOUNT).binding_id,
            enabled=False, status="off", revision=1, actor="op-session:" + "a" * 64,
            command_key="b" * 64, covered_through_message_id=1,
            reconcile_after_item_id=77, backlog=0, failures=0,
            created_at=utcnow(), updated_at=utcnow()))
    HistoricalMemory(store).process("tenant-a", ACCOUNT, 101)
    store.close()
    with database[1].begin() as connection:
        historical = historical_schema(connection, LAUNCH)
        old = {table.name: [dict(row) for row in connection.execute(table.select()).mappings()]
            for table in historical.sorted_tables if table.name != "alembic_version"}
        assert old["of_history_memory_receipt"], "populated memory proof must survive"
        assert old["of_content_sync_account"][0]["reconcile_after_item_id"] == 77
        upgrade(connection)
        upgrade(connection)
        check_schema(connection)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        for name, before in old.items():
            table = historical.tables[name]
            assert [dict(row) for row in connection.execute(table.select()).mappings()] == before, name
        assert connection.scalar(text("SELECT reconcile_priority_after_item_id FROM of_content_sync_account")) is None
        assert "ix_messages_account_ingested" in {index["name"] for index in inspect(connection).get_indexes("messages")}
        for name in ("of_launch_command", "of_ledger_resolution", "of_content_sync_occurrence",
                "of_content_sync_source", "of_content_post_progress", "of_provider_read_budget", "of_launch_worker_health",
                "of_ledger_retry_command"):
            assert connection.scalar(text(f'SELECT count(*) FROM "{name}"')) == 0
