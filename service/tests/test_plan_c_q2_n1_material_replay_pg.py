"""Q2 journey K (N1): a validly signed MATERIAL event replayed under changed transport keys.

Text-only autonomous profile (stopped_text / text_no_offers), two fans, signed HTTP intake -> normal worker ticks.
Real PostgreSQL; fake model; fake FansAPI provider. Nothing is seeded: every state change is made by a signed
event, the audited operator resolution, the guarded provider verification or the operator release.

The signature covers the body only. The X-OFAPI idempotency key is transport metadata, so a captured body can be
resent under any key. The tests assert what must hold whatever the product decides about replay semantics:
no second retire/rebind of a connection, no deleted or rewritten evidence, no send and no model call while a
hold stands, no spill onto the other fan, and a genuinely new event still takes effect.

OPEN PRODUCT DECISIONS (characterised below, deliberately NOT guessed):
  D1. Same signed tip body (same provider tip id) resent under a new key AFTER the audited review of the first
      copy: today it is a new delivery, it re-holds that fan and needs a second audited review.
  D2. Same signed accounts.otp_code_required body resent under a new key AFTER provider verification and the
      operator release: today it blocks sending again and needs a new verification + release.
  Both are fail-closed (never a send), neither retires or rebinds anything. Options: keep (a replay costs a review),
  or treat an exact already-reviewed body (+ provider tip id) in the same epoch as already classified. The second
  option would also swallow a genuinely repeated body-identical challenge (empty payload), so it is the owner's call.
"""
import hashlib
import hmac
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conversation_engine import operator_v2
from conversation_engine.pilot_scope import ProviderRequestGuard
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import (OfEnginePilotReconnectReceipt, OfEnginePilotWebhookDigest, OfFansConnection,
                                       OfFansWebhook)
from db.onlyfans_fans_intake_models import OfFansLink, OfFansProjection
from fans_api_adapter.auth_recovery import _recover_reconnected_evidence, recover_current_challenge
from fans_api_adapter.https_transport import FansHttpsTransport
from fans_api_adapter.protocol import FansApiClient
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused
from plan_c_worker import create_app
from test_engine_real_worker_pg import FAN2, PROVIDER, CREATOR, SECRET, answer, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_scope_pg import ACTOR
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401
from test_plan_c_events_stage3_pg import K, ev, rig, tip_event  # noqa: F401

REPLAYS = 3


def snap(rig):
    """Everything the replay could retire, rebind or erase, plus the wire/model counters."""
    with Session(rig.w.database[1]) as s:
        link = s.get(OfFansLink, (ACCOUNT, str(CREATOR)))
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        return {
            'connections': sorted((c.epoch, c.status, c.provider_account_id, c.provider_creator_id)
                                  for c in s.scalars(select(OfFansConnection))),
            'link': (link.state, link.block_reason, link.send_blocked, link.reconnect_count, link.session_refreshes),
            'policy': (policy.enabled, policy.revision, policy.limit_units),
            'webhooks': {(w.epoch, w.event_key): w.payload_sha256 for w in s.scalars(select(OfFansWebhook))},
            'projections': {(p.epoch, p.event_key): (p.status, p.detail) for p in s.scalars(select(OfFansProjection))},
            'digests': {(d.epoch, d.event_key, d.payload_sha256): d.safety_hold
                        for d in s.scalars(select(OfEnginePilotWebhookDigest))},
            'receipts': {(r.epoch, r.event_key, r.payload_sha256): bytes(r.raw_body)
                         for r in s.scalars(select(OfEnginePilotReconnectReceipt))},
            'commands': {(c.command_key, c.kind): (c.request_hash, c.result_json)
                         for c in s.scalars(select(OfLaunchCommand))},
            'posts': len(rig.w.fans.posts), 'model_calls': len(rig.seen),
        }


def assert_evidence_only_grew(before, after):
    for table in ('webhooks', 'projections', 'digests', 'receipts', 'commands'):
        for key, value in before[table].items():
            assert after[table].get(key) == value, f'{table} {key} was erased or rewritten by a replay'


def assert_state_unchanged(before, after):
    for part in ('connections', 'link', 'policy'):
        assert after[part] == before[part], f'{part} changed by a replay'


def replay(rig, body, raw, prefix, n=REPLAYS):
    for i in range(n):
        assert rig.post(body, key=f'{prefix}-{i}', raw=raw) == raw


def swap_pilot(rig, transport):
    """Cold-restart the worker object on the same durable state (the new account's credentials file is rewritten)."""
    rig.client.__exit__(None, None, None)
    rig.pilot = build_supervised_pilot(rig.w.store, rig.env, model_transport=rig.model, fans_transport=transport)
    rig.client = TestClient(create_app(rig.pilot, run_loop=False))
    rig.client.__enter__()


# ----------------------------------------------------------------------------------------------- same epoch
@pytest.mark.parametrize('name,payload', [
    ('accounts.disconnected', {'onlyfans_id': CREATOR}),
    ('accounts.authentication_failed', {'onlyfans_id': CREATOR, 'onlyfans_username': 'x', 'state': 'failed'}),
])
def test_replayed_disconnect_or_auth_failure_under_fresh_keys_is_one_effect_and_erases_nothing(rig, name, payload):
    rig.fans_in()
    rig.ticks()
    assert len(rig.w.fans.posts) == 2 and len(rig.seen) == 2, 'control: both fans answered before the event'
    body = ev(name, payload)
    raw = rig.post(body, key='material-0')
    with pytest.raises(Refused):                # the first delivery really took effect: sending is disabled
        rig.pilot.tick()
    first = snap(rig)
    assert first['policy'][0] is False and first['link'][2] is True
    assert first['link'][0] == ('disconnected' if name.endswith('disconnected') else 'auth_failed')
    assert first['connections'][0][1] == ('retired' if name.endswith('disconnected') else 'active')

    replay(rig, body, raw, 'material-replay')
    rig.post(inbound(78001, 'are you still there?', fan=FAN), key='after-replay-a')
    rig.post(inbound(78002, 'hello?', fan=FAN2), key='after-replay-b')
    with pytest.raises(Refused):
        rig.pilot.tick()
    after = snap(rig)
    assert_state_unchanged(first, after)
    assert_evidence_only_grew(first, after)
    assert {k for k in after['webhooks'] if k[1].startswith('material-replay')} == {(1, f'material-replay-{i}') for i in range(REPLAYS)}, \
        'every authenticated replay is retained as evidence'
    assert after['posts'] == first['posts'] and after['model_calls'] == first['model_calls'], 'no send, no model call'
    assert rig.block(FAN) == rig.block(FAN2) == 'pilot_provider_link_blocked'

    # Control: a genuinely new material event (a different tip) still takes effect.
    rig.post(tip_event(FAN2, tid='genuinely-new-tip'), key='genuinely-new')
    new = snap(rig)
    assert [h for (e, k, d), h in new['digests'].items() if k == 'genuinely-new'] == [True]
    assert_evidence_only_grew(after, new)


# ----------------------------------------------------------------------------------------------- old epoch
def test_replay_of_old_epoch_disconnect_challenge_and_reconnect_cannot_retire_or_rebind_the_recovered_connection(rig):
    rig.fans_in()
    rig.ticks()
    dis = ev('accounts.disconnected', {'onlyfans_id': CREATOR})
    dis_raw = rig.post(dis, key='old-disconnect')
    otp = ev('accounts.otp_code_required', {})
    new = {'event': 'accounts.reconnected', 'account_id': 'acct_replacement',
           'payload': {'onlyfans_id': str(CREATOR), 'state': 'authenticated', 'latestAuthAttempt': {'success': True}}}
    new_raw = rig.post(new, key='replacement-reconnect')
    held = snap(rig)
    assert held['connections'] == [(1, 'retired', PROVIDER, str(CREATOR))], 'a signed new-account reconnect alone rebinds nothing'
    assert held['digests'][(1, 'replacement-reconnect', hashlib.sha256(new_raw).hexdigest())] is True

    # Normal path: guarded provider verification of the exact retained reconnect, then the operator release.
    guard = ProviderRequestGuard(rig.w.store, TENANT, ACCOUNT, rig.pilot.scope_revision)
    wire = FansHttpsTransport(enabled=True, transport=httpx.MockTransport(lambda req: answer(
        200, [{'id': 'acct_replacement', 'onlyfans_id': CREATOR, 'is_authenticated': True}])))
    client = FansApiClient(account='acct_replacement', creator_id=CREATOR, epoch=1, token='fixture-token', http=wire,
                           timeout=5, enabled=True, request_guard=guard)
    result = _recover_reconnected_evidence(
        rig.w.store, TENANT, ACCOUNT, client, expected_revision=rig.pilot.scope_revision, event_epoch=1,
        event_key='replacement-reconnect', raw_body=new_raw, signature=hmac.new(SECRET, new_raw, hashlib.sha256).hexdigest(),
        secret=SECRET, command_key='c' * 64, actor=ACTOR, reason='verified replacement account')
    assert result['connectionEpoch'] == 2 and result['sendBlocked']
    rig.pilot.worker.intake.release_send_block(TENANT, ACCOUNT, str(CREATOR), actor=ACTOR, reason='operator release')
    creds = Path(rig.env['TWIN_ENGINE_FANSAPI_ENV_FILE'])
    creds.write_text('FANSAPI_KEY=fixture-key\nFANSAPI_ACCT=acct_replacement\n')
    creds.chmod(0o600)

    def provider(req):
        if req.method == 'GET' and req.url.path == '/api/accounts':
            return answer(200, [{'id': 'acct_replacement', 'onlyfans_id': CREATOR, 'is_authenticated': True}])
        return rig.w.fans.handle(req)
    swap_pilot(rig, httpx.MockTransport(provider))
    rig.post(inbound(78101, 'hello again', fan=FAN), key='recovered-in')
    rig.ticks()
    healthy = snap(rig)
    assert healthy['connections'] == [(1, 'retired', PROVIDER, str(CREATOR)), (2, 'active', 'acct_replacement', str(CREATOR))]
    assert healthy['link'][:3] == ('connected', None, False) and healthy['policy'][0] is True
    assert healthy['posts'] == 3, 'control: the recovered connection sends'

    replay(rig, dis, dis_raw, 'old-disconnect-replay')          # old acct_ id, old epoch, valid signature
    rig.post(otp, key='old-otp-replay')                          # old challenge, no account in body but the old one
    replay(rig, new, new_raw, 'new-reconnect-replay')            # the reconnect that created epoch 2
    after = snap(rig)
    assert_state_unchanged(healthy, after)
    assert_evidence_only_grew(healthy, after)
    assert [after['projections'][(1, f'old-disconnect-replay-{i}')] for i in range(REPLAYS)] == [('ignored', 'superseded_account')] * REPLAYS
    assert after['projections'][(1, 'old-otp-replay')] == ('ignored', 'superseded_account')
    rig.post(inbound(78102, 'still there?', fan=FAN2), key='after-old-replays')
    rig.ticks()
    assert len(rig.w.fans.posts) == healthy['posts'] + 1 and len(rig.seen) == healthy['model_calls'] + 1, \
        'replays of old-epoch events do not stop the recovered connection from working'
    assert rig.block(FAN) is None and rig.block(FAN2) is None

    # Control: a genuinely new disconnect of the CURRENT account still retires it (once) and stops sending.
    current = ev('accounts.disconnected', {'onlyfans_id': CREATOR})
    current['account_id'] = 'acct_replacement'
    rig.post(current, key='genuine-new-disconnect')
    final = snap(rig)
    assert final['connections'][1] == (2, 'retired', 'acct_replacement', str(CREATOR))
    assert final['link'][0] == 'disconnected' and final['policy'][0] is False
    assert_evidence_only_grew(after, final)


# ----------------------------------------------------------------------------------------------- D1 (open)
def test_replayed_reviewed_tip_under_fresh_keys_is_fail_closed_loses_no_evidence_and_leaves_other_fan_alone(rig):
    """CHARACTERISATION of open decision D1: today an exact reviewed tip body resent under a new key re-holds that fan."""
    tip = tip_event(FAN)
    raw = rig.post(tip, key='tip-original')
    rig.fans_in()
    rig.ticks()
    [item] = rig.items('provider_event')
    assert rig.reconcile(item, 801, raw=raw)['resolved']
    rig.ticks()
    assert len(rig.w.fans.posts) == 2 and rig.block(FAN) is None, 'control: reviewed once, fan resumed'
    reviewed = snap(rig)

    replay(rig, tip, raw, 'tip-replay', n=2)
    rig.post(inbound(78201, 'hey again', fan=FAN), key='tip-after-a')
    rig.post(inbound(78202, 'hey you', fan=FAN2), key='tip-after-b')
    rig.ticks()
    held = snap(rig)
    assert_evidence_only_grew(reviewed, held)
    assert held['receipts'][(1, 'tip-original', hashlib.sha256(raw).hexdigest())] == raw, 'original retained bytes intact'
    assert_state_unchanged(reviewed, held)                       # no connection/link/policy change at all
    assert rig.block(FAN) == 'pilot_evidence_pending' and rig.block(FAN2) is None, 'decision D1: re-held, scoped to that fan'
    assert held['posts'] == reviewed['posts'] + 1 and held['model_calls'] == reviewed['model_calls'] + 1, \
        'only the other fan was answered; the held fan got no send and no model call'
    items = sorted(rig.items('provider_event'), key=lambda i: i['eventKey'])
    assert [i['eventKey'] for i in items] == ['tip-replay-0', 'tip-replay-1']
    for n, entry in enumerate(items):                            # each replay is released only by the audited path
        assert rig.block(FAN) == 'pilot_evidence_pending'
        assert rig.reconcile(entry, 810 + n, raw=raw)['resolved']
    rig.ticks()
    assert rig.block(FAN) is None and rig.requests() == {FAN: 'completed', FAN2: 'completed'}
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfLaunchCommand).where(
            OfLaunchCommand.kind == 'plan_c_webhook_resolution')) == 3, 'the first review record is kept, two more added'

    # Control: a genuinely new tip (new provider tip id) holds its fan.
    rig.post(tip_event(FAN2, tid='t-genuinely-new'), key='tip-new')
    assert rig.block(FAN2) == 'pilot_evidence_pending'


# ----------------------------------------------------------------------------------------------- D2 (open)
def test_replayed_challenge_after_provider_verification_blocks_again_without_rebind_or_evidence_loss(rig):
    """CHARACTERISATION of open decision D2: an exact old otp challenge body under a new key re-blocks sending."""
    rig.fans_in()
    rig.ticks()
    body = ev('accounts.otp_code_required', {})
    raw = rig.post(body, key='otp-original')
    with pytest.raises(Refused):
        rig.pilot.tick()

    def verify(command):
        guard = ProviderRequestGuard(rig.w.store, TENANT, ACCOUNT, rig.pilot.scope_revision)
        wire = FansHttpsTransport(enabled=True, transport=httpx.MockTransport(lambda req: answer(
            200, [{'id': PROVIDER, 'onlyfans_id': CREATOR, 'is_authenticated': True}])))
        client = FansApiClient(account=PROVIDER, creator_id=CREATOR, epoch=1, token='fixture-token', http=wire,
                               timeout=5, enabled=True, request_guard=guard)
        out = recover_current_challenge(rig.w.store, TENANT, ACCOUNT, client, expected_revision=rig.pilot.scope_revision,
                                        command_key=command * 64, actor=ACTOR, reason='verified provider identity')
        assert out['status'] == 'provider_verified' and out['sendBlocked'] and not out['replayed']
        rig.pilot.worker.intake.release_send_block(TENANT, ACCOUNT, str(CREATOR), actor=ACTOR, reason='operator release')

    verify('d')
    rig.post(inbound(78301, 'hi', fan=FAN), key='otp-recovered-in')
    rig.ticks()
    recovered = snap(rig)
    assert recovered['link'][:3] == ('connected', None, False) and recovered['posts'] == 3, 'control: verified and released'

    replay(rig, body, raw, 'otp-replay', n=2)
    rig.post(inbound(78302, 'anyone?', fan=FAN2), key='otp-after')
    with pytest.raises(Refused):
        rig.pilot.tick()
    blocked = snap(rig)
    assert blocked['connections'] == recovered['connections'], 'no retire, no rebind, no new epoch'
    assert blocked['link'][:3] == ('auth_failed', 'auth_challenge', True) and blocked['policy'][0] is False, 'decision D2: blocked again'
    assert blocked['posts'] == recovered['posts'] and blocked['model_calls'] == recovered['model_calls']
    assert_evidence_only_grew(recovered, blocked)
    assert rig.block(FAN) == rig.block(FAN2) == 'pilot_provider_link_blocked'
    # It is released only by a new guarded provider verification plus an operator release, never by another event.
    rig.post(ev('messages.unread_count_updated', {'count': 1}), key='otp-counter')
    assert rig.block(FAN) == 'pilot_provider_link_blocked'
    verify('e')
    rig.ticks()
    final = snap(rig)
    assert final['connections'] == recovered['connections'] and final['posts'] == recovered['posts'] + 1
    assert_evidence_only_grew(blocked, final)
