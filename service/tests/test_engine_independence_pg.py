"""Independent replies preserve the original uncertain action and its budget."""
from datetime import datetime, timezone
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, unthrottle
from test_engine_simulated_journey_pg import incoming
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineIndependenceProof, OfEngineIndependentAdmission
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.store import Refused, Store


def setup(database, outcomes=('unknown','accepted'), first_body='First answer.'):
    store, authority = configured(database, mode='automated', limit=8)
    FansWebhookAdapter(store).bind_simulated(TENANT, ACCOUNT, 'acct_independent', 'creator-fixture')
    model = SyntheticModel({'first':SyntheticProposal(first_body),
        'What is 4 + 5?':SyntheticProposal('4 + 5 = 9.'),
        'What is 2 + 7?':SyntheticProposal('2 + 7 = 9.'),
        'unrelated':SyntheticProposal('Your earlier action was cancelled.'),
        'What is 1 + 8?':SyntheticProposal('Your earlier action was cancelled.')})
    engine = EngineService(store, model)
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'first')
    first_decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    first = engine.queue(TENANT, ACCOUNT, first_decision)
    provider = SyntheticFansProvider(outcomes, message_id=9601)
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    assert authority.dispatch(TENANT, ACCOUNT, first, wire) == 'unknown'
    return store, authority, engine, provider, wire, first


def arithmetic(engine, store, mid=2, question='What is 4 + 5?'):
    incoming(store, mid, question)
    decision = engine.plan(TENANT, ACCOUNT, FAN, mid)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    return decision, intent


def test_proven_answer_sends_without_replacing_unknown_action(database):
    store, authority, engine, provider, wire, first = setup(database)
    decision, intent = arithmetic(engine, store)
    unthrottle(database[1])
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'accepted'
    with Session(database[1]) as s:
        prior = s.get(OfTextIntent, (ACCOUNT, first))
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        proof = s.get(OfEngineIndependenceProof, (ACCOUNT, decision))
        slot = s.scalar(select(OfEngineIndependentAdmission).where(OfEngineIndependentAdmission.intent_id == intent))
        assert prior.status == 'unknown' and state.unresolved_attempt_id == prior.attempt_id
        assert policy.active_intent_id == first and policy.used_units == 2
        assert proof.anchor_intent_id == first and proof.kind == 'arithmetic.v1'
        assert slot.status == 'settled'
        assert s.get(Message, (ACCOUNT, FAN, 9601)).body == '4 + 5 = 9.'
    assert len(provider.calls) == 2
    store.close()


@pytest.mark.parametrize('question', ['unrelated', 'What is 1 + 8?'])
def test_different_topic_or_model_assertion_never_proves_independence(database, question):
    store, authority, engine, provider, wire, first = setup(database)
    incoming(store, 2, question)
    decision = engine.plan(TENANT, ACCOUNT, FAN, 2)
    with Session(database[1]) as s:
        assert s.get(OfEngineIndependenceProof, (ACCOUNT, decision)) is None
    with pytest.raises(Refused):
        engine.queue(TENANT, ACCOUNT, decision)
    assert len(provider.calls) == 1
    store.close()


def test_two_unknowns_survive_anchor_reconciliation_and_restart(database):
    store, authority, engine, provider, wire, first = setup(database, ('accepted_lost','unknown'))
    decision, intent = arithmetic(engine, store)
    unthrottle(database[1])
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'unknown'
    with Session(database[1]) as s:
        a, b = s.get(OfTextIntent, (ACCOUNT, first)), s.get(OfTextIntent, (ACCOUNT, intent))
        assert a.status == b.status == 'unknown'
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id == a.attempt_id
        assert s.get(OfEngineIndependentAdmission, (ACCOUNT, b.attempt_id)).status == 'unknown'
    assert wire.reconcile_accepted(first) == 'accepted'
    with Session(database[1]) as s:
        a, b = s.get(OfTextIntent, (ACCOUNT, first)), s.get(OfTextIntent, (ACCOUNT, intent))
        state, policy = s.get(OfConversationState, (ACCOUNT, FAN)), s.get(OfDeliveryPolicy, ACCOUNT)
        assert a.status == 'accepted' and b.status == 'unknown'
        assert state.unresolved_attempt_id == b.attempt_id and policy.active_intent_id == intent
        assert s.get(OfEngineIndependentAdmission, (ACCOUNT, b.attempt_id)).status == 'unknown'
    store.close()
    restarted = Store(database[0])
    from onlyfans_runtime.text_delivery import TextAuthority
    assert TextAuthority(restarted).dispatch(TENANT, ACCOUNT, intent,
        FansApiSyntheticTransport(restarted, TENANT, ACCOUNT, provider)) == 'unknown'
    assert len(provider.calls) == 2
    restarted.close()


def test_new_input_cancels_unsent_independent_reply_but_keeps_unknown(database):
    store, authority, engine, provider, wire, first = setup(database)
    decision, intent = arithmetic(engine, store)
    unthrottle(database[1])
    grant = authority.admit(TENANT, ACCOUNT, intent)
    incoming(store, 3, 'What is 2 + 7?')
    assert authority.consume_dispatch(TENANT, grant) is None
    with Session(database[1]) as s:
        old, newer = s.get(OfTextIntent, (ACCOUNT, first)), s.get(OfTextIntent, (ACCOUNT, intent))
        assert old.status == 'unknown' and newer.status == 'cancelled'
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id == old.attempt_id
        assert s.get(OfEngineIndependentAdmission, (ACCOUNT, grant.attempt_id)).status == 'settled'
    assert len(provider.calls) == 1
    store.close()


def test_proof_and_admission_cannot_be_erased_by_downgrade(database):
    store, authority, engine, provider, wire, first = setup(database)
    decision, intent = arithmetic(engine, store)
    unthrottle(database[1])
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'accepted'
    with pytest.raises(DBAPIError, match='independence proof is append-only'):
        with database[1].begin() as c:
            c.execute(text('DELETE FROM of_engine_independence_proof WHERE decision_id=:id'), {'id':decision})
    from alembic import command
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import importlib
    from db.schema import _config, check_schema
    migration = importlib.import_module('db.migrations.versions.20261107_0200_independent_replies')
    with database[1].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            with pytest.raises(RuntimeError, match='Preserve independent action evidence'):
                migration.downgrade()
    with pytest.raises(RuntimeError, match='Preserve .* evidence'):
        with database[1].begin() as c:
            command.downgrade(_config(c), '0113_conversation_engine')
    with database[1].connect() as c:
        check_schema(c)
    store.close()


def test_schema_rejects_wrong_independent_active_predicate_and_missing_new_guards(database):
    from db.schema import check_schema, SchemaMismatch
    from sqlalchemy import text
    with database[1].connect() as connection:
        transaction = connection.begin()
        connection.execute(text('DROP INDEX uq_engine_independent_active'))
        connection.execute(text("CREATE UNIQUE INDEX uq_engine_independent_active ON of_engine_independent_admission(account_id) WHERE status = 'settled'"))
        with pytest.raises(SchemaMismatch, match='incompatible predicate'):
            check_schema(connection)
        transaction.rollback()
    guards = [('of_engine_independence_proof', 'of_engine_independence_proof_immutable'),
              ('of_engine_independent_admission', 'of_engine_independent_admission_guard'),
              ('of_engine_memory_binding', 'of_engine_memory_binding_immutable'),
              ('of_engine_catalogue_alias', 'of_engine_catalogue_alias_immutable'),
              ('of_engine_operator_command', 'of_engine_operator_command_immutable')]
    for table, trigger in guards:
        with database[1].connect() as connection:
            transaction = connection.begin()
            connection.execute(text(f'DROP TRIGGER {trigger} ON {table}'))
            with pytest.raises(SchemaMismatch):
                check_schema(connection)
            transaction.rollback()
    with database[1].connect() as connection:
        check_schema(connection)


@pytest.mark.parametrize('pending', ['4 + 5 = 9.', '4 + 5 = 8.', 'The answer to your question is ten.'])
def test_pending_duplicate_or_ambiguous_claim_cannot_be_bypassed(database, pending):
    store, authority, engine, provider, wire, first = setup(database, first_body=pending)
    incoming(store, 2, 'What is 4 + 5?')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 2)
    with Session(database[1]) as s:
        assert s.get(OfEngineIndependenceProof, (ACCOUNT, decision)) is None
    with pytest.raises(Refused):
        engine.queue(TENANT, ACCOUNT, decision)
    assert len(provider.calls) == 1
    store.close()


def test_distinct_proven_arithmetic_can_follow_uncertain_arithmetic(database):
    store, authority, engine, provider, wire, first = setup(database, first_body='1 + 2 = 3.')
    decision, intent = arithmetic(engine, store)
    unthrottle(database[1])
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'accepted'
    assert len(provider.calls) == 2
    store.close()
