"""Plan C operator HTTP surface (of-engine-operator.v2) against a real database and the real approval gate.

The model is the in-process fixture and the provider is httpx.MockTransport (no network). Every approval here is an
explicit HTTP command from a named operator session; nothing approves or hands back automatically.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_engine_real_worker_pg import world, answer, PROVIDER, CREATOR, FAN2  # noqa: F401
from test_engine_approval_pg import stage, offer_draft, send, approvals
from test_engine_operator_pg import setup as synthetic_setup, command as synthetic_command
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from conversation_engine.engine import SyntheticProposal
from conversation_engine.operator import execute
from conversation_engine.real_worker import RealConversationWorker
from db.models import OfConversationState
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineDecision, OfEngineOwner, OfEngineRequest
from db.onlyfans_fans_intake_models import OfFansEvidence
from db.onlyfans_models import OfControlAudit
from fans_api_adapter.real_transport import FansApiRealTransport, RealFansConfig, build_client
from onlyfans_commands.engine_approval_api import router, SCHEMA, HEADER
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow

TOKEN = 'fixture-control-token'
ACTOR = 'op-session:' + 'a' * 64
OTHER = 'op-session:' + 'b' * 64
EVIDENCE = 'operator:fansapi:checked the creator chat in the OnlyFans app'


def key(n):
    return f'{n:064x}'


@pytest.fixture
def api(world, monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'api')
    monkeypatch.setenv('ONLYFANS_CONTROL_TOKEN', TOKEN)
    monkeypatch.setenv('ONLYFANS_CONTROL_TENANT', TENANT)
    for name in ('ONLYFANS_API_TOKEN', 'ONLYFANS_CONFIGURATION_TOKEN', 'ONLYFANS_CAMPAIGN_TOKEN', 'ONLYFANS_CONTENT_TOKEN',
                 'ONLYFANS_CONNECTION_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    app = FastAPI()
    app.state.control_store = world.store
    app.include_router(router)

    @app.middleware('http')
    async def test_scope(request, call_next):      # stands in for AgencyScopeMiddleware on a scoped request
        if request.headers.get('x-test-agency-scope'):
            from onlyfans_read.agency_scope import AgencyScope, current_scope
            current_scope.set(AgencyScope(('11111111-1111-1111-1111-111111111111',),
                'agency-user:22222222-2222-2222-2222-222222222222:33333333-3333-3333-3333-333333333333', True, True))
        return await call_next(request)
    world.app = app
    with TestClient(app) as client:
        world.client = client
        yield world


HEADERS = {'Authorization': f'Bearer {TOKEN}', HEADER: SCHEMA}


def url(path, account=ACCOUNT):
    return f'/onlyfans/control/v2/accounts/{account}/{path}'


def get(api, path, **kw):
    return api.client.get(url(path), headers=HEADERS, **kw)


def post(api, path, body, headers=HEADERS):
    return api.client.post(url(path), headers=headers, json=body)


def drafts(api):
    response = get(api, 'drafts')
    assert response.status_code == 200, response.text
    return response.json()['drafts']


def draft_path(draft, action):
    return f"drafts/{draft['requestId']}/{draft['generation']}/{action}"


def approve_body(draft, n, actor=ACTOR):
    return {'expectedVersion': draft['draftVersion'], 'commandKey': key(n), 'actor': actor}


def conv(fan=FAN):
    return f'{ACCOUNT}:{fan}'


def revision(api, fan=FAN):
    with Session(api.database[1]) as s:
        return s.get(OfConversationState, (ACCOUNT, fan)).revision


# ------------------------------------------------------------------ the dashboard's parser, written out (plan-c.ts)
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$')
ID = re.compile(r'^[A-Za-z0-9._:-]{1,64}$')
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def is_int(value, low, high):
    return type(value) is int and low <= value <= high


def assert_dashboard_draft(raw, account=ACCOUNT):
    """Everything planc-dash parsePlanCDraft requires; a draft failing any of it would make the whole list unreadable."""
    assert UUID.match(raw['requestId']) and is_int(raw['generation'], 0, 2 ** 31 - 1)
    assert is_int(raw['draftVersion'], 1, 2 ** 31 - 1) and re.fullmatch(r'[0-9a-f]{64}', raw['draftHash'])
    assert raw['status'] in ('awaiting', 'approved') and ISO.match(raw['expiresAt']) and raw['expiresAt'].endswith('Z')
    assert re.fullmatch(r'[0-9]{1,19}', raw['fanId']) and raw['conversationId'] == f"{account}:{raw['fanId']}"
    assert ID.match(raw['conversationId']) and raw['action'] in ('reply', 'offer')
    assert 1 <= len(raw['parts']) <= 8
    assert len({p['part_index'] for p in raw['parts']}) == len(raw['parts'])
    for p in raw['parts']:
        assert is_int(p['part_index'], 0, 1000) and isinstance(p['body'], str) and len(p['body']) <= 4000 and p['action']
        assert p['price_cents'] is None or is_int(p['price_cents'], 0, 2_000_000)
        assert p['currency'] is None or re.fullmatch(r'[A-Z]{3}', p['currency'])
        for field in ('media_ids', 'preview_ids'):
            assert isinstance(p[field], list) and all(isinstance(m, (str, int)) and str(m) for m in p[field])
        assert p['catalog_item_id'] is None or isinstance(p['catalog_item_id'], (int, str))
    assert is_int(raw['conversation']['revision'], 0, 2 ** 31 - 2)
    assert len(raw['conversation']['messages']) <= 10
    for m in raw['conversation']['messages']:
        assert isinstance(m['direction'], str) and isinstance(m['text'], str) and ISO.match(m['at'])
    assert raw['evidence'] is None or isinstance(raw['evidence'], str)
    assert raw['approvedBy'] is None or isinstance(raw['approvedBy'], str)
    assert raw['approvedAt'] is None or ISO.match(raw['approvedAt'])
    assert (raw['status'] == 'approved') == (raw['approvedBy'] is not None)


def assert_dashboard_item(item, account=ACCOUNT):
    assert re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', item['id']) and 1 <= len(item['kind']) <= 64
    assert 1 <= len(item['owner']) <= 64 and 1 <= len(item['nextStep']) <= 128, item
    assert len(item['summary']) <= 1000
    if 'conversationId' in item:
        assert ID.match(item['conversationId']) and item['conversationId'].startswith(account + ':')
    assert 'revision' not in item or is_int(item['revision'], 0, 2 ** 31 - 2)
    assert 'createdAt' not in item or ISO.match(item['createdAt'])


def assert_ack(body, **expected):
    assert body['schemaVersion'] == SCHEMA and body['ok'] is True and body['accountId'] == ACCOUNT
    assert re.fullmatch(r'[0-9a-f]{64}', body['commandKey']) and type(body['replayed']) is bool
    for name, value in expected.items():
        assert body[name] == value, (name, body)


# ======================================================================== auth, schema, strict JSON
def test_credential_schema_header_and_configuration_are_enforced(api, monkeypatch):
    path = url('drafts')
    assert api.client.get(path, headers={HEADER: SCHEMA}).status_code == 401
    assert api.client.get(path, headers={**HEADERS, 'Authorization': 'Bearer wrong'}).status_code == 401
    assert api.client.get(path, headers={**HEADERS, 'Authorization': 'Basic ' + TOKEN}).status_code == 401
    assert api.client.get(path, headers={'Authorization': HEADERS['Authorization']}).status_code == 400
    assert api.client.get(path, headers={**HEADERS, HEADER: 'of-engine-operator.v1'}).status_code == 400
    ok = api.client.get(path, headers=HEADERS)
    assert ok.status_code == 200 and ok.headers[HEADER] == SCHEMA and ok.headers['cache-control'] == 'private, no-store'
    assert ok.json()['schemaVersion'] == SCHEMA
    monkeypatch.setenv('ONLYFANS_CONTROL_TENANT', 'deployment')
    assert api.client.get(path, headers=HEADERS).status_code == 503
    monkeypatch.setenv('ONLYFANS_CONTROL_TENANT', TENANT)
    monkeypatch.delenv('ONLYFANS_CONTROL_TOKEN')
    assert api.client.get(path, headers=HEADERS).status_code == 503
    monkeypatch.setenv('ONLYFANS_CONTROL_TOKEN', TOKEN)
    api.app.state.control_store = None
    assert api.client.get(path, headers=HEADERS).status_code == 503
    api.app.state.control_store = api.store
    assert api.client.get(url('drafts', 'acc-unknown'), headers=HEADERS).status_code == 404
    assert api.client.get(url('drafts', 'bad id'), headers=HEADERS).status_code in (400, 404)


def test_agency_scope_is_refused_the_surface_is_operator_only(api, monkeypatch):
    stage(api)
    response = api.client.get(url('drafts'), headers={**HEADERS, 'x-test-agency-scope': '1'})
    assert response.status_code == 403 and response.json()['code'] == 'operator_only'
    assert api.client.get(url('drafts'), headers=HEADERS).status_code == 200


def test_strict_body_rules(api):
    staged, mid = stage(api)
    d = drafts(api)[0]
    path = url(draft_path(d, 'approve'))
    good = approve_body(d, 1)
    raw = lambda text, ctype='application/json': api.client.post(path, headers={**HEADERS, 'Content-Type': ctype}, content=text)
    assert raw(json.dumps(good), 'text/plain').status_code == 400
    assert raw(json.dumps(good)[:-1] + ',"expectedVersion":1}').status_code == 400          # duplicate key
    assert raw('{"expectedVersion":NaN,"commandKey":"%s","actor":"%s"}' % (key(1), ACTOR)).status_code == 400
    assert raw('{"expectedVersion":Infinity,"commandKey":"%s","actor":"%s"}' % (key(1), ACTOR)).status_code == 400
    assert raw('[]').status_code == 400 and raw('not json').status_code == 400
    assert raw(json.dumps(good) + ' ' * 8200).status_code == 413
    for bad in ({**good, 'extra': 1}, {k: v for k, v in good.items() if k != 'actor'}, {**good, 'commandKey': 'short'},
                {**good, 'commandKey': 'A' * 64}, {**good, 'expectedVersion': True}, {**good, 'expectedVersion': '1'},
                {**good, 'expectedVersion': 0}, {**good, 'expectedVersion': 1.5}):
        assert post(api, draft_path(d, 'approve'), bad).status_code == 400, bad
    for actor in ('engine', 'offline-engine', 'auto-approver', 'system', 'operator-jane', '', 'op-session:short',
                  'agency-user:not-a-uuid'):
        assert post(api, draft_path(d, 'approve'), {**good, 'actor': actor}).status_code == 400, actor
    assert post(api, f'drafts/not-a-uuid/0/approve', good).status_code == 400
    assert api.client.post(url(f"drafts/{d['requestId']}/0/destroy"), headers=HEADERS, json=good).status_code == 404
    assert approvals(api)[0].status == 'awaiting' and api.fans.posts == []                    # nothing above approved anything


# ======================================================================== draft listing
def test_draft_listing_matches_the_dashboard_contract_exactly(api):
    api.say_plain('Hey you, glad you wrote')
    staged, mid = stage(api, 'Hey you, glad you wrote', parts=('and one more thing',), say='are you there')
    listed = get(api, 'drafts').json()
    assert listed['ok'] is True and listed['accountId'] == ACCOUNT and listed['schemaVersion'] == SCHEMA
    [d] = listed['drafts']
    assert_dashboard_draft(d)
    assert set(d) >= {'requestId', 'generation', 'draftVersion', 'draftHash', 'status', 'action', 'fanId', 'conversationId',
                      'expiresAt', 'parts', 'conversation', 'evidence', 'approvedBy', 'approvedAt'}
    assert d['status'] == 'awaiting' and d['action'] == 'reply' and d['draftVersion'] == 1
    assert [p['body'] for p in d['parts']] == ['Hey you, glad you wrote', 'and one more thing']
    assert [p['part_index'] for p in d['parts']] == [0, 1]
    assert d['fanId'] == str(FAN) and d['conversationId'] == conv() and d['conversation']['revision'] == revision(api)
    assert d['conversation']['messages'][-1] == {'direction': 'fan', 'text': 'are you there', 'at': d['conversation']['messages'][-1]['at']}
    assert approvals(api)[0].draft_hash == d['draftHash'] and approvals(api)[0].request_id == d['requestId']
    expires = datetime.fromisoformat(d['expiresAt'].replace('Z', '+00:00'))
    assert timedelta(0) < expires - datetime.now(timezone.utc) <= timedelta(hours=24)


def test_offer_listing_shows_the_card_the_operator_approves(api):
    staged, grant = offer_draft(api)
    [d] = drafts(api)
    assert_dashboard_draft(d)
    assert d['action'] == 'offer'
    offer = [p for p in d['parts'] if p['action'] == 'offer'][0]
    assert offer['price_cents'] and offer['currency'] == 'USD' and offer['media_ids'] and offer['catalog_item_id'] is not None
    assert d['offer'] == {'productId': str(offer['catalog_item_id']), 'mediaIds': [str(m) for m in offer['media_ids']],
                          'mediaCount': len(offer['media_ids']), 'priceCents': offer['price_cents'], 'currency': 'USD'}


def test_listing_is_empty_for_a_synthetic_account_and_commands_are_refused(api):
    from onlyfans_runtime.text_delivery import TextAuthority
    with Session(api.database[1]) as s:
        from db.onlyfans_delivery_models import OfDeliveryPolicy
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        args = dict(expected_revision=policy.revision, enabled=policy.enabled, limit_units=policy.limit_units,
                    throttle_seconds=policy.throttle_seconds)
    TextAuthority(api.store).configure(TENANT, ACCOUNT, actor='lab', reason='back to synthetic', transport_kind='synthetic', **args)
    assert get(api, 'drafts').json()['drafts'] == [] and get(api, 'unresolved').json()['items'] == []
    body = {'commandKey': key(5), 'actor': ACTOR, 'expectedRevision': 0}
    assert post(api, f'conversations/{conv()}/stop', body).status_code == 409
    assert post(api, f'drafts/{uuid.uuid4()}/0/approve', {'expectedVersion': 1, 'commandKey': key(6), 'actor': ACTOR}).status_code == 409


# ======================================================================== draft commands
def test_approve_is_idempotent_and_records_who_approved(api):
    staged, mid = stage(api)
    [d] = drafts(api)
    body = approve_body(d, 10)
    first = post(api, draft_path(d, 'approve'), body)
    assert first.status_code == 200, first.text
    assert_ack(first.json(), commandKey=key(10), replayed=False, recordedVersion=1, requestId=d['requestId'], action='approve')
    row = approvals(api)[0]
    assert row.status == 'approved' and row.actor == ACTOR and api.fans.posts == []        # approving never sends by itself
    [shown] = drafts(api)
    assert shown['status'] == 'approved' and shown['approvedBy'] == ACTOR and shown['draftHash'] == d['draftHash']
    assert_dashboard_draft(shown)
    again = post(api, draft_path(d, 'approve'), body)
    assert again.status_code == 200 and again.json() == {**first.json(), 'replayed': True}
    assert len(approvals(api)) == 1
    # the same key with any other body is refused, never applied
    assert post(api, draft_path(d, 'approve'), {**body, 'actor': OTHER}).status_code == 409
    assert post(api, draft_path(d, 'reject'), {**body, 'reason': 'changed my mind'}).status_code == 409
    assert approvals(api)[0].status == 'approved'
    # a second click with a new key is the approval layer's own idempotence: still one approval, one send
    second = post(api, draft_path(d, 'approve'), approve_body(d, 11, OTHER))
    assert second.status_code == 200 and second.json()['recordedVersion'] == 1 and approvals(api)[0].actor == ACTOR
    api.no_throttle()
    assert send(api, staged)['outcome'] == 'accepted' and len(api.fans.posts) == 1


def test_version_conflict_is_409_and_changes_nothing(api):
    stage(api)
    [d] = drafts(api)
    stale = {**approve_body(d, 12), 'expectedVersion': 7}
    response = post(api, draft_path(d, 'approve'), stale)
    assert response.status_code == 409 and response.json()['ok'] is False
    assert approvals(api)[0].status == 'awaiting'
    assert post(api, draft_path(d, 'approve'), {**approve_body(d, 13), 'expectedVersion': 7}).status_code == 409   # not recorded as done
    assert post(api, draft_path(d, 'approve'), approve_body(d, 13)).status_code == 200                              # the key is still usable
    unknown = post(api, f'drafts/{uuid.uuid4()}/0/approve', approve_body(d, 14))
    assert unknown.status_code == 404
    assert post(api, f"drafts/{d['requestId']}/5/approve", approve_body(d, 15)).status_code == 404


def test_reject_needs_a_reason_and_removes_the_draft(api):
    staged, mid = stage(api)
    [d] = drafts(api)
    body = {**approve_body(d, 20), 'reason': 'wrong tone for this fan'}
    assert post(api, draft_path(d, 'reject'), {k: v for k, v in body.items() if k != 'reason'}).status_code == 400
    for reason in ('', '   ', 'x' * 201, 'bad\x00reason'):
        assert post(api, draft_path(d, 'reject'), {**body, 'reason': reason}).status_code == 400
    first = post(api, draft_path(d, 'reject'), body)
    assert first.status_code == 200 and first.json()['status'] == 'rejected'
    assert_ack(first.json(), commandKey=key(20), replayed=False, action='reject')
    assert post(api, draft_path(d, 'reject'), body).json()['replayed'] is True
    assert post(api, draft_path(d, 'reject'), {**body, 'reason': 'another reason'}).status_code == 409
    assert drafts(api) == []
    assert {r.status for r in api.rows(OfEngineRequest)} == {'cancelled'} and api.fans.posts == []
    assert send(api, staged)['outcome'] != 'accepted' and api.fans.posts == []


def test_edit_makes_a_new_version_that_needs_its_own_approval(api):
    staged, mid = stage(api, 'Hey you, glad you wrote', parts=('second part',))
    [d] = drafts(api)
    ok = post(api, draft_path(d, 'approve'), approve_body(d, 30))
    assert ok.status_code == 200
    [approved] = drafts(api)
    body = {'expectedVersion': 1, 'commandKey': key(31), 'actor': ACTOR, 'parts': ['Hi, glad you wrote!', 'second part, edited']}
    edited = post(api, draft_path(d, 'edit'), body)
    assert edited.status_code == 200, edited.text
    assert_ack(edited.json(), recordedVersion=2, generation=d['generation'] + 1, action='edit', status='awaiting', replayed=False)
    [new] = drafts(api)
    assert_dashboard_draft(new)
    assert new['draftVersion'] == 2 and new['status'] == 'awaiting' and new['approvedBy'] is None
    assert new['requestId'] == d['requestId'] and new['generation'] == d['generation'] + 1      # an edit supersedes the draft
    assert [p['body'] for p in new['parts']] == body['parts'] and new['draftHash'] != approved['draftHash']
    assert [r.status for r in approvals(api)] == ['invalidated', 'awaiting']                # the approved version is retired
    assert post(api, draft_path(d, 'edit'), body).json() == {**edited.json(), 'replayed': True}
    assert post(api, draft_path(d, 'edit'), {**body, 'parts': ['x y', 'z w']}).status_code == 409   # key reuse
    stale = post(api, draft_path(d, 'approve'), approve_body(d, 32))                                  # expectedVersion 1 is now stale
    assert stale.status_code == 409
    assert api.fans.posts == []
    assert send(api, staged)['outcome'] != 'accepted' and api.fans.posts == []                      # an edit is not an approval
    assert post(api, draft_path(new, 'approve'), approve_body(new, 33)).status_code == 200
    api.no_throttle()
    head = [x for x in api.rows(OfEngineDecision) if x.generation == new['generation'] and x.part_index == 0][0]
    assert api.worker.send_approved(TENANT, ACCOUNT, head.decision_id)['outcome'] == 'accepted'
    assert [p for p in (x['body']['text'] for x in api.fans.posts)] == body['parts']


@pytest.mark.parametrize('parts', [['one—dash', 'two'], ['', 'two'], [], 'text', [1, 2], ['a'] * 5])
def test_edit_input_is_validated(api, parts):
    stage(api, 'one', parts=('two',))
    [d] = drafts(api)
    response = post(api, draft_path(d, 'edit'), {'expectedVersion': 1, 'commandKey': key(40), 'actor': ACTOR, 'parts': parts})
    assert response.status_code == 400, response.text
    assert [r.status for r in approvals(api)] == ['awaiting']


def test_edit_cannot_change_the_part_count_or_smuggle_a_price(api):
    stage(api, 'one', parts=('two',))
    [d] = drafts(api)
    assert post(api, draft_path(d, 'edit'), {'expectedVersion': 1, 'commandKey': key(41), 'actor': ACTOR, 'parts': ['only one']}).status_code == 400
    assert post(api, draft_path(d, 'edit'), {'expectedVersion': 1, 'commandKey': key(42), 'actor': ACTOR,
                                              'parts': ['one', 'sure, the whole set is $5']}).status_code in (400, 409)
    assert [r.status for r in approvals(api)] == ['awaiting']


# ======================================================================== conversation controls
def test_stop_pauses_automation_withdraws_the_draft_and_is_replay_safe(api):
    staged, mid = stage(api)
    [d] = drafts(api)
    assert post(api, draft_path(d, 'approve'), approve_body(d, 50)).status_code == 200
    revision_before = revision(api)
    body = {'commandKey': key(51), 'actor': ACTOR, 'expectedRevision': revision_before}
    first = post(api, f'conversations/{conv()}/stop', body)
    assert first.status_code == 200, first.text
    assert_ack(first.json(), conversationId=conv(), action='stop', recordedRevision=revision_before + 1, replayed=False)
    with Session(api.database[1]) as s:
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        assert (state.mode, state.takeover_state) == ('paused', 'none')
        audit = s.get(OfControlAudit, (ACCOUNT, key(51)))
        assert (audit.actor, audit.action) == (ACTOR, 'pause_automation')
    assert drafts(api) == [] and [r.status for r in approvals(api)] == ['invalidated']
    assert send(api, staged)['outcome'] != 'accepted' and api.fans.posts == []
    assert post(api, f'conversations/{conv()}/stop', body).json() == {**first.json(), 'replayed': True}
    assert post(api, f'conversations/{conv()}/takeover', body).status_code == 409           # same key, other action
    assert post(api, f'conversations/{conv()}/stop', {**body, 'actor': OTHER}).status_code == 409
    assert post(api, f'conversations/{conv()}/stop', {**body, 'commandKey': key(52)}).status_code == 409   # stale revision AND not automated


def test_takeover_and_handback_never_approve_or_replay_a_draft(api):
    staged, mid = stage(api)
    [d] = drafts(api)
    assert post(api, draft_path(d, 'approve'), approve_body(d, 60)).status_code == 200
    base = revision(api)
    take = post(api, f'conversations/{conv()}/takeover', {'commandKey': key(61), 'actor': ACTOR, 'expectedRevision': base})
    assert take.status_code == 200 and take.json()['recordedRevision'] == base + 1
    assert drafts(api) == [] and [r.status for r in approvals(api)] == ['invalidated']
    with Session(api.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'human_controlled'
        assert s.get(OfEngineOwner, (ACCOUNT, FAN)).mode == 'human'
    # the held conversation is visible with a revision the next command can use
    [hold] = [i for i in get(api, 'unresolved').json()['items'] if i['kind'] == 'human_hold']
    assert_dashboard_item(hold)
    assert hold['conversationId'] == conv() and hold['revision'] == base + 1 and hold['owner'] == 'human'
    assert hold['nextStep'] == 'awaiting_handback'
    assert send(api, staged)['outcome'] != 'accepted' and api.fans.posts == []
    stale = post(api, f'conversations/{conv()}/handback', {'commandKey': key(62), 'actor': ACTOR, 'expectedRevision': base})
    assert stale.status_code == 409
    back = post(api, f'conversations/{conv()}/handback', {'commandKey': key(63), 'actor': ACTOR, 'expectedRevision': base + 1})
    assert back.status_code == 200, back.text
    assert_ack(back.json(), action='handback', recordedRevision=base + 2, replayed=False)
    assert post(api, f'conversations/{conv()}/handback', {'commandKey': key(63), 'actor': ACTOR, 'expectedRevision': base + 1}).json()['replayed'] is True
    # handed back: the engine owns planning again, but the old draft is neither approved nor sendable, and nothing went out
    with Session(api.database[1]) as s:
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        assert (state.mode, state.takeover_state) == ('automated', 'none')
        assert s.get(OfEngineOwner, (ACCOUNT, FAN)).mode == 'active'
    assert drafts(api) == [] and [r.status for r in approvals(api)] == ['invalidated']
    assert all(i['kind'] != 'human_hold' for i in get(api, 'unresolved').json()['items'])
    assert send(api, staged)['outcome'] != 'accepted' and api.fans.posts == [] and api.rows(OfTextIntent) == []
    assert post(api, draft_path(d, 'approve'), approve_body(d, 64)).status_code == 409           # a dead draft cannot be approved
    # a fresh turn stages a NEW draft that needs its own approval
    api.say_plain('a fresh answer after handback')
    fresh = api.turn(TENANT, ACCOUNT, FAN, mid)
    assert fresh['outcome'] == 'awaiting_approval' and api.fans.posts == []
    [new] = drafts(api)
    assert new['status'] == 'awaiting' and new['generation'] == 1 and new['conversation']['revision'] == base + 2


def test_handback_is_refused_unless_a_human_holds_the_conversation(api):
    stage(api)
    response = post(api, f'conversations/{conv()}/handback', {'commandKey': key(70), 'actor': ACTOR, 'expectedRevision': revision(api)})
    assert response.status_code == 409
    assert [r.status for r in approvals(api)] == ['awaiting']


def test_conversation_ids_and_scope_are_validated(api):
    stage(api)
    body = {'commandKey': key(71), 'actor': ACTOR, 'expectedRevision': revision(api)}
    assert post(api, f'conversations/other-account:{FAN}/stop', body).status_code == 400
    assert post(api, f'conversations/{ACCOUNT}:0/stop', body).status_code == 400
    assert post(api, f'conversations/{ACCOUNT}:{FAN}x/stop', body).status_code == 400
    assert post(api, f'conversations/{ACCOUNT}:999/stop', body).status_code == 404          # no such fan
    assert post(api, f'conversations/{conv()}/resume', body).status_code == 404            # no such action
    assert post(api, f'conversations/{conv()}/stop', {**body, 'extra': 1}).status_code == 400
    assert post(api, f'conversations/{conv()}/stop', {**body, 'expectedRevision': -1}).status_code == 400
    assert post(api, f'conversations/{conv()}/stop', {**body, 'expectedRevision': True}).status_code == 400
    assert post(api, f'conversations/{conv()}/stop', {**body, 'actor': 'engine'}).status_code == 400
    assert api.client.post(url(f'conversations/{conv()}/stop', 'acc-unknown'), headers=HEADERS, json=body).status_code in (400, 404)
    with Session(api.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated'


# ======================================================================== unresolved work
def unknown_send(api):
    """The provider answers 503 to the send: an ambiguous outcome. Returns the unknown intent id."""
    def handle(request):
        if request.method == 'POST' and request.url.path.endswith('/messages'):
            return answer(503, {})
        return api.fans.handle(request)
    client = build_client(RealFansConfig('fixture-token', PROVIDER, 'https://api.fansapi.com', 5), creator_id=CREATOR,
                          epoch=1, transport=httpx.MockTransport(handle))
    transport = FansApiRealTransport(api.store, TENANT, ACCOUNT, client, allowed_chats=frozenset({str(FAN), str(FAN2)}),
                                     sleep=lambda s: None, enabled=True)
    api.worker = RealConversationWorker(api.engine, api.intake, transport, sleep=lambda sec: api.no_throttle())
    staged, mid = stage(api, 'this will be an unknown send')
    [d] = drafts(api)
    assert post(api, draft_path(d, 'approve'), approve_body(d, 80)).status_code == 200
    api.no_throttle()
    result = api.worker.send_approved(TENANT, ACCOUNT, staged['decisionId'])
    assert result['outcome'] != 'accepted', result
    [intent] = api.rows(OfTextIntent)
    assert intent.status == 'unknown'
    return intent.intent_id, staged


def test_applied_unlock_note_retains_unlinked_financial_evidence(api, monkeypatch):
    from test_engine_real_worker_pg import _sell, unlock
    from db.models import Transaction
    _, message_id = _sell(api)
    with monkeypatch.context() as worker:
        worker.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
        assert api.send(unlock(message_id), key='operator-unlock')['outcome'] == 'evidence'
    items = get(api, 'unresolved').json()['items']
    [item] = [entry for entry in items if entry['kind'] == 'unreconciled_access' and 'Provider reported an unlock' in entry['summary']]
    result = post(api, f"unresolved/{item['id']}/reconcile",
                  {'commandKey':key(870), 'actor':ACTOR, 'evidenceRef':EVIDENCE})
    assert result.status_code == 200 and result.json()['outcome'] == 'evidence_recorded_finance_unlinked'
    assert result.json()['resolved'] is False
    assert any(entry['id'] == item['id'] for entry in get(api, 'unresolved').json()['items'])
    with Session(api.database[1]) as s:
        assert list(s.scalars(select(Transaction))) == []


def test_access_review_refuses_evidence_from_an_earlier_creator_binding(api, monkeypatch):
    from test_engine_real_worker_pg import _sell, unlock
    from db.models import OfCreatorBinding
    _, message_id = _sell(api)
    with monkeypatch.context() as worker:
        worker.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
        api.send(unlock(message_id), key='binding-unlock')
    with Session(api.database[1]) as s, s.begin():
        [access] = list(s.scalars(select(OfEngineAccessEvidence)))
        evidence_key, intent_id = 'separate-revocation-evidence', access.intent_id
        s.add(OfEngineAccessEvidence(account_id=ACCOUNT, evidence_key=evidence_key, intent_id=intent_id,
            fan_id=FAN, message_id=message_id, media_json=access.media_json, status='revoked', applied=False,
            evidence_ref='fixture:separate-revocation', occurred_at=utcnow(), created_at=utcnow()))
    item_id = 'unreconciled_access:access-' + evidence_key
    assert item_id in {item['id'] for item in get(api, 'unresolved').json()['items']}
    with Session(api.database[1]) as s, s.begin():
        s.add(OfCreatorBinding(id='historical-binding', tenant_id='historical-tenant', creator_id='historic',
                               account_id=ACCOUNT, channel='onlyfans', revoked_at=utcnow()))
        s.flush()
        s.get(OfTextIntent, (ACCOUNT, intent_id)).binding_id = 'historical-binding'
    assert item_id not in {item['id'] for item in get(api, 'unresolved').json()['items']}
    assert post(api, f'unresolved/{item_id}/reconcile',
                {'commandKey':key(871), 'actor':ACTOR, 'evidenceRef':EVIDENCE}).status_code == 404


def test_unresolved_displays_unknown_model_cost_and_open_request(api):
    from conversation_engine.model_client import FixtureChatClient, ModelOutcomeUnknown
    from conversation_engine.model_runtime import ModelRuntime
    from conversation_engine.writer_context import StaticCreatorProfiles, active_profile
    from test_engine_model_pg import config
    client = FixtureChatClient([ModelOutcomeUnknown('simulated lost response')])
    api.engine.model = ModelRuntime(api.database[1], client, config(), StaticCreatorProfiles({ACCOUNT:active_profile(ACCOUNT)}))
    api.operator_on = False
    result, _ = api.say('hello')
    assert result['outcome'] == 'refused' and len(client.requests) == 1 and api.fans.posts == []
    items = get(api, 'unresolved').json()['items']
    [call] = [item for item in items if item['kind'] == 'model_call']
    [request] = [item for item in items if item['kind'] == 'open_request']
    assert call['owner'] == 'operator' and call['nextStep'] == 'review_model_usage'
    assert 'do not assume it was free' in call['summary'].lower()
    assert request['owner'] and request['nextStep'] and request['conversationId'] == conv()
    for item in (call, request):
        assert_dashboard_item(item)


def test_unresolved_displays_unverified_returned_model_identity(api):
    from conversation_engine.model_client import ChatResult, FixtureChatClient, Usage
    from conversation_engine.model_runtime import ModelRuntime
    from conversation_engine.writer_context import StaticCreatorProfiles, active_profile
    from test_engine_model_pg import config, ok
    client = FixtureChatClient([ChatResult(text=ok('hello'), usage=Usage(100, 10, 500),
                                          model='m/primary', provider='Wrong Host')])
    cfg = config(upstream_provider='fixture-host', upstream_reported_name='Fixture Host')
    api.engine.model = ModelRuntime(api.database[1], client, cfg, StaticCreatorProfiles({ACCOUNT:active_profile(ACCOUNT)}))
    api.operator_on = False
    result, _ = api.say('hello')
    assert result['outcome'] == 'refused' and api.fans.posts == []
    items = get(api, 'unresolved').json()['items']
    [call] = [item for item in items if item['kind'] == 'model_call']
    assert call['nextStep'] == 'review_model_identity' and 'do not use' in call['summary'].lower()
    assert any(item['kind'] == 'handover_request' for item in items)
    assert_dashboard_item(call)


def test_waiting_fan_request_remains_visible_without_automatic_followup(api):
    staged, _ = offer_draft(api)
    draft = drafts(api)[0]
    assert post(api, draft_path(draft, 'approve'), approve_body(draft, 880)).status_code == 200
    assert send(api, staged)['outcome'] == 'accepted'
    before = len(api.fans.posts)
    items = get(api, 'unresolved').json()['items']
    [request] = [item for item in items if item['kind'] == 'open_request']
    assert request['nextStep'] == 'await_purchase_or_question' and 'waiting_fan' in request['summary']
    assert len(api.fans.posts) == before
    assert_dashboard_item(request)


def test_unresolved_lists_unknown_sends_with_owner_and_next_step_and_reconcile_never_clears_them(api):
    intent_id, staged = unknown_send(api)
    posts_before = len(api.fans.posts)
    body = get(api, 'unresolved').json()
    assert body['ok'] is True and body['accountId'] == ACCOUNT and body['schemaVersion'] == SCHEMA
    [item] = [i for i in body['items'] if i['kind'] == 'unknown_send']
    assert_dashboard_item(item)
    assert item['id'] == f'unknown_send:{intent_id}' and item['owner'] and item['nextStep'] == 'reconcile_with_provider_history'
    assert item['conversationId'] == conv() and item['revision'] == revision(api) and 'resend' in item['summary'].lower()
    for every in body['items']:
        assert_dashboard_item(every)

    path = f"unresolved/{item['id']}/reconcile"
    good = {'commandKey': key(81), 'actor': ACTOR, 'evidenceRef': EVIDENCE}
    # no evidence, wrong evidence, short evidence, synthetic evidence: all refused and nothing recorded
    for bad in ({k: v for k, v in good.items() if k != 'evidenceRef'}, {**good, 'evidenceRef': ''}, {**good, 'evidenceRef': 'looked fine'},
                {**good, 'evidenceRef': 'operator:fansapi:abc'}, {**good, 'evidenceRef': 'operator:fansapi:' + 'x' * 109},
                {**good, 'evidenceRef': 'synthetic:fixture review'}, {**good, 'evidenceRef': 'operator:fansapi:       '},
                {**good, 'evidenceRef': 5}, {**good, 'actor': 'engine'}, {**good, 'extra': 1}):
        assert post(api, path, bad).status_code == 400, bad
    with Session(api.database[1]) as s:
        assert s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.kind == 'plan_c_reconcile')).first() is None

    first = post(api, path, good)
    assert first.status_code == 200, first.text
    assert_ack(first.json(), id=item['id'], action='reconcile', outcome='evidence_recorded_hold_retained', resolved=False, replayed=False)
    # the operator's evidence is audited, but an unknown send is never marked not-sent, replaced or resent
    [intent] = api.rows(OfTextIntent)
    assert intent.intent_id == intent_id and intent.status == 'unknown' and intent.receipt_json is None
    assert [d.status for d in api.rows(OfEngineDecision)] == ['unknown'] and len(api.rows(OfTextIntent)) == 1
    assert len(api.fans.posts) == posts_before
    with Session(api.database[1]) as s:
        record = s.get(OfLaunchCommand, (ACCOUNT, key(81)))
        assert record.actor == ACTOR and record.reason == EVIDENCE and record.kind == 'plan_c_reconcile'
    [after] = [i for i in get(api, 'unresolved').json()['items'] if i['kind'] == 'unknown_send']
    assert after['nextStep'] == 'awaiting_provider_proof' and 'does not release' in after['summary']
    assert post(api, path, good).json() == {**first.json(), 'replayed': True}
    assert post(api, path, {**good, 'evidenceRef': EVIDENCE + ' again'}).status_code == 409           # key reuse, other body
    assert post(api, path, {**good, 'commandKey': key(82)}).status_code == 200                        # a second observation is allowed
    assert post(api, f'unresolved/unknown_send:{uuid.uuid4()}/reconcile', {**good, 'commandKey': key(83)}).status_code == 404
    assert post(api, 'unresolved/nonsense/reconcile', {**good, 'commandKey': key(84)}).status_code == 404
    assert len(api.fans.posts) == posts_before and api.rows(OfTextIntent)[0].status == 'unknown'
    # the fan stays held: a new reply is not sent to a fan with an unresolved send
    api.say_plain('another answer')
    again = api.say('hello again')[0]
    assert again['outcome'] != 'accepted' and len(api.fans.posts) == posts_before


def test_unconfirmed_access_evidence_is_listed_and_acknowledged_without_touching_the_row(api):
    intent_id, staged = unknown_send(api)
    with Session(api.database[1]) as s, s.begin():
        s.add(OfEngineAccessEvidence(account_id=ACCOUNT, evidence_key='ev-1', intent_id=intent_id, fan_id=FAN, message_id=7001,
            media_json='[]', status='revoked', applied=False, evidence_ref='fixture', occurred_at=utcnow(), created_at=utcnow()))
    items = get(api, 'unresolved').json()['items']
    access = [i for i in items if i['kind'] == 'unreconciled_access']
    assert len(access) == 1 and access[0]['id'] == 'unreconciled_access:access-ev-1'
    assert_dashboard_item(access[0])
    assert access[0]['owner'] and access[0]['nextStep'] == 'confirm_access_evidence'
    ack = post(api, f"unresolved/{access[0]['id']}/reconcile", {'commandKey': key(90), 'actor': ACTOR, 'evidenceRef': EVIDENCE})
    assert ack.status_code == 200 and ack.json()['outcome'] == 'acknowledged'
    assert [i for i in get(api, 'unresolved').json()['items'] if i['kind'] == 'unreconciled_access'] == []
    with Session(api.database[1]) as s:      # the evidence row itself is untouched
        row = s.get(OfEngineAccessEvidence, (ACCOUNT, 'ev-1'))
        assert (row.status, row.applied) == ('revoked', False)


def evidence_row(api, *, kind='transaction', status='held', created=None, reason='amount_mismatch', fan=FAN,
                 amount=1000, scoped=True, epoch=1):
    from db.onlyfans_engine_models import OfFansWebhook
    evidence_id = str(uuid.uuid4())
    event_key = 'fixture-' + evidence_id if scoped else None
    raw = json.dumps({'fixture_evidence': evidence_id}, separators=(',', ':')).encode()
    digest = hashlib.sha256(raw).hexdigest()
    event = {'transaction':'transactions.new', 'chargeback':'chargebacks.new', 'unlock':'messages.ppv.unlocked',
             'human_sent':'messages.sent'}[kind]
    with Session(api.database[1]) as s, s.begin():
        if scoped:
            s.add(OfFansWebhook(account_id=ACCOUNT, epoch=epoch, event_key=event_key, event_type=event,
                                payload_sha256=digest, raw_body=raw, status='accepted', received_at=utcnow()))
        s.add(OfFansEvidence(account_id=ACCOUNT, evidence_id=evidence_id, dedupe_key=evidence_id[:20], kind=kind,
            source='webhook', event_key=event_key, fan_id=fan, amount_cents=amount, provider_status='done',
            occurred_at=utcnow(), status=status, reason=reason, raw_sha256=digest,
            created_at=created or utcnow(), updated_at=utcnow()))
    return evidence_id


def test_unlinked_money_is_listed_apart_and_reconcile_dismisses_it_without_creating_a_grant(api):
    api.say_plain('hello')
    api.say('hi')       # the fan exists in the pilot scope
    held = evidence_row(api, status='held')
    fresh = evidence_row(api, status='pending', reason='awaiting_unlock')
    stale = evidence_row(api, status='pending', reason='awaiting_unlock', created=utcnow() - timedelta(hours=2), amount=700)
    chargeback = evidence_row(api, kind='chargeback', status='pending', created=utcnow() - timedelta(hours=2))
    unlock = evidence_row(api, kind='unlock', status='held', reason='ambiguous_pairing')
    items = {i['id']: i for i in get(api, 'unresolved').json()['items']}
    assert f'unlinked_transaction:{held}' in items and f'unlinked_transaction:{stale}' in items
    assert f'unlinked_transaction:{fresh}' not in items, 'a pending transaction still inside its matching window is not listed'
    assert f'unlinked_transaction:{chargeback}' in items
    assert 'does not automatically revoke' in items[f'unlinked_transaction:{chargeback}']['summary']
    assert f'unreconciled_access:{unlock}' in items
    for item in items.values():
        assert_dashboard_item(item)
        assert item['owner'] and item['nextStep']
    assert items[f'unlinked_transaction:{held}']['conversationId'] == conv()
    summary = items[f'unlinked_transaction:{held}']['summary']
    assert '1000' in summary and 'not attributed to a message' in summary
    intents_before = [i.intent_id for i in api.rows(OfTextIntent)]
    result = post(api, f'unresolved/unlinked_transaction:{held}/reconcile', {'commandKey': key(100), 'actor': ACTOR, 'evidenceRef': EVIDENCE})
    assert result.status_code == 200 and result.json()['outcome'] == 'dismissed' and result.json()['resolved'] is True
    with Session(api.database[1]) as s:
        row = s.get(OfFansEvidence, (ACCOUNT, held))
        assert (row.status, row.resolved_by, row.reason, row.intent_id, row.grant_id, row.peer_id) == ('dismissed', ACTOR, 'operator_reconciled', None, None, None)
    assert [i.intent_id for i in api.rows(OfTextIntent)] == intents_before
    assert f'unlinked_transaction:{held}' not in {i['id'] for i in get(api, 'unresolved').json()['items']}
    assert post(api, f'unresolved/unlinked_transaction:{held}/reconcile', {'commandKey': key(100), 'actor': ACTOR, 'evidenceRef': EVIDENCE}).json()['replayed'] is True
    assert post(api, f'unresolved/unlinked_transaction:{held}/reconcile', {'commandKey': key(101), 'actor': ACTOR, 'evidenceRef': EVIDENCE}).status_code in (404, 409)
    # a stale pending transaction may be dismissed too; a pending chargeback or a fresh transaction may not
    assert post(api, f'unresolved/unlinked_transaction:{stale}/reconcile', {'commandKey': key(102), 'actor': ACTOR, 'evidenceRef': EVIDENCE}).json()['outcome'] == 'dismissed'
    assert post(api, f'unresolved/unlinked_transaction:{chargeback}/reconcile', {'commandKey': key(103), 'actor': ACTOR, 'evidenceRef': EVIDENCE}).status_code == 409
    assert post(api, f'unresolved/unreconciled_access:{unlock}/reconcile', {'commandKey': key(104), 'actor': ACTOR, 'evidenceRef': EVIDENCE}).json()['outcome'] == 'dismissed'
    # an id of the wrong kind never touches another kind of row
    assert post(api, f'unresolved/unreconciled_access:{fresh}/reconcile', {'commandKey': key(105), 'actor': ACTOR, 'evidenceRef': EVIDENCE}).status_code == 404


def test_unscoped_or_historical_provider_evidence_is_preserved_without_leaking_details(api):
    from db.models import OfCreatorBinding
    from db.onlyfans_engine_models import OfFansConnection
    stage(api)
    unscoped = evidence_row(api, scoped=False, amount=987654)
    other_fan = evidence_row(api, fan=FAN2, amount=876543)
    with Session(api.database[1]) as s, s.begin():
        s.add(OfCreatorBinding(id='evidence-historical-binding', tenant_id='old-tenant', creator_id='old-creator',
                               account_id=ACCOUNT, channel='onlyfans', revoked_at=utcnow()))
        s.flush()
        s.add(OfFansConnection(account_id=ACCOUNT, epoch=2, binding_id='evidence-historical-binding',
                               provider_account_id='old-provider', provider_creator_id='old-creator',
                               status='retired', created_at=utcnow()))
    historical = evidence_row(api, epoch=2, amount=765432)
    current = evidence_row(api, amount=123456)
    items = get(api, 'unresolved').json()['items']
    assert any(item['kind'] == 'unscoped_provider_evidence' for item in items)
    shown = json.dumps(items)
    assert f'unlinked_transaction:{current}' in shown and '123456' in shown
    for hidden, amount in ((unscoped, 987654), (other_fan, 876543), (historical, 765432)):
        assert hidden not in shown and str(amount) not in shown
        response = post(api, f'unresolved/unlinked_transaction:{hidden}/reconcile',
                        {'commandKey':key(900 + amount), 'actor':ACTOR, 'evidenceRef':EVIDENCE})
        assert response.status_code == 404
    [draft] = drafts(api)
    assert draft['evidence'] == '1 held payment record(s) need review'
    with Session(api.database[1]) as s:
        assert all(s.get(OfFansEvidence, (ACCOUNT, ident)).status == 'held'
                   for ident in (unscoped, other_fan, historical, current))


def test_conflicting_webhook_provenance_is_not_used_for_operator_attribution(api):
    from db.onlyfans_engine_models import OfFansWebhook, OfFansWebhookConflict
    stage(api)
    evidence_id = evidence_row(api)
    with Session(api.database[1]) as s, s.begin():
        evidence = s.get(OfFansEvidence, (ACCOUNT, evidence_id))
        raw = b'{"different":true}'
        s.add(OfFansWebhookConflict(account_id=ACCOUNT, epoch=1, event_key=evidence.event_key,
                                    payload_sha256=hashlib.sha256(raw).hexdigest(), raw_body=raw, received_at=utcnow()))
    items = get(api, 'unresolved').json()['items']
    assert any(item['kind'] == 'unscoped_provider_evidence' for item in items)
    assert evidence_id not in json.dumps(items)
    assert post(api, f'unresolved/unlinked_transaction:{evidence_id}/reconcile',
                {'commandKey':key(999), 'actor':ACTOR, 'evidenceRef':EVIDENCE}).status_code == 404


def test_unscoped_and_old_human_sends_cannot_take_over_current_creator_chat(api):
    from db.models import OfCreatorBinding
    from db.onlyfans_engine_models import OfFansConnection
    stage(api)
    unscoped = evidence_row(api, kind='human_sent', status='pending', scoped=False)
    with Session(api.database[1]) as s, s.begin():
        s.add(OfCreatorBinding(id='old-human-binding', tenant_id='old-tenant', creator_id='old-creator',
                               account_id=ACCOUNT, channel='onlyfans', revoked_at=utcnow()))
        s.flush()
        s.add(OfFansConnection(account_id=ACCOUNT, epoch=2, binding_id='old-human-binding',
                               provider_account_id='old-provider', provider_creator_id='old-creator',
                               status='retired', created_at=utcnow()))
    historical = evidence_row(api, kind='human_sent', status='pending', epoch=2)
    api.intake.classify_sent(ACCOUNT, FAN)
    api.intake._takeover(ACCOUNT, FAN, historical, utcnow())
    with Session(api.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated'
        for ident in (unscoped, historical):
            row = s.get(OfFansEvidence, (ACCOUNT, ident))
            assert row.status == 'pending' and row.reason == 'creator_scope_unverified'
    items = get(api, 'unresolved').json()['items']
    assert any(item['kind'] == 'unscoped_provider_evidence' for item in items)
    assert unscoped not in json.dumps(items) and historical not in json.dumps(items)


def test_pending_historical_webhook_is_held_before_projection_into_current_creator(api):
    from db.models import Message, OfCreatorBinding
    from db.onlyfans_engine_models import OfFansConnection, OfFansWebhook
    from db.onlyfans_fans_intake_models import OfFansProjection
    stage(api)
    raw = json.dumps({'event':'messages.received', 'account_id':'old-provider', 'payload':{
        'id':989898, 'fromUser':{'id':FAN}, 'text':'old creator only',
        'createdAt':datetime.now(timezone.utc).isoformat(), 'isSentByMe':False, 'media':[], 'price':0}}).encode()
    with Session(api.database[1]) as s, s.begin():
        s.add(OfCreatorBinding(id='old-project-binding', tenant_id='old-tenant', creator_id='old-creator',
                               account_id=ACCOUNT, channel='onlyfans', revoked_at=utcnow()))
        s.flush()
        s.add(OfFansConnection(account_id=ACCOUNT, epoch=2, binding_id='old-project-binding',
                               provider_account_id='old-provider', provider_creator_id='old-creator',
                               status='retired', created_at=utcnow()))
        s.flush()
        s.add(OfFansWebhook(account_id=ACCOUNT, epoch=2, event_key='old-pending', event_type='messages.received',
                            payload_sha256=hashlib.sha256(raw).hexdigest(), raw_body=raw,
                            status='accepted', received_at=utcnow()))
        s.flush()
        s.add(OfFansProjection(account_id=ACCOUNT, epoch=2, event_key='old-pending', status='pending',
                               attempts=0, updated_at=utcnow()))
    assert api.intake._project(ACCOUNT, 2, 'old-pending') == 'held'
    with Session(api.database[1]) as s:
        assert s.get(Message, (ACCOUNT, FAN, 989898)) is None
        row = s.get(OfFansProjection, (ACCOUNT, 2, 'old-pending'))
        assert (row.status, row.detail) == ('held', 'historical_creator_binding')
        assert s.get(OfFansWebhook, (ACCOUNT, 2, 'old-pending')).raw_body == raw
    assert any(item['kind'] == 'unscoped_provider_evidence' for item in get(api, 'unresolved').json()['items'])


def test_previously_linked_unlock_requires_current_webhook_lineage_when_settled(api, monkeypatch):
    from fans_api_adapter.payment_evidence import settle_unlock
    from test_engine_real_worker_pg import _sell, unlock
    _, message_id = _sell(api)
    assert api.send(unlock(message_id), key='stale-linked-unlock')['outcome'] == 'evidence'
    with Session(api.database[1]) as s, s.begin():
        [row] = list(s.scalars(select(OfFansEvidence).where(OfFansEvidence.kind == 'unlock')))
        assert row.reason == 'awaiting_commerce' and row.intent_id is not None
        row.event_key = None
        evidence_id = row.evidence_id
    with monkeypatch.context() as worker:
        worker.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
        assert settle_unlock(api.store, ACCOUNT, evidence_id, utcnow()) == 'held'
    with Session(api.database[1]) as s:
        assert list(s.scalars(select(OfEngineAccessEvidence))) == []
        assert s.get(OfFansEvidence, (ACCOUNT, evidence_id)).status == 'held'


def test_handover_requests_are_listed_and_cannot_be_reconciled_here(api):
    staged, mid = stage(api)
    request = api.rows(OfEngineRequest)[0]
    sent = execute(api.store, TENANT, ACCOUNT, request.request_id, action='request_handover', expected_revision=request.revision,
                   command_key=key(110), actor=ACTOR, target=ACTOR, expected_fan_id=FAN)
    assert sent['status'] == 'handover_pending'
    [item] = [i for i in get(api, 'unresolved').json()['items'] if i['kind'] == 'handover_request']
    assert_dashboard_item(item)
    assert item['id'] == f'handover_request:{request.request_id}' and item['owner'] == 'human' and item['nextStep'] == 'accept_handover'
    assert item['conversationId'] == conv() and item['requestRevision'] == sent['recordedRevision']
    held = post(api, f"unresolved/{item['id']}/reconcile", {'commandKey': key(111), 'actor': ACTOR, 'evidenceRef': EVIDENCE})
    assert held.status_code == 409
    assert api.rows(OfEngineRequest)[0].status == 'handover_pending'
    hold = post(api, f'unresolved/human_hold:{FAN}/reconcile', {'commandKey': key(112), 'actor': ACTOR, 'evidenceRef': EVIDENCE})
    assert hold.status_code == 409


# ======================================================================== v1 operator workflow for the real pilot
def request_of(api):
    request = api.rows(OfEngineRequest)[0]
    return request.request_id, request.revision


def v1(api, request_id, revision, action, n, *, actor=ACTOR, target=None, evidence=None):
    return execute(api.store, TENANT, ACCOUNT, request_id, action=action, expected_revision=revision, command_key=key(n),
                   actor=actor, target=target, evidence_ref=evidence, expected_fan_id=FAN)


def test_fansapi_operator_workflow_needs_a_human_and_explicit_fansapi_evidence(api):
    staged, mid = stage(api)
    request_id, rev = request_of(api)
    for bad in ('engine', 'offline-engine', 'system', 'operator-jane', ''):
        with pytest.raises(Refused):
            v1(api, request_id, rev, 'request_handover', 120, actor=bad, target=ACTOR)
    sent = v1(api, request_id, rev, 'request_handover', 121, target=ACTOR)
    assert sent['status'] == 'handover_pending' and v1(api, request_id, rev, 'request_handover', 121, target=ACTOR)['replayed'] is True
    with pytest.raises(Refused, match='acceptance'):
        v1(api, request_id, sent['recordedRevision'], 'accept_handover', 122)
    api.store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=revision(api), command_key='human-take',
                      actor=ACTOR, reason='Take responsibility', action='request_takeover')
    accepted = v1(api, request_id, sent['recordedRevision'], 'accept_handover', 123)
    assert accepted['status'] == 'handover_accepted' and accepted['owner'] == ACTOR
    rev = accepted['recordedRevision']
    for evidence in ('synthetic:review', 'operator:fansapi:abc', 'operator:fansapi:' + 'x' * 109, 'operator:other:checked it', None, ''):
        with pytest.raises(Refused):
            v1(api, request_id, rev, 'complete', 124, evidence=evidence)
    with pytest.raises(Refused):
        v1(api, request_id, rev, 'complete', 125, actor=OTHER, evidence=EVIDENCE)           # only the accepting human
    done = v1(api, request_id, rev, 'complete', 126, evidence=EVIDENCE)
    assert done['status'] == 'completed' and done['nextStep'] == 'none'
    assert [d.status for d in api.rows(OfEngineDecision)] == ['cancelled']
    assert [r.status for r in approvals(api)] == ['invalidated'] and api.fans.posts == []
    from db.onlyfans_engine_models import OfEngineOperatorCommand
    with Session(api.database[1]) as s:
        assert s.get(OfEngineOperatorCommand, (ACCOUNT, key(126))).evidence_ref == EVIDENCE


def test_fansapi_decline_with_evidence_cancels_the_draft(api):
    staged, mid = stage(api)
    request_id, rev = request_of(api)
    sent = v1(api, request_id, rev, 'request_handover', 130, target=ACTOR)
    api.store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=revision(api), command_key='human-take-2',
                      actor=ACTOR, reason='Take responsibility', action='request_takeover')
    accepted = v1(api, request_id, sent['recordedRevision'], 'accept_handover', 131)
    done = v1(api, request_id, accepted['recordedRevision'], 'decline', 132, evidence='operator:fansapi:declined after reading the chat')
    assert done['status'] == 'declined'
    assert send(api, staged)['outcome'] != 'accepted' and api.fans.posts == []


def test_completing_a_real_offer_still_needs_confirmed_access(api):
    staged, grant = offer_draft(api)
    request_id, rev = request_of(api)
    sent = v1(api, request_id, rev, 'request_handover', 140, target=ACTOR)
    api.store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=revision(api), command_key='human-take-3',
                      actor=ACTOR, reason='Take responsibility', action='request_takeover')
    accepted = v1(api, request_id, sent['recordedRevision'], 'accept_handover', 141)
    with pytest.raises(Refused, match='offer access is not confirmed'):
        v1(api, request_id, accepted['recordedRevision'], 'complete', 142, evidence=EVIDENCE)
    assert api.rows(OfEngineRequest)[0].status == 'handover_accepted'


def test_unknown_delivery_still_blocks_completion_for_the_real_pilot(api):
    intent_id, staged = unknown_send(api)
    request_id, rev = request_of(api)
    sent = v1(api, request_id, rev, 'request_handover', 150, target=ACTOR)
    api.store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=revision(api), command_key='human-take-4',
                      actor=ACTOR, reason='Take responsibility', action='request_takeover')
    accepted = v1(api, request_id, sent['recordedRevision'], 'accept_handover', 151)
    with pytest.raises(Refused, match='unresolved delivery'):
        v1(api, request_id, accepted['recordedRevision'], 'complete', 152, evidence=EVIDENCE)
    assert api.rows(OfTextIntent)[0].status == 'unknown'


def test_synthetic_accounts_keep_synthetic_evidence_only(database):
    store, authority, engine, decision, request_id, rev = synthetic_setup(database)
    sent = synthetic_command(store, request_id, rev, 'request_handover', '1', target=ACTOR)
    store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=0, command_key='human-takeover',
                  actor=ACTOR, reason='Take responsibility', action='request_takeover')
    accepted = synthetic_command(store, request_id, sent['recordedRevision'], 'accept_handover', '2')
    for evidence in ('operator:fansapi:checked the creator chat', 'operator:synthetic:x', 'review', None):
        with pytest.raises(Refused):
            synthetic_command(store, request_id, accepted['recordedRevision'], 'complete', '3', evidence=evidence)
    done = synthetic_command(store, request_id, accepted['recordedRevision'], 'complete', '3', evidence='synthetic:review')
    assert done['status'] == 'completed'
    store.close()


def test_no_automatic_approval_or_handback_in_the_new_surface():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / 'conversation_engine' / 'operator_v2.py').read_text() + (root / 'onlyfans_commands' / 'engine_approval_api.py').read_text()
    # approve and release_takeover are reachable only from an explicit, named-operator command function
    assert text.count('approval.approve(') == 1 and text.count("'release_takeover'") == 1
    assert 'create_draft_approval' not in text and 'send_approved' not in text and 'authorize_send' not in text
