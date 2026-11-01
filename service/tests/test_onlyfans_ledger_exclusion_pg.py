"""Audited per-transaction ledger exclusion: the account stops being held, the unlinked fan never does."""
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, TENANT
from test_onlyfans_ledger_pg import parsed, record, transaction
from db.models import Fan, Transaction
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_ledger_models import OfLedgerExclusion
from onlyfans_runtime import media_delivery, native_sales
from onlyfans_runtime.commerce import held
from onlyfans_runtime.launch_operations import exclude_unresolved_ledger, ledger_status
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow

ACTOR = "op-session:" + "a" * 64
KEY = "e" * 64
FAN, OTHER = 101, 102
REASON = "Provider gave no message identity for this purchase; operator accepts it stays unlinked"


def purchase(**changes):
    """Occurs after the current binding was created, so it counts against the account."""
    return parsed(transaction(createdAt=utcnow().isoformat() + "Z", **changes))


def seed_unlinked(store, account, **changes):
    assert record(store, account, purchase(**changes)) == "applied"
    with Session(store.engine) as s:
        return s.scalar(select(Transaction.id))


def fingerprint(store, account):
    return ledger_status(store, TENANT, account)["fingerprint"]


def exclude(store, account, tx_id, **changes):
    args = dict(transaction_id=tx_id, command_key=KEY, actor=ACTOR, reason=REASON,
        expected_fingerprint=fingerprint(store, account))
    args.update(changes)
    return exclude_unresolved_ledger(store, TENANT, account, **args)


def test_account_is_held_before_exclusion_and_only_the_unlinked_fan_after(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        tx_id = seed_unlinked(store, account)
        with Session(store.engine) as s:
            assert held(s, account) and held(s, account, FAN) and held(s, account, OTHER)
        before = ledger_status(store, TENANT, account)
        assert before["unresolvedTransactions"] == 1 and before["excludedTransactions"] == 0
        assert before["commerceHold"] is True
        result = exclude(store, account, tx_id)
        assert result["excluded"] is True and result["blocker"] == "provider_message_identity_missing"
        with Session(store.engine) as s:
            assert not held(s, account), "no other clause holds the account"
            assert not held(s, account, OTHER), "another fan is sellable"
            assert held(s, account, FAN), "the unlinked fan stays blocked"
            tx = s.get(Transaction, tx_id)
            assert tx.message_id is None and tx.status == "cleared" and tx.source == "ledger"
            assert s.get(Fan, (account, FAN)).lifetime_spend_cents == 1000, "money is untouched"
            assert s.scalar(select(func.count()).select_from(Transaction)) == 1
            row = s.scalar(select(OfLedgerExclusion))
            assert (row.account_id, row.fan_id, row.transaction_id, row.actor, row.reason, row.command_key) == (
                account, FAN, tx_id, ACTOR, REASON, KEY)
            audit = s.get(OfLaunchCommand, (account, KEY))
            assert audit.kind == "ledger_exclusion" and audit.actor == ACTOR and audit.reason == REASON
        state = ledger_status(store, TENANT, account)
        assert state["unresolvedTransactions"] == 0 and state["excludedTransactions"] == 1
        assert state["commerceHold"] is False
        from onlyfans_runtime.launch_ledger.dashboard import read_ledger
        with Session(store.engine) as s, s.begin():
            dash = read_ledger(s, TENANT, account)
        # The dashboard keeps showing the purchase (fixed field set); only the account-wide hold is lifted.
        assert dash["unlinkedPaidCount"] == 1 and dash["commerceHeld"] is False and dash["requiresInvestigation"] is True
    finally:
        store.close()


def test_exclusion_is_refused_unless_the_blocker_is_exactly_missing_message_identity(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        # The provider did report a message id; that purchase belongs to resolve_ledger, never to an exclusion.
        tx_id = seed_unlinked(store, account, messageId=123)
        with pytest.raises(Refused, match="provider_message_identity_missing"):
            exclude(store, account, tx_id)
        with pytest.raises(Refused, match="unavailable"):
            exclude(store, account, tx_id + 999)
        for bad in (True, "1", 0, -1, None):
            with pytest.raises(Refused, match="invalid"):
                exclude(store, account, bad)
        with Session(store.engine) as s:
            assert s.scalar(select(func.count()).select_from(OfLedgerExclusion)) == 0
            assert s.scalar(select(func.count()).select_from(OfLaunchCommand)) == 0
            assert held(s, account), "still held"
    finally:
        store.close()


def test_exclusion_is_refused_for_a_linked_unpaid_or_historical_transaction(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        tx_id = seed_unlinked(store, account)
        with Session(store.engine) as s, s.begin():
            s.get(Transaction, tx_id).status = "refunded"
        with pytest.raises(Refused, match="not an unresolved paid purchase"):
            exclude(store, account, tx_id)
        with Session(store.engine) as s, s.begin():
            tx = s.get(Transaction, tx_id)
            tx.status, tx.message_id = "cleared", 55
        with pytest.raises(Refused, match="not an unresolved paid purchase"):
            exclude(store, account, tx_id)
    finally:
        store.close()


def test_exclusion_is_idempotent_and_a_conflicting_replay_is_refused(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        tx_id = seed_unlinked(store, account)
        args = dict(transaction_id=tx_id, command_key=KEY, actor=ACTOR, reason=REASON,
            expected_fingerprint=fingerprint(store, account))
        first = exclude_unresolved_ledger(store, TENANT, account, **args)
        assert exclude_unresolved_ledger(store, TENANT, account, **args) == first
        for change in ({"reason": "A different reason"}, {"actor": "op-session:" + "b" * 64},
                       {"expected_fingerprint": "0" * 64}):
            with pytest.raises(Refused, match="conflict"):
                exclude_unresolved_ledger(store, TENANT, account, **{**args, **change})
        # The same transaction under a new command key is refused, never double-recorded.
        with pytest.raises(Refused, match="already excluded"):
            exclude(store, account, tx_id, command_key="f" * 64)
        with Session(store.engine) as s:
            assert s.scalar(select(func.count()).select_from(OfLedgerExclusion)) == 1
            assert s.scalar(select(func.count()).select_from(OfLaunchCommand)) == 1
        # Cross-tenant callers cannot reach it.
        with pytest.raises(Refused):
            exclude_unresolved_ledger(store, "other-tenant", account, **{**args, "command_key": "d" * 64})
    finally:
        store.close()


def test_database_rejects_duplicate_or_blank_exclusions(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        tx_id = seed_unlinked(store, account)
        exclude(store, account, tx_id)
        with Session(store.engine) as s:
            row = s.scalar(select(OfLedgerExclusion))
            fields = dict(account_id=account, binding_id=row.binding_id, fan_id=FAN, transaction_id=tx_id,
                blocker=row.blocker, reason="r", actor="a", command_key="9" * 64, evidence_hash="8" * 64,
                created_at=utcnow())
        for change in ({}, {"transaction_id": tx_id, "command_key": "7" * 64}, {"command_key": KEY},
                       {"reason": "  "}, {"actor": ""}):
            with Session(store.engine) as s, pytest.raises(IntegrityError):
                s.add(OfLedgerExclusion(**{**fields, **change}))
                s.commit()
    finally:
        store.close()


def test_exclusion_does_not_hide_other_account_holds(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        tx_id = seed_unlinked(store, account)
        exclude(store, account, tx_id)
        # A second unlinked purchase from another fan still holds the whole account.
        second = purchase(id="native-tx-2", user={"id": OTHER})
        assert record(store, account, second) == "applied"
        with Session(store.engine) as s:
            assert held(s, account) and held(s, account, FAN)
        # And a conflicting observation of the excluded purchase still holds it.
        assert record(store, account, {**purchase(), "amount_cents": 1100}) == "conflict"
        with Session(store.engine) as s:
            assert held(s, account)
    finally:
        store.close()


def test_native_sales_and_media_delivery_refuse_only_the_unlinked_fan(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        tx_id = seed_unlinked(store, account)
        with Session(store.engine) as s, s.begin():
            s.add(OfDeliveryPolicy(account_id=account, transport_kind="synthetic", media_enabled=True))
        snapshot = {"media_ids": [1], "preview_ids": []}
        with Session(store.engine) as s:
            for fan in (FAN, OTHER):
                assert native_sales.eligible(s, account, fan, utcnow()) == (False, "commerce_evidence_conflict")
        exclude(store, account, tx_id)
        with Session(store.engine) as s:
            assert native_sales.eligible(s, account, FAN, utcnow()) == (False, "commerce_evidence_conflict")
            assert native_sales.eligible(s, account, OTHER, utcnow())[1] != "commerce_evidence_conflict"
            with pytest.raises(Refused, match="commerce evidence needs review"):
                media_delivery.assert_unowned(s, account, FAN, snapshot)
            media_delivery.assert_unowned(s, account, OTHER, snapshot)
    finally:
        store.close()


def test_migration_downgrade_drops_only_the_exclusion_table_and_upgrade_restores_it(database):
    from alembic import command
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from importlib.util import spec_from_file_location, module_from_spec
    from pathlib import Path
    from db.schema import HEAD, _config, check_schema
    url, engine = database
    assert HEAD == "0107_active_selling_quote"
    with engine.begin() as c:
        # Later manual-media releases deliberately prohibit a whole-chain
        # rollback below 0100. Prove that guard, without bypassing it.
        with pytest.raises(RuntimeError, match="Preserve free-media"):
            with c.begin_nested():
                command.downgrade(_config(c), "0097_release_controls")
        check_schema(c)
        # Exercise 0098's own reversible table operation in isolation. It must
        # not erase financial scans, command history, or contextual evidence.
        path = Path(__file__).resolve().parents[1] / "db/migrations/versions/20261029_0100_ledger_exclusion.py"
        spec = spec_from_file_location("exclusion_migration_fixture", path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(c)):
            migration.downgrade()
            assert not c.dialect.has_table(c, "of_ledger_exclusion")
            assert c.dialect.has_table(c, "of_ledger_scan") and c.dialect.has_table(c, "of_launch_command")
            assert c.dialect.has_table(c, "of_contextual_offer")
            migration.upgrade()
        check_schema(c)
        assert c.dialect.has_table(c, "of_ledger_exclusion")


def test_postsale_guard_reads_the_same_unlinked_fans_on_postgresql_even_when_excluded(database, monkeypatch):
    from native_pilot import postsale
    from twin_bridge.store import readonly
    store, account, _ = verified(database, monkeypatch)
    try:
        tx_id = seed_unlinked(store, account)
        with readonly(database[0]) as con:
            assert postsale.unlinked_paid_fans(con, account) == {FAN}
            assert postsale.unlinked_paid_fans(con, "another-account") == set()
        exclude(store, account, tx_id)
        with readonly(database[0]) as con:
            assert postsale.unlinked_paid_fans(con, account) == {FAN}, "an exclusion never releases the fan's own card"
    finally:
        store.close()
