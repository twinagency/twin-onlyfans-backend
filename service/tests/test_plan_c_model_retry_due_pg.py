"""R0: a released model refusal must not strand a fan's request (Plan C pilot, schema 0129b).

Every test drives the REAL scheduler: SupervisedPilot.tick() -> worker.turn() -> engine.plan() -> ModelRuntime.propose().
The provider is an httpx.MockTransport feeding the real OpenRouterClient; FansAPI is the fixture MockFans. Synthetic
data only. Only the model layer's clock (runtime + ledger) is advanced; the scheduler compares due times with that
same ledger clock.
"""
import json
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.models import Message
from db.onlyfans_engine_models import OfEngineModelCall, OfEngineRequest
from model_transport.openrouter import _retry_after
from model_transport.pilot_supervised import build_supervised_pilot
from plan_c_worker import _work_counts, create_app
from test_engine_real_worker_pg import FAN2, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft, signed  # noqa: F401
from test_plan_c_events_stage3_pg import Rig
from test_plan_c_text_activation_pg import ACTOR, key, stopped_text  # noqa: F401

SECRET_IN_BODY = 'sk-or-v1-0123456789abcdef0123456789abcdef'


def ok_response(request):
    return httpx.Response(200, json={'model': 'm/primary', 'choices': [{'message': {'content': json.dumps({'body': 'hey you'})},
        'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 5, 'cost': 0.0001},
        'openrouter_metadata': {'endpoints': {'available': [{'provider': 'Fixture Provider', 'selected': True}]}}})


class Provider:
    """Scripted provider: 'ok' | int status | (int status, headers) | 'connect' | 'timeout'. Exhausted script = 'ok'."""
    def __init__(self, script=()):
        self.script, self.calls, self.requests, self.lock = list(script), [], [], threading.Lock()
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        if request.method == 'POST' and request.url.path.endswith('/chat/completions'):
            with self.lock:
                step = self.script.pop(0) if self.script else 'ok'
                self.calls.append(step if not isinstance(step, tuple) else step[0])
                self.requests.append(json.loads(request.content))
            if step == 'ok':
                return ok_response(request)
            if step in ('invalid', 'multipart', 'identity'):
                response = json.loads(ok_response(request).content)
                if step == 'identity':
                    response['model'] = 'm/unverified'
                else:
                    response['choices'][0]['message']['content'] = json.dumps(
                        {'body': 'it is only $5 for the set'} if step == 'invalid' else {'body': 'hey you', 'parts': ['how was your day', 'tell me more']})
                return httpx.Response(200, json=response)
            if step == 'connect':
                raise httpx.ConnectError('synthetic connect failure', request=request)
            if step == 'timeout':
                raise httpx.ReadTimeout('synthetic read timeout', request=request)
            status, headers = step if isinstance(step, tuple) else (step, {})
            return httpx.Response(status, headers={'x-request-id': 'req-synthetic-1', **headers}, json={'error': {
                'code': status, 'message': f'synthetic provider error key={SECRET_IN_BODY}',
                'metadata': {'provider_name': 'Fixture Upstream', 'raw': 'PROMPT TEXT MUST NOT BE STORED'}}})
        return ok_response(request)


class World:
    def __init__(self, stopped, script):
        self.rig = Rig(stopped)
        self.provider = Provider(script)
        self.rig.client.__exit__(None, None, None)
        self.w = self.rig.w
        self.offset = timedelta(0)
        from onlyfans_runtime.store import utcnow
        self.clock = lambda: utcnow() + self.offset
        self.pilot = self.new_pilot()
        self.rig.pilot = self.pilot
        self.rig.client = TestClient(create_app(self.pilot, run_loop=False))
        self.rig.client.__enter__()

    def new_pilot(self):
        """A separate worker process view over the same database (restart / second worker)."""
        pilot = build_supervised_pilot(self.w.store, self.rig.env, model_transport=self.provider.transport,
                                       fans_transport=self.w.fans.transport)
        pilot.worker.engine.model._clock = self.clock
        pilot.worker.engine.model.ledger.clock = self.clock
        return pilot

    def close(self):
        self.rig.close()

    def advance(self, seconds):
        self.offset = timedelta(seconds=seconds)

    def ticks(self, n, pilot=None):
        for _ in range(n):
            (pilot or self.pilot).tick()

    def calls(self):
        with Session(self.w.database[1]) as s:
            return list(s.scalars(select(OfEngineModelCall).where(OfEngineModelCall.account_id == ACCOUNT)
                .order_by(OfEngineModelCall.created_at, OfEngineModelCall.seq)))

    def request(self, source):
        with Session(self.w.database[1]) as s:
            r = s.scalar(select(OfEngineRequest).where(OfEngineRequest.account_id == ACCOUNT,
                                                      OfEngineRequest.source_message_id == source))
            return None if r is None else (r.status, r.next_step, r.handover_target)

    def out_messages(self):
        with Session(self.w.database[1]) as s:
            return s.scalar(select(func.count()).select_from(Message).where(Message.direction == 'out'))


@pytest.fixture
def make_world(stopped_text):
    made = []

    def build(script):
        made.append(World(stopped_text, script))
        return made[-1]
    yield build
    for w in made:
        w.close()


def post(world, mid, fan=FAN, text_='hello, how was your day?'):
    world.rig.post(inbound(mid, text_, fan=fan), key=f'r0-{mid}')


def test_429_recovers_after_due_time_without_another_fan_message(make_world):
    world = make_world([429])
    post(world, 91001)
    world.ticks(1)
    [row] = world.calls()
    assert (row.status, row.outcome, row.error_class) == ('released', 'http_429', 'http_429')
    assert row.retry_not_before is not None and timedelta(seconds=29) <= row.retry_not_before - row.updated_at <= timedelta(seconds=31)
    assert world.request(91001) == ('open', 'prepare_reply', None)
    assert _work_counts(world.pilot)['modelRetryDelayed'] == 1
    world.advance(15)
    world.ticks(3)
    assert world.provider.calls == [429], 'never before the due time'
    world.advance(31)
    world.ticks(3)
    assert world.provider.calls == [429, 'ok']
    assert world.request(91001)[0] == 'completed'
    assert len(world.w.fans.posts) == 1 and world.out_messages() == 1
    world.advance(600)
    world.ticks(3)
    assert world.provider.calls == [429, 'ok'] and len(world.w.fans.posts) == 1, 'no duplicate late answer'


def test_failure_evidence_is_bounded_and_sanitized(make_world):
    world = make_world([(429, {'Retry-After': '45', 'X-RateLimit-Remaining': '0'})])
    post(world, 91002)
    world.ticks(1)
    [row] = world.calls()
    detail = json.loads(row.failure_detail_json)
    assert detail['status'] == 429 and detail['retryAfterSeconds'] == '45' and detail['rateLimitRemaining'] == '0'
    assert detail['requestId'] == 'req-synthetic-1' and detail['providerName'] == 'Fixture Upstream'
    assert detail['errorCode'] == 429
    raw = row.failure_detail_json
    assert SECRET_IN_BODY not in raw and 'sk-or' not in raw and 'PROMPT TEXT' not in raw and 'prose omitted' in raw
    assert len(raw.encode()) <= 2000
    assert timedelta(seconds=44) <= row.retry_not_before - row.updated_at <= timedelta(seconds=46)


def test_retry_after_longer_than_cooldown_is_honoured(make_world):
    world = make_world([(429, {'Retry-After': '120'})])
    post(world, 91003)
    world.ticks(1)
    for seconds in (31, 90, 118):
        world.advance(seconds)
        world.ticks(2)
        assert world.provider.calls == [429], f'retried early at +{seconds}s'
    world.advance(122)
    world.ticks(3)
    assert world.provider.calls == [429, 'ok'] and world.request(91003)[0] == 'completed'


def test_retry_after_http_date_and_bounds():
    now = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)
    assert _retry_after('Wed, 11 Nov 2026 08:01:30 GMT', now=now) == 90
    assert _retry_after('Wed, 11 Nov 2026 07:59:00 GMT', now=now) == 0
    assert _retry_after('99999') == 99999 and _retry_after('7') == 7
    assert _retry_after('soon') is None and _retry_after(None) is None
    assert _retry_after('1' * 100) == 10**64 and _retry_after('9' * 10000) == 10**64
    assert _retry_after('0' * 100 + '7') == 7 and _retry_after('0' * 100) == 0


def test_restart_before_due_retries_exactly_once(make_world):
    world = make_world([429])
    post(world, 91004)
    world.ticks(1)
    restarted = world.new_pilot()  # a new process: nothing in memory, only the durable ledger
    world.advance(10)
    world.ticks(2, restarted)
    assert world.provider.calls == [429]
    world.advance(31)
    world.ticks(3, restarted)
    assert world.provider.calls == [429, 'ok'] and len(world.w.fans.posts) == 1


def test_two_concurrent_workers_retry_once_and_send_once(make_world):
    world = make_world([429])
    post(world, 91005)
    world.ticks(1)
    other = world.new_pilot()
    world.advance(31)
    errors = []

    def run(pilot):
        try:
            for _ in range(3):
                pilot.tick()
        except Exception as exc:  # recorded, not hidden
            errors.append(type(exc).__name__)
    threads = [threading.Thread(target=run, args=(p,)) for p in (world.pilot, other)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    world.ticks(2)
    assert world.provider.calls == [429, 'ok'], (world.provider.calls, errors)
    assert len(world.w.fans.posts) == 1 and world.out_messages() == 1
    assert world.request(91005)[0] == 'completed'


def test_second_429_hands_over_instead_of_looping(make_world):
    world = make_world([429, 429])
    post(world, 91006)
    world.ticks(1)
    world.advance(31)
    world.ticks(2)
    assert world.provider.calls == [429, 429]
    for seconds in (62, 300, 3600):
        world.advance(seconds)
        world.ticks(2)
    assert world.provider.calls == [429, 429], 'attempt limit respected'
    assert world.request(91006) == ('handover_pending', 'await_handover_acceptance', 'operator')
    assert not world.w.fans.posts


@pytest.mark.parametrize('status', [400, 401, 403])
def test_non_retryable_refusal_goes_to_a_human_immediately(make_world, status):
    world = make_world([status])
    post(world, 91007)
    world.ticks(1)
    assert world.provider.calls == [status]
    [row] = world.calls()
    assert row.status == 'released' and row.retry_not_before is None
    world.advance(600)
    world.ticks(3)
    assert world.provider.calls == [status], 'never blindly retried'
    assert world.request(91007) == ('handover_pending', 'await_handover_acceptance', 'operator')
    assert not world.w.fans.posts


@pytest.mark.parametrize('status', [503, 408])
def test_other_availability_refusals_recover(make_world, status):
    world = make_world([status])
    post(world, 91008)
    world.ticks(1)
    world.advance(31)
    world.ticks(3)
    assert world.provider.calls == [status, 'ok'] and world.request(91008)[0] == 'completed'
    assert len(world.w.fans.posts) == 1 and world.out_messages() == 1


def test_not_sent_connect_failure_recovers(make_world):
    world = make_world(['connect'])
    post(world, 91009)
    world.ticks(1)
    [row] = world.calls()
    assert (row.status, row.outcome) == ('released', 'not_sent') and row.retry_not_before is not None
    world.advance(31)
    world.ticks(3)
    assert world.provider.calls == ['connect', 'ok'] and world.request(91009)[0] == 'completed'
    assert len(world.w.fans.posts) == 1 and world.out_messages() == 1


def test_unknown_outcome_stays_held_and_is_never_retried(make_world):
    world = make_world(['timeout'])
    post(world, 91010)
    world.ticks(1)
    [row] = world.calls()
    assert row.status == 'unknown' and row.retry_not_before is None
    for seconds in (31, 600, 3600):
        world.advance(seconds)
        world.ticks(2)
    assert world.provider.calls == ['timeout'] and world.request(91010)[0] == 'open' and not world.w.fans.posts


def test_scope_stop_during_wait_prevents_the_retry(make_world):
    world = make_world([429])
    post(world, 91011)
    world.ticks(1)
    current = pilot_scope.describe(world.w.store, TENANT, ACCOUNT)
    pilot_scope.set_status(world.w.store, TENANT, ACCOUNT, 'stopped', expected_revision=current['revision'],
                           command_key=key(91911), actor=ACTOR, reason='stop while a model retry is pending')
    world.advance(31)
    for _ in range(3):
        try:
            world.pilot.tick()
        except Exception:
            pass  # a stopped scope refuses the tick; that is the control working
    assert world.provider.calls == [429] and not world.w.fans.posts


def test_takeover_during_wait_prevents_the_retry(make_world):
    from db.models import OfConversationState
    world = make_world([429])
    post(world, 91012)
    world.ticks(1)
    with Session(world.w.database[1]) as s:
        revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
    world.w.store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=revision, command_key='r0-takeover',
                          actor=ACTOR, reason='takeover while a model retry is pending', action='request_takeover')
    world.advance(31)
    world.ticks(3)
    assert world.provider.calls == [429] and not world.w.fans.posts


def test_newer_message_supersedes_the_waiting_request_without_a_duplicate(make_world):
    world = make_world([429])
    post(world, 91013, text_='are you free tonight?')
    world.ticks(1)
    post(world, 91014, text_='Baby?')
    world.ticks(2)
    assert world.provider.calls == [429, 'ok'] and world.request(91014)[0] == 'completed'
    world.advance(31)
    world.ticks(3)
    assert world.provider.calls == [429, 'ok'], 'the old request is not answered again after the newer one'
    assert world.request(91013) == ('cancelled', 'superseded_by_newer_input', None)
    assert len(world.w.fans.posts) == 1


def test_other_fan_progresses_while_one_request_waits(make_world):
    world = make_world([429])
    post(world, 91015)
    world.ticks(1)
    post(world, 91016, fan=FAN2)
    world.ticks(2)
    assert world.provider.calls == [429, 'ok'] and world.request(91016)[0] == 'completed'
    assert world.request(91015) == ('open', 'prepare_reply', None)


def test_legacy_released_429_without_due_time_is_rescheduled(make_world):
    """Rows written before 0129b have no retry_not_before: the fixed cooldown from their last update applies."""
    world = make_world([429])
    post(world, 91017)
    world.ticks(1)
    with world.w.database[1].begin() as c:
        c.execute(text("ALTER TABLE of_engine_model_call DISABLE TRIGGER of_engine_model_call_guard"))
        c.execute(update(OfEngineModelCall).values(retry_not_before=None, failure_detail_json=None))
        c.execute(text("ALTER TABLE of_engine_model_call ENABLE TRIGGER of_engine_model_call_guard"))
    world.advance(31)
    world.ticks(3)
    assert world.provider.calls == [429, 'ok'] and world.request(91017)[0] == 'completed'


def test_released_retry_evidence_is_immutable(make_world):
    world = make_world([429])
    post(world, 91018)
    world.ticks(1)
    with pytest.raises(Exception, match='immutable'):
        with world.w.database[1].begin() as c:
            c.execute(update(OfEngineModelCall).values(retry_not_before=None))


def test_populated_0129_upgrade_and_guarded_downgrade(make_world):
    """Existing released rows survive 0129 -> 0129b unchanged (NULL due time); downgrade refuses to drop evidence."""
    from db.schema import _config, check_schema, upgrade
    from alembic import command
    world = make_world([429, 'ok', 429])
    post(world, 91019)
    world.ticks(1)
    with world.w.database[1].begin() as c:
        c.execute(text("ALTER TABLE of_engine_model_call DISABLE TRIGGER of_engine_model_call_guard"))
        c.execute(update(OfEngineModelCall).values(retry_not_before=None, failure_detail_json=None))
        c.execute(text("ALTER TABLE of_engine_model_call ENABLE TRIGGER of_engine_model_call_guard"))
        before = c.execute(text('SELECT call_id,status,outcome,reserved_microusd,updated_at FROM of_engine_model_call')).all()
        command.downgrade(_config(c), '0129_reconnect_receipt')
        check_schema(c, expected='0129_reconnect_receipt')
        upgrade(c)
        check_schema(c)
        assert c.execute(text('SELECT call_id,status,outcome,reserved_microusd,updated_at FROM of_engine_model_call')).all() == before
        assert c.scalar(text('SELECT count(*) FROM of_engine_model_call WHERE retry_not_before IS NOT NULL')) == 0
    world.advance(31)
    world.ticks(3)
    assert world.provider.calls == [429, 'ok']
    post(world, 91020, text_='and now?')
    world.ticks(1)
    assert world.provider.calls == [429, 'ok', 429]  # new evidence written on 0129b
    with pytest.raises(Exception, match='Preserve model retry evidence'):
        with world.w.database[1].begin() as c:
            command.downgrade(_config(c), '0129_reconnect_receipt')
