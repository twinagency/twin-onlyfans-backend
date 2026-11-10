"""Q3 independent counterexamples: immutable candidate image, synthetic providers and isolated PG."""
import json
import httpx
import pytest
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world, inbound
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN
from test_plan_c_draft_app_pg import configured_draft as base_draft
from test_plan_c_text_activation_pg import stopped_text, start
from test_plan_c_final_counterexamples import incoming_http, model_reply
from model_transport.pilot_supervised import build_supervised_pilot

@pytest.fixture
def configured_draft(base_draft):
    # Supply the profile before stopped_text installs it through normal scope configuration.
    w, env, transport, seen = base_draft
    path = env['ENGINE_MODEL_PROFILES_FILE']
    with open(path) as f:
        data = json.load(f)
    data[ACCOUNT]['facts'] = {'age': '27', 'job': 'teacher',
        'family': 'my sister is a nurse', 'pets': 'no dogs', 'home_city': 'Perth'}
    with open(path, 'w') as f:
        json.dump(data, f)
    return base_draft

@pytest.mark.parametrize('message,bad', [
    ('hello', 'The photo costs four.'),
    ('hello', 'It is a ten-minute video.'),
    ('What do you do?', 'I work as a nurse.'),
    ('Do you have a dog?', 'I have a dog.'),
], ids=['unsolicited-price', 'unsolicited-media', 'other-person-job', 'negated-pet-fact'])
def test_unsupported_assertion_is_corrected_before_dispatch(stopped_text, message, bad):
    w, env, _, seen, first = stopped_text
    start(w, first['revision'])
    def mock(request):
        seen.append(json.loads(request.content))
        return model_reply(bad if len(seen) == 1 else 'Good to hear from you.')
    pilot = build_supervised_pilot(w.store, env, model_transport=httpx.MockTransport(mock), fans_transport=w.fans.transport)
    incoming_http(pilot, 95100, message)
    result = pilot.tick()
    sent = [p['body']['text'] for p in w.fans.posts]
    print('Q3_OBSERVED', json.dumps({'incoming':message, 'bad':bad,'result':result,'calls':len(seen),'sent':sent},default=str))
    assert bad not in sent, 'unsupported statement was accepted and dispatched'
    assert sent == ['Good to hear from you.'] and len(seen) == 2
    assert all(not p['body'].get('price') and not p['body'].get('mediaFiles') for p in w.fans.posts)

@pytest.mark.parametrize('message,reply', [
    ('What do you do?', 'I work as a teacher.'),
    ('hello', 'Good to hear from you.'),
])
def test_documented_and_ordinary_controls_are_delivered(stopped_text, message, reply):
    w,env,_,seen,first = stopped_text
    start(w,first['revision'])
    def mock(request):
        seen.append(json.loads(request.content))
        return model_reply(reply)
    pilot=build_supervised_pilot(w.store,env,model_transport=httpx.MockTransport(mock),fans_transport=w.fans.transport)
    incoming_http(pilot,95101,message)
    pilot.tick()
    assert [p['body']['text'] for p in w.fans.posts] == [reply] and len(seen)==1
