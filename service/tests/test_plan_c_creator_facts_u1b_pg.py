"""U1b: unsupported creator facts through the normal path: signed intake -> runtime -> judge -> authorization -> pilot tick ->
simulated provider. The approved profile documents age, job (barista) and home city only. Disposable PG, synthetic providers.

The unsupported text must never reach the provider; the existing bounded correction then applies and a different, truthful
reply may be sent. A documented fact is delivered unchanged with no correction.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from model_transport.pilot_supervised import build_supervised_pilot
from plan_c_worker import create_app
from test_engine_real_worker_pg import world, inbound
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN
from test_plan_c_draft_app_pg import configured_draft as base_draft, signed
from test_plan_c_text_activation_pg import stopped_text, start

TRUTHFUL = 'Good to hear from you.'


@pytest.fixture
def configured_draft(base_draft):
    """The same fixture, but the approved profile documents her age, job and city (the pilot's creator facts)."""
    _world, env, _transport, _seen = base_draft
    path = env['ENGINE_MODEL_PROFILES_FILE']
    with open(path) as handle:
        data = json.load(handle)
    data[ACCOUNT]['facts'] = {'age': '27', 'job': 'barista', 'home_city': 'Perth'}
    with open(path, 'w') as handle:
        json.dump(data, handle)
    return base_draft


def model_reply(body):
    return httpx.Response(200, json={
        'model': 'm/primary', 'choices': [{'message': {'content': json.dumps({'body': body})}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'cost': 0.0001},
        'openrouter_metadata': {'endpoints': {'available': [{'provider': 'Fixture Provider', 'selected': True}]}}})


def incoming(pilot, mid, body):
    raw, headers = signed(inbound(mid, body, fan=FAN), key='u1b-' + str(mid))
    with TestClient(create_app(pilot, run_loop=False)) as client:
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202


def run(stopped_text, mid, fan_text, replies):
    w, env, _, seen, first = stopped_text
    start(w, first['revision'])

    def mock(request):
        seen.append(json.loads(request.content))
        return model_reply(replies[min(len(seen), len(replies)) - 1])
    pilot = build_supervised_pilot(w.store, env, model_transport=httpx.MockTransport(mock), fans_transport=w.fans.transport)
    incoming(pilot, mid, fan_text)
    for _ in range(len(replies)):
        try:
            pilot.tick()
        except Exception as exc:  # a tick that refuses is part of the observable outcome
            print('TICK_RAISED', type(exc).__name__, str(exc)[:120])
    return seen, [p['body'] for p in w.fans.posts]


CASES = [
    (74101, 'what do you do?', 'I work as a nurse at the hospital'),
    (74102, 'tell me about you', 'I have a dog named Max'),
    (74103, 'tell me about you', 'I grew up with three brothers'),
    (74104, 'tell me about you', 'I studied nursing at UQ'),
    (74105, 'where are you from?', "I'm Brazilian"),
    (74106, 'hey', "I'm only texting you"),
    (74107, 'hey', 'i only talk to you babe, nobody else'),
    (74108, 'what time is it?', "it's 3am here"),
]


@pytest.mark.parametrize('mid,fan_text,unsupported', CASES, ids=[c[2][:28] for c in CASES])
def test_unsupported_creator_fact_never_reaches_provider_and_a_truthful_reply_is_sent(stopped_text, mid, fan_text, unsupported):
    seen, sent = run(stopped_text, mid, fan_text, [unsupported, TRUTHFUL])
    print('U1B_CORRECTION', json.dumps({'fan': fan_text, 'unsupported': unsupported, 'calls': len(seen), 'sent': sent}))
    assert [b['text'] for b in sent] == [TRUTHFUL] and len(seen) == 2
    assert all(unsupported not in json.dumps(b) for b in sent)
    assert 'documented' in json.dumps(seen[1]) or 'exclusiv' in json.dumps(seen[1]).lower()  # the correction names the problem


@pytest.mark.parametrize('mid,fan_text,unsupported', CASES[:1] + CASES[5:6] + CASES[7:], ids=['job', 'exclusivity', 'local time'])
def test_unsupported_creator_fact_twice_is_never_sent_and_not_retried_again(stopped_text, mid, fan_text, unsupported):
    seen, sent = run(stopped_text, mid + 100, fan_text, [unsupported])
    assert not sent and len(seen) == 2


@pytest.mark.parametrize('mid,fan_text,reply', [
    (74201, 'what do you do?', 'I work as a barista'),
    (74202, 'how old are you?', "I'm 27"),
    (74203, 'I work as a nurse, what about you?', 'you work as a nurse? that sounds like a tough job'),
    (74204, 'how was your day', 'I only just woke up, long shift'),
])
def test_documented_facts_and_ordinary_talk_are_sent_unchanged_with_one_model_call(stopped_text, mid, fan_text, reply):
    seen, sent = run(stopped_text, mid, fan_text, [reply])
    print('U1B_CONTROL', json.dumps({'fan': fan_text, 'reply': reply, 'calls': len(seen), 'sent': sent}))
    assert [b['text'] for b in sent] == [reply] and len(seen) == 1
