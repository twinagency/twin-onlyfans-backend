"""Real database index behavior: many sent cards, one uncertain delivery."""
import importlib.util
import os
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


def test_item_scope_migration_preserves_evidence_and_uncertainty(tmp_path):
    url=os.environ.get("OFFER_FIX_TEST_PG_URL") or "sqlite:///"+str(tmp_path/"scope.db")
    eng=create_engine(url)
    source=Path(__file__).parents[1]/"db/migrations/versions/20261101_0100_contextual_item_scope.py"
    spec=importlib.util.spec_from_file_location("scope_migration",source)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE of_contextual_offer(account_id TEXT,fan_id BIGINT,state TEXT)"))
        c.execute(text("CREATE UNIQUE INDEX uq_contextual_unresolved ON of_contextual_offer(account_id,fan_id) WHERE state IN ('reserved','sent','unknown')"))
        c.execute(text("INSERT INTO of_contextual_offer VALUES('test',1,'sent')"))
        with Operations.context(MigrationContext.configure(c)):
            migration.upgrade()
            c.execute(text("INSERT INTO of_contextual_offer VALUES('test',1,'sent')"))
            c.execute(text("INSERT INTO of_contextual_offer VALUES('test',1,'reserved')"))
            with pytest.raises(IntegrityError),c.begin_nested():
                c.execute(text("INSERT INTO of_contextual_offer VALUES('test',1,'unknown')"))
            with pytest.raises(RuntimeError,match="multiple confirmed cards"):
                migration.downgrade()
        assert c.scalar(text("SELECT count(*) FROM of_contextual_offer WHERE state='sent'"))==2
        assert c.scalar(text("SELECT count(*) FROM of_contextual_offer WHERE state='reserved'"))==1
    eng.dispose()
