"""Account isolation: identical fan and message ids in several accounts never share state; no singleton lookups remain."""
import hashlib
import hmac
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from conversation_engine import approval, commercial_scope as cs, commerce, journey, operator_v2, pilot_scope
from conversation_engine import scope_contract as sc, writer_context
from db.models import Message, OfConversationState
from db.onlyfans_engine_models import OfEnginePilotScope, OfFansConnection, OfFansWebhook
from db.onlyfans_models import OfRuntime
from fans_api_adapter.events import FansEventIntake
from onlyfans_runtime.store import Refused, utcnow
from test_commercial_preparation_pg import ACTOR, TENANT
from test_commercial_scope_contract import body, legacy_config
from test_commercial_scope_lifecycle_pg import OTHER, activate, launchable, scope_state, stop  # noqa: F401
from test_commercial_scope_lookup_pg import legacy_account, runtime as runtime_of
from test_commercial_scope_promote_pg import key, prepared, promote
from test_onlyfans_foundation_pg import database, seed  # noqa: F401

SERVICE = Path(__file__).resolve().parents[1]
SECRET = b'isolation-secret'


@pytest.fixture(autouse=True)
def fixture_only(monkeypatch):
    monkeypatch.setenv('TWIN_PLAN_C_FIXTURE_ONLY', '1')


def test_no_singleton_scope_lookup_remains_outside_a_short_justified_allow_list():
    """A ratchet: reading or creating 'the' scope row by the literal id 1 is how every other account became invisible."""
    patterns = [re.compile(p) for p in (r"OfEnginePilotScope\.scope_id\s*==\s*1\b", r"get\(\s*OfEnginePilotScope\s*,\s*1\s*\)",
                                         r"WHERE\s+scope_id\s*=\s*1", r"OfEnginePilotScope\(\s*scope_id\s*=\s*1\b")]
    found = {}
    for path in sorted(SERVICE.rglob('*.py')):
        relative = path.relative_to(SERVICE).as_posix()
        if relative.startswith(('tests/', 'db/migrations/', 'db/snapshots/')):
            continue
        hits = [n for n, line in enumerate(path.read_text().splitlines(), 1)
                if not line.lstrip().startswith('#') and any(p.search(line) for p in patterns)]
        if hits:
            found[relative] = len(hits)
    assert found == {
        # configure: "is the one legacy pilot slot already taken by another creator", and the legacy row's creation.
        'conversation_engine/pilot_scope.py': 2,
        # operator surface fence: does a legacy trial pilot exist anywhere (unchanged installation behaviour).
        'conversation_engine/operator_v2.py': 1,
    }


def fleet(database, count=4):
    """Several launched commercial accounts with IDENTICAL audience ids; one legacy trial pilot beside them."""
    legacy_store, legacy = legacy_account(database)
    stores, live = {}, {}
    for i in range(1, count + 1):
        store, account, cfg, promoted = launchable(database, i, n=100)
        stores[account] = store
        live[account] = activate(store, account, promoted['revision'], 600 + i)
    return legacy_store, stores, live


def close(legacy_store, stores):
    legacy_store.close()
    for store in stores.values():
        store.close()


def provider_event(cfg_provider, mid, fan, text_body='hello'):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace('+00:00', 'Z')
    return {'event': 'messages.received', 'account_id': cfg_provider, 'payload': {
        'id': mid, 'fromUser': {'id': fan}, 'text': text_body, 'createdAt': stamp, 'isSentByMe': False, 'media': [], 'price': 0}}


def signed(payload):
    raw = json.dumps(payload, separators=(',', ':')).encode()
    return raw, hmac.new(SECRET, raw, hashlib.sha256).hexdigest()


def test_identical_fan_and_message_ids_stay_inside_their_own_account_through_intake_control_and_approval(database):
    legacy_store, stores, live = fleet(database)
    engine = database[1]
    try:
        accounts = sorted(stores)
        # The SAME inbound message id and fan id arrives in every account (each from its own provider account).
        for i, account in enumerate(accounts, 1):
            raw, signature = signed(provider_event(f'acct_prep_{i}', 777, 101, f'hello from fan 101 in account {i}'))
            result = FansEventIntake(stores[account]).accept(raw, signature, SECRET, 'event-777',
                                                           expected_tenant=TENANT, expected_account=account)
            assert result.get('outcome') != 'ignored', result
        with Session(engine) as s:
            for i, account in enumerate(accounts, 1):
                rows = s.scalars(select(OfFansWebhook).where(OfFansWebhook.event_key == 'event-777', OfFansWebhook.account_id == account)).all()
                assert len(rows) == 1, account
                body_text = s.scalar(select(Message.body).where(Message.account_id == account, Message.fan_id == 101, Message.message_id == 777))
                assert body_text in (None, f'hello from fan 101 in account {i}'), account       # never another account's text
            assert s.scalar(select(func.count()).select_from(OfFansWebhook).where(OfFansWebhook.event_key == 'event-777')) == len(accounts)
        # Another account's provider id, or a fan outside the audience, is refused for that account.
        raw, signature = signed(provider_event('acct_prep_2', 778, 101))
        with pytest.raises(Refused):
            FansEventIntake(stores[accounts[0]]).accept(raw, signature, SECRET, 'event-778', expected_tenant=TENANT, expected_account=accounts[0])
        raw, signature = signed(provider_event('acct_prep_1', 779, 555))
        with pytest.raises(Refused, match='fan scope'):
            FansEventIntake(stores[accounts[0]]).accept(raw, signature, SECRET, 'event-779', expected_tenant=TENANT, expected_account=accounts[0])
        # A control action on fan 101 of the first account changes that conversation only.
        first, second = accounts[0], accounts[1]
        with Session(engine) as s:
            before = {a: s.get(OfConversationState, (a, 101)).mode for a in accounts}
            revision = {a: s.get(OfConversationState, (a, 101)).revision for a in accounts}
        stores[first].control(TENANT, first, 101, mode='paused', expected_revision=revision[first], command_key=key(700), actor=ACTOR,
                              reason='Operator pauses one fan', action='pause_automation')
        with Session(engine) as s:
            after = {a: s.get(OfConversationState, (a, 101)).mode for a in accounts}
        assert after[first] == 'paused' and all(after[a] == before[a] for a in accounts if a != first)
        with pytest.raises(Refused, match='operator permission'):
            stores[second].control(TENANT, second, 101, mode='paused', expected_revision=revision[second], command_key=key(701), actor=OTHER,
                                   reason='Not the named operator', action='pause_automation')
        # Approval queues are account-scoped and audience-scoped too.
        assert all(approval.pending(stores[a], TENANT, a) == [] for a in accounts)
    finally:
        close(legacy_store, stores)


def test_a_held_account_does_not_stop_the_others_and_stamps_are_distinct(database):
    from onlyfans_runtime.launch_operations import account_state, control_account
    legacy_store, stores, live = fleet(database)
    engine = database[1]
    try:
        accounts = sorted(stores)
        held, others = accounts[1], [a for a in accounts if a != accounts[1]]
        with Session(engine, expire_on_commit=False) as s, s.begin():
            runtime = stores[held]._runtime(s, TENANT, held)
            fingerprint = account_state(s, runtime)['fingerprint']
        control_account(stores[held], TENANT, held, action='emergency_stop', command_key=key(710), actor=ACTOR,
                        reason='Hold one account', expected_fingerprint=fingerprint)
        assert scope_state(engine, held)['status'] == 'stopped' and scope_state(engine, held)['runtime'] is False
        stamps = set()
        with Session(engine) as s:
            for account in others:
                assert scope_state(engine, account)['status'] == 'active' and scope_state(engine, account)['runtime'] is True
                row, config = pilot_scope.require(s, runtime_of(engine, stores[account], account), active=True)
                assert config['fans'] == [101, 102]
                stamps.add((account, row.scope_id, row.fingerprint, row.revision))
            with pytest.raises(Refused, match='not active'):
                pilot_scope.require(s, runtime_of(engine, stores[held], held), active=True)
            # Provider budgets are per row: reserving for one account never moves another's counter.
        assert len({s_[1] for s_ in stamps}) == len(others) and len({s_[2] for s_ in stamps}) == len(others)
        target = others[0]
        pilot_scope.reserve_provider_request(stores[target], TENANT, target, expected_revision=live[target]['revision'])
        with Session(engine) as s:
            used = {a: s.scalar(select(OfEnginePilotScope.provider_requests_used).where(OfEnginePilotScope.account_id == a)) for a in accounts}
        assert used[target] == 3 and all(used[a] == 2 for a in accounts if a != target)
        # The legacy trial pilot is untouched by any of it.
        with Session(engine) as s:
            legacy = s.get(OfEnginePilotScope, 1)
            assert (legacy.status, legacy.revision, legacy.provider_requests_used) == ('configured', 1, 0)
    finally:
        close(legacy_store, stores)


def test_profiles_and_identity_lines_are_bound_per_account_and_preparation_binds_nothing(database):
    from conversation_engine.writer_context import CreatorProfile, profile_data
    legacy_store, legacy = legacy_account(database)
    one, a1, _ = prepared(database, 1)
    two, a2, _ = prepared(database, 2)
    three, a3, _ = prepared(database, 3)
    engine = database[1]
    try:
        named = lambda name, line: profile_data(CreatorProfile(name=name, limits=('no meetups',), role_statement=line, limit_terms=('meet',)))
        promote(one, a1, n=100, creator_profile=named('First Creator', 'I am the first creator'))
        promote(two, a2, n=100, creator_profile=named('Second Creator', 'I am the second creator'))
        with Session(engine) as s:
            assert writer_context.active_profile('legacy-a', session=s).name == 'Test Creator'
            assert writer_context.active_profile(a1, session=s).name == 'First Creator'
            assert writer_context.active_profile(a2, session=s).name == 'Second Creator'
            # A preparation row binds no profile (it used to crash the profile lookup); the env source answers instead.
            assert writer_context.active_profile(a3, session=s) is None
            assert journey.identity_line_for(a1, s=s) == 'I am the first creator'
            assert journey.identity_line_for(a2, s=s) == 'I am the second creator'
            assert journey.scoped_config(s, a3) is journey.journey_config()           # preparation binds no identity line
            assert journey.scoped_config(s, a1).limit_terms == ('meet',)
    finally:
        legacy_store.close(); one.close(); two.close(); three.close()


def test_provider_history_coverage_is_strict_for_a_scoped_account_and_never_borrowed_from_another(database):
    from db.onlyfans_fans_intake_models import OfFansBackfill
    legacy_store, legacy = legacy_account(database)
    store, account, cfg, promoted = launchable(database, 1)
    plain_store, plain, _ = prepared(database, 2)
    engine = database[1]
    try:
        def run(a, marker):
            with Session(engine) as s, s.begin():
                s.add(OfFansBackfill(account_id=a, run_id=f'run-{a}', reason='operator', start_date=datetime(2015, 1, 1), status='complete',
                    pages_used=1, pages_budget=5, marker=json.dumps(marker), rows_seen=0, rows_new=0, started_at=utcnow(), updated_at=utcnow()))
        run(account, {'done_chats': ['101', '102']})
        run(plain, {'done_chats': ['101', '102']})
        with Session(engine) as s:
            assert journey.backfill_complete(s, plain, 101) is True                 # no scope of its own: lenient, as before
            assert journey.backfill_complete(s, account, 101) is False              # its own commercial scope: strict
        # Strict coverage is provable for a commercial scope through the same stopped-backfill evidence.
        stopped = stop(store, account, promoted['revision'], 720)
        guard = pilot_scope.authorize_stopped_backfill(store, TENANT, account, expected_revision=stopped['revision'], actor=ACTOR,
            reason='History catch-up', command_key=key(721), start_date=datetime(2015, 1, 1), request_limit=10, base='https://api.fansapi.com')
        assert guard.evidence['product'] is None and guard.evidence['fans'] == [101, 102]
        with Session(engine) as s:
            conn = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account))
        with Session(engine) as s, s.begin():
            row = s.get(OfFansBackfill, (account, f'run-{account}'))
            row.marker = json.dumps({'done_chats': ['101', '102'], 'exhausted_chats': ['101', '102'], 'pilot_coverage': {
                'kind': 'provider_history_end.v1', 'command': key(721), 'binding': conn.binding_id, 'epoch': conn.epoch,
                'provider_account': conn.provider_account_id}})
        with Session(engine) as s:
            assert journey.backfill_complete(s, account, 101) is True
            assert journey.backfill_complete(s, account, 103) is False
    finally:
        legacy_store.close(); store.close(); plain_store.close()


def test_operator_surface_serves_each_accounts_own_scope_and_fences_accounts_without_one(database):
    legacy_store, legacy = legacy_account(database)
    store, account, cfg, promoted = launchable(database, 1)
    prep_store, prep_account, _ = prepared(database, 2)
    engine = database[1]
    try:
        assert operator_v2._scope(store, TENANT, account)[1] is True                  # commercial account no longer NotFound
        operator_v2.require_pilot_reader(store, TENANT, account, ACTOR)
        with pytest.raises(Refused, match='operator permission'):
            operator_v2.require_pilot_reader(store, TENANT, account, OTHER)
        # An account with only preparation authority is fenced while the legacy trial pilot exists, and unknown accounts stay unknown.
        with pytest.raises(operator_v2.NotFound):
            operator_v2.require_pilot_reader(prep_store, TENANT, prep_account, ACTOR)
        with pytest.raises(operator_v2.NotFound):
            operator_v2.require_pilot_reader(store, TENANT, 'no-such-account', ACTOR)
        with pytest.raises(operator_v2.NotFound):
            operator_v2.require_pilot_reader(store, 'another-tenant', account, ACTOR)
        # Without any legacy pilot the installation behaves as before: preparation-only accounts are not fenced.
        with engine.begin() as c:
            c.execute(text("ALTER TABLE of_engine_pilot_scope DISABLE TRIGGER of_engine_pilot_scope_guard"))
            c.execute(text("DELETE FROM of_engine_pilot_scope WHERE scope_id=1"))
            c.execute(text("ALTER TABLE of_engine_pilot_scope ENABLE TRIGGER of_engine_pilot_scope_guard"))
        operator_v2.require_pilot_reader(prep_store, TENANT, prep_account, ACTOR)
    finally:
        legacy_store.close(); store.close(); prep_store.close()


def test_trial_exception_and_legacy_only_tools_never_cover_a_commercial_scope(database):
    from onlyfans_runtime import monitor_incident_recovery as recovery
    legacy_store, legacy = legacy_account(database)
    store, account, cfg, promoted = launchable(database, 1)
    engine = database[1]
    try:
        with pytest.raises(Refused, match='legacy trial pilot'):
            pilot_scope.set_trial_takeover_exception(store, TENANT, account, enabled=True, actor=ACTOR, reason='forged')
        # Even a forged start record cannot make the exception cover a commercial fan.
        start, _ = pilot_scope._trial_exception_keys('binding-' + account)
        with engine.begin() as c:
            c.execute(text("INSERT INTO of_launch_command(account_id,command_key,binding_id,kind,request_hash,actor,reason,before_json,result_json,created_at)"
                           " VALUES(:a,:k,:b,'plan_c_trial_takeover_exc','0',:actor,'forged',:before,'{}',now())"),
                      {'a': account, 'k': start, 'b': 'binding-' + account, 'actor': ACTOR, 'before': json.dumps({'fans': [101, 102]})})
        with Session(engine) as s:
            assert pilot_scope.trial_takeover_exception(s, account, 'binding-' + account, 101) is False
            assert pilot_scope.trial_exception_covers_pilot(s, runtime_of(engine, store, account)) is False
        proof = {'version': 'plan-c-monitor-resolution.v2', 'account_id': account}
        with pytest.raises(Refused, match='unsupported'):
            recovery.record_resolution(store, TENANT, account, proof=proof, actor=ACTOR, command_key=key(730))
    finally:
        legacy_store.close(); store.close()


def test_a_commercial_scope_has_no_pinned_product_in_pricing_offers_and_approval(database):
    store, account, cfg, promoted = launchable(database, 1)
    engine = database[1]
    try:
        activate(store, account, promoted['revision'], 740)
        with Session(engine) as s:
            runtime = runtime_of(engine, store, account)
            assert journey.pilot_purchase_scope(s, runtime, 101, SimpleNamespace(fan_id=101, body='send me the photo')) == ()
            from conversation_engine.engine import _price_information
            assert _price_information(s, runtime, 'how much is it?') == []
        assert commerce.prepare_pilot_offer(store, TENANT, account, 101, 1, None) is None
        parts = [{'action': 'reply', 'terms': None, 'catalog_item_id': None},
                 {'action': 'offer', 'terms': {'price_cents': 300, 'media_ids': [1], 'preview_ids': []}, 'catalog_item_id': 7}]
        config = pilot_scope.describe(store, TENANT, account)['config']
        assert approval.offer_pin_reason(config, parts[:1]) is None
        assert approval.offer_pin_reason(config, parts) == 'commercial_product_contract_unavailable'
        legacy = {'product': {'id': 7, 'price_cents': 300, 'media_ids': [1], 'preview_ids': []}}
        assert approval.offer_pin_reason(legacy, parts) is None
        assert approval.offer_pin_reason({'product': {**legacy['product'], 'price_cents': 400}}, parts) == 'pilot_product_scope_changed'
        from model_transport.pilot_draft import refuse_commercial
        with pytest.raises(Refused, match='legacy trial scope'):
            refuse_commercial(config)
        refuse_commercial(None)
    finally:
        store.close()


def test_legacy_writers_are_fenced_for_every_account_that_has_a_scope(tmp_path, monkeypatch):
    from moxy_replacement import routing
    from twin_bridge import coordination
    path = tmp_path / 'app.db'
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE of_engine_pilot_scope (scope_id integer primary key, account_id text)')
    con.executemany('INSERT INTO of_engine_pilot_scope VALUES (?,?)', [(1, 'of-9100001'), (7, 'of-9100002'), (8, 'of-9100003')])
    con.commit(); con.close()
    monkeypatch.setattr(coordination, '_database_url', lambda: f'sqlite:///{path}')
    monkeypatch.delenv(routing.PILOT_ENV, raising=False)
    for account in ('of-9100001', 'of-9100002', 'of-9100003'):
        assert routing.durable_pilot_state(account) == routing.SELECTED, account
    assert routing.durable_pilot_state('of-9100004') == routing.NOT_SELECTED
    assert routing.job_fence_reason('of-9100002', 'scheduled_send', {'send_purpose': 'manual'}) == 'pilot_canonical_authority_required'
