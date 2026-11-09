"""Current policy versus media capability: actual HTTP/runtime/validation/send boundary pairs."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world,inbound
from test_plan_c_draft_app_pg import configured_draft,signed
from test_plan_c_supervised_pg import supervised
from test_offline_completion_pg import normal_pilot
from test_plan_c_sales_journey_pg import writer
from test_onlyfans_text_delivery_pg import ACCOUNT
from db.onlyfans_engine_models import OfEngineRequest
from plan_c_worker import create_app
from conversation_engine.model_output import JudgeContext,judge
from conversation_engine.writer_context import CreatorProfile

CASES=[
 ('specific_media','What does that video show?', 'I cannot discuss that video because no video is available.',False),
 ('implicit_media','Can you discuss what is in the video?', 'I cannot discuss it because no video is available.',False),
 ('media_reason_first','What is shown in that video?', 'Since no video is available, I cannot discuss that video.',False),
 ('negated_media','Can we discuss gardening rather than pictures?', "I cannot discuss gardening because no photos are available.",True),
 ('not_media','I mean talking about cooking, not a video.', "Since no cooking video is available, we cannot discuss cooking.",True),
]
@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
@pytest.mark.parametrize('label,question,first,rejected',CASES,ids=[c[0] for c in CASES])
def test_normal_conversation_and_truthful_limits_complete(supervised,label,question,first,rejected):
    w,env,_,_,_=supervised
    normal_pilot(w,env,selecting=False)
    calls=[]
    corrected="I don't offer meetups." if label=='documented_limit_violation' else 'We can discuss cooking or gardening here.'
    def choose(latest,original):
        calls.append(latest)
        return {'body':first if len(calls)==1 else corrected}
    pilot,seen,_=writer(w,env,choose=choose)
    raw,headers=signed(inbound(24100,question),key='boundary-'+label)
    with TestClient(create_app(pilot,run_loop=False)) as client:
        assert client.post('/onlyfans/webhooks/plan-c',content=raw,headers=headers).status_code==202
    pilot.tick()
    assert len(w.fans.posts)==1,'legitimate reply or truthful limit must complete'
    delivered=w.fans.posts[0]['body']['text']
    assert (corrected if rejected else first) in delivered
    assert len(seen)==(2 if rejected else 1)
    assert not w.fans.posts[0]['body'].get('price',0)
    with Session(w.database[1]) as s:
        request=s.scalar(select(OfEngineRequest).where(OfEngineRequest.source_message_id==24100))
        assert request.status=='completed' and request.next_step=='none'
