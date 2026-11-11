"""Same behavioral assertion on investigated 659ea29 (red) and the exact R0 image (green).

No R0 schema or classifier imports: baseline failure must demonstrate scheduler stranding, not a missing column.
"""
import json
from datetime import timedelta
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from conversation_engine import pilot_scope
from db.onlyfans_engine_models import OfEngineRequest
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import utcnow
from test_engine_real_worker_pg import inbound, world
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_events_stage3_pg import Rig
from test_plan_c_text_activation_pg import stopped_text


def test_one_inbound_429_due_retry_completes_and_sends_exactly_once(stopped_text):
    rig = Rig(stopped_text)
    calls = []
    def model(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={'error': {'code': 429}})
        return httpx.Response(200, json={'model': 'm/primary', 'choices': [{'message': {
            'content': json.dumps({'body': 'hey you'})}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 5, 'cost': 0.0001},
            'openrouter_metadata': {'endpoints': {'available': [{'provider': 'Fixture Provider', 'selected': True}]}}})
    pilot = build_supervised_pilot(rig.w.store, rig.env, model_transport=httpx.MockTransport(model),
                                    fans_transport=rig.w.fans.transport)
    offset = [0]
    clock = lambda: utcnow() + timedelta(seconds=offset[0])
    pilot.worker.engine.model._clock = pilot.worker.engine.model.ledger.clock = clock
    try:
        rig.post(inbound(93001, 'hello how was your day', fan=FAN), key='r0-red-green')
        pilot.tick()
        assert len(calls) == 1 and rig.w.fans.posts == []
        offset[0] = 31
        for _ in range(4): pilot.tick()
        with Session(rig.w.database[1]) as s:
            request = s.scalar(select(OfEngineRequest).where(OfEngineRequest.account_id == ACCOUNT,
                OfEngineRequest.source_message_id == 93001))
            assert len(calls) == 2, f'429 request stranded: calls={len(calls)}, status={request.status}, step={request.next_step}'
            assert request.status == 'completed'
        assert len(rig.w.fans.posts) == 1 and rig.w.fans.posts[0]['body']['text'] == 'hey you'
    finally:
        rig.close()
