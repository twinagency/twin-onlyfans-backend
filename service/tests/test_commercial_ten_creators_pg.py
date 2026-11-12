"""Step 6 / Q12: TEN distinct commercial creators in ONE database through the real worker path.

Every creator has a fan 401 and a fan 402, the same vault media ids (31, 41), the same product names ('Beach photo', 'Pool video'),
the same fake-provider message ids (7001, 7002) and the same event shapes. What differs is only what must differ: the creator's own
provider account and credential, facts, prices, budgets and controls. Five creators are healthy; five are degraded in five different
ways (model 429, one fan taken over, scope stopped, emergency stop, delivery lane busy). Nothing is seeded: grants come from ordinary
preparation, ownership from the exact unlock, budgets from each scope row.
"""
import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from commercial_worker_fixture import FAN_A, FAN_B, OPERATOR, build_world, key
from conversation_engine import approval, pilot_scope
from conversation_engine.ownership_gate import owned_state
from conversation_engine.writer_context import set_active_profiles
from db.models import OfConversationState
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineModelCall, OfEngineOfferGrant
from onlyfans_runtime.launch_operations import account_state, control_account
from onlyfans_runtime.store import Refused
from test_commercial_matrix_pg import Clock, Scripted, approve_live, card_to, drain, grants, model_calls  # noqa: F401
from test_commercial_preparation_pg import TENANT
from test_onlyfans_foundation_pg import database  # noqa: F401

HEALTHY, RATE_LIMITED, TAKEN_OVER, STOPPED, EMERGENCY, BUSY = (1, 2, 3, 4, 10), 5, 6, 7, 8, 9
BEACH, POOL = lambda i: 1000 + 100 * i, lambda i: 2000 + 100 * i                     # cents, distinct per creator


def attempt(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except Refused as exc:
        return {'refused': str(exc)}


def city_writer(i):
    def choose(latest, prompt, approved, proposal):
        if 'where are you from' in latest.lower():
            return {'body': 'I am from ' + ' and '.join(sorted(set(re.findall(r'City\d+', prompt)))) + '.'}
        return proposal
    return choose


def live(w, fan):
    return [a for a in approval.pending(w.store, TENANT, w.account, fan) if a['status'] == 'awaiting']


def own_cards(w, fan):
    return card_to(w, fan)


def test_q12_ten_creators_with_identical_ids_stay_isolated_and_a_degraded_creator_never_starves_the_rest(database, tmp_path, monkeypatch):
    fleet = {}
    try:
        for i in range(1, 11):
            folder = tmp_path / f'creator{i}'
            folder.mkdir()
            w = build_world(database, tmp_path, monkeypatch, i=i, fans=(FAN_A, FAN_B), folder=folder, choose=city_writer(i))
            (folder / 'fans-creds').write_text(f'FANSAPI_KEY=fixture-key-{i}\nFANSAPI_ACCT={w.provider}\n')
            w.product('Beach photo', [31], standard=BEACH(i), description='Beach photo, one picture.')
            w.product('Pool video', [41], standard=POOL(i), kinds={41: 'video'}, description='Pool video, one clip.')
            if i == RATE_LIMITED:
                w.scripted = Scripted(w, ['ok', (429, {'Retry-After': '30'})])
                w.model_transport = w.scripted.transport
            w.launch(facts=(('home_city', f'City{i}'),), limits={'send_units': 100 + i, 'daily_send_units': 50 + i})
            fleet[i] = w
        assert len({w.account for w in fleet.values()}) == len({w.provider for w in fleet.values()}) == len({w.creator for w in fleet.values()}) == 10
        clock = Clock().govern(fleet[RATE_LIMITED].pilot)

        # ---- 1. every creator answers the same factual question from ITS OWN facts
        for i, w in fleet.items():
            staged, mid = w.say('where are you from?', FAN_A)
            w.approve_turn(staged)
            w.unthrottle()
            w.pilot.tick()
            bodies = [p['body']['text'] for p in w.fans_mock.posts]
            assert bodies == [f'I am from City{i}.'], (i, bodies)
            assert all(set(re.findall(r'City\d+', p)) <= {f'City{i}'} for p in w.model_prompts), i

        # ---- 2. controls: one creator each is degraded; identical fan ids everywhere
        w6 = fleet[TAKEN_OVER]
        with Session(database[1]) as s:
            revision = s.get(OfConversationState, (w6.account, FAN_A)).revision
        w6.store.control(TENANT, w6.account, FAN_A, mode='human_controlled', expected_revision=revision, command_key='q12-takeover',
                         actor=OPERATOR, reason='staff takeover of one fan', action='request_takeover')
        w7 = fleet[STOPPED]
        pilot_scope.set_status(w7.store, TENANT, w7.account, 'stopped', expected_revision=w7.scope_revision, command_key=key(0xA701),
                               actor=OPERATOR, reason='operator stops one creator')
        w8 = fleet[EMERGENCY]
        with Session(database[1], expire_on_commit=False) as s, s.begin():
            fingerprint = account_state(s, w8.store._runtime(s, TENANT, w8.account))['fingerprint']
        control_account(w8.store, TENANT, w8.account, action='emergency_stop', command_key=key(0xA801), actor=OPERATOR,
                        reason='emergency stop of one creator', expected_fingerprint=fingerprint)

        # ---- 3. the SAME asks in all ten creators (same fan ids, same words, same media ids)
        staged = {}
        for i, w in fleet.items():
            for fan, ask in ((FAN_A, 'show me the beach photo'), (FAN_B, 'send me the pool video')):
                result = attempt(w.say, ask, fan)
                staged[(i, fan)] = result
        for i, w in fleet.items():
            for fan in (FAN_A, FAN_B):
                for a in live(w, fan):
                    attempt(approval.approve, w.store, TENANT, w.account, a['requestId'], a['generation'], expected_version=a['version'], actor=OPERATOR)
        for w in fleet.values():
            w.unthrottle()
        with database[1].begin() as c:                                               # the busy creator's delivery lane is not free
            c.execute(text("UPDATE of_delivery_policy SET next_dispatch_at=CURRENT_TIMESTAMP+INTERVAL '1 hour' WHERE account_id=:a"),
                      {'a': fleet[BUSY].account})
        for _ in range(3):                                                           # one scheduler per creator, round robin
            for i, w in fleet.items():
                attempt(w.pilot.tick)

        # ---- 4. healthy creators: two exact cards each, at THEIR price, through THEIR provider path and credential
        for i in HEALTHY:
            w = fleet[i]
            a, b = own_cards(w, FAN_A), own_cards(w, FAN_B)
            assert [c['body']['price'] for c in a] == [BEACH(i) / 100] and a[0]['body']['mediaFiles'] == ['31'], i
            assert [c['body']['price'] for c in b] == [POOL(i) / 100] and b[0]['body']['mediaFiles'] == ['41'], i
            assert f'USD {BEACH(i) / 100:.2f}' in a[0]['body']['text']
            assert all(p['path'].startswith(f'/api/{w.provider}/') and p['auth'] == f'Bearer fixture-key-{i}' for p in w.fans_mock.posts), i
            paid_intents=w.rows(OfTextIntent, OfTextIntent.price_cents > 0)
            assert all(x.provider_message_id is not None for x in paid_intents), [(i,x.status,x.reason,x.provider_message_id,x.wire_started_at) for x in paid_intents]
            assert sorted(x.provider_message_id for x in paid_intents) == [7002, 7003], i   # ids repeat fleet-wide
        # ---- 5. degraded creators hold exactly what they should and nothing else
        assert own_cards(fleet[RATE_LIMITED], FAN_A) == [] and fleet[RATE_LIMITED].scripted.calls[:2] == ['ok', 429]
        assert own_cards(fleet[TAKEN_OVER], FAN_A) == [] and len(own_cards(fleet[TAKEN_OVER], FAN_B)) == 1   # one fan held, the other served
        assert fleet[STOPPED].fans_mock.posts[1:] == [] and fleet[EMERGENCY].fans_mock.posts[1:] == []
        assert own_cards(fleet[BUSY], FAN_A) == [] and own_cards(fleet[BUSY], FAN_B) == []                # not sent yet, nothing lost

        # ---- 6. the exact unlock of creator 3's card (provider message 7002, fan 401) belongs to creator 3 alone
        w3, w4 = fleet[3], fleet[4]
        sent3 = next(x for x in w3.rows(OfTextIntent, OfTextIntent.price_cents > 0, OfTextIntent.fan_id == FAN_A))
        event = w3.unlock_event(sent3.provider_message_id, BEACH(3) / 100)
        w3.deliver(event, 'q12-unlock-3')
        w3.pilot.tick()
        attempt(w4.deliver, event, 'q12-unlock-3')                                    # creator 3's webhook offered to creator 4
        wrong_price = w4.unlock_event(sent3.provider_message_id, BEACH(3) / 100)      # same message id and fan, creator 3's amount
        attempt(w4.deliver, wrong_price, 'q12-unlock-4')
        w4.pilot.tick()
        with Session(database[1]) as s:
            owned = {i: owned_state(s, fleet[i].account, FAN_A) for i in fleet}
            proof = {i: s.scalar(select(func.count()).select_from(OfEngineAccessEvidence).where(OfEngineAccessEvidence.account_id == fleet[i].account))
                     for i in fleet}
        assert owned[3] == ({31}, None) and proof[3] == 1
        assert all(owned[i][0] == set() and proof[i] == 0 for i in fleet if i != 3), (owned, proof)

        # ---- 7. grants, budgets and ledgers are per creator
        with Session(database[1]) as s:
            grant_counts = dict(s.execute(select(OfEngineOfferGrant.account_id, func.count()).group_by(OfEngineOfferGrant.account_id)).all())
            calls = dict(s.execute(select(OfEngineModelCall.account_id, func.count()).group_by(OfEngineModelCall.account_id)).all())
        for i in HEALTHY:
            assert grant_counts[fleet[i].account] == 2 and calls[fleet[i].account] == 3, i
        limits = {i: pilot_scope.describe(w.store, TENANT, w.account)['config']['limits']['send_units'] for i, w in fleet.items()}
        assert limits == {i: 100 + i for i in fleet}
        used = {i: w.rows(OfTextIntent, OfTextIntent.status == 'accepted') for i, w in fleet.items()}
        assert all(len(used[i]) == 3 for i in HEALTHY) and len(used[STOPPED]) == 1 and len(used[EMERGENCY]) == 1

        # ---- 8. recovery of the degraded creators never needed a different creator to move
        before = {i: len(fleet[i].fans_mock.posts) for i in HEALTHY}
        clock.offset = timedelta(seconds=40)
        for _ in range(3):
            attempt(fleet[RATE_LIMITED].pilot.tick)
        w5 = fleet[RATE_LIMITED]
        for a in live(w5, FAN_A):
            approval.approve(w5.store, TENANT, w5.account, a['requestId'], a['generation'], expected_version=a['version'], actor=OPERATOR)
        with database[1].begin() as c:
            c.execute(text("UPDATE of_delivery_policy SET next_dispatch_at=CURRENT_TIMESTAMP-INTERVAL '1 second'"))
        for _ in range(3):
            for i in (RATE_LIMITED, BUSY):
                attempt(fleet[i].pilot.tick)
        assert [c['body']['price'] for c in own_cards(w5, FAN_A)] == [BEACH(RATE_LIMITED) / 100]
        assert {i: len(fleet[i].fans_mock.posts) for i in HEALTHY} == before, 'nothing extra was sent for anyone else'
        w9 = fleet[BUSY]
        assert [c['body']['price'] for c in own_cards(w9, FAN_A)] == [BEACH(BUSY) / 100] and len(own_cards(w9, FAN_B)) <= 1
        assert len(grants(w9, FAN_A)) == 1 and len(grants(fleet[1], FAN_A)) == 1
    finally:
        set_active_profiles(None)
        for w in fleet.values():
            w.store.close()
