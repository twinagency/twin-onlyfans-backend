"""Paired logout revocation on an owned database, including copied-token replay."""
import os
from pathlib import Path
from sqlalchemy import text
from test_onlyfans_foundation_pg import database


def execute_script(connection, script):
    with connection.connection.driver_connection.cursor() as cursor:
        cursor.execute(script)


def test_paired_session_upgrade_and_current_token_revocation(database):
    dashboard=Path(os.environ['ONLYFANS_TEST_DASHBOARD'])
    engine=database[1]
    # Roles are local test infrastructure, never fetched from a live Supabase project.
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
        assert c.scalar(text('select current_database()')).startswith('of_test_')
        c.exec_driver_sql('CREATE SCHEMA extensions; CREATE EXTENSION pgcrypto WITH SCHEMA extensions')
        for role in ('anon','authenticated','service_role'):
            if not c.scalar(text('select 1 from pg_roles where rolname=:r'),{'r':role}):
                c.exec_driver_sql('CREATE ROLE '+role+(' BYPASSRLS' if role=='service_role' else ''))
        c.exec_driver_sql('GRANT USAGE ON SCHEMA public,extensions TO service_role')
        execute_script(c,(dashboard/'deployment/dashboard-owner-schema.sql').read_text())
        upgrade=(dashboard/'deployment/dashboard-session-revocation-upgrade.sql').read_text()
        execute_script(c,upgrade)
        # A rerun of the release operation must preserve existing sessions.
        c.exec_driver_sql("SELECT public.dashboard_admin_session_create(repeat('c',64))")
        execute_script(c,upgrade)
        assert c.scalar(text("SELECT public.dashboard_owner_session_verify(repeat('c',64))"))
        execute_script(c,(dashboard/'scripts/check-owner-auth.sql').read_text())
        execute_script(c,(dashboard/'scripts/check-session-revocation.sql').read_text())
