
import httpx
import pytest
from sqlalchemy.orm import Session
from db.models import OfConversationState
from test_plan_c_supervised_pg import supervised, stage, approve
from test_plan_c_draft_app_pg import configured_draft
from test_engine_real_worker_pg import world, answer
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT, FAN
from test_plan_c_scope_pg import ACTOR
from model_transport.pilot_supervised import build_supervised_pilot

@pytest.mark.parametrize("change", ["takeover", "contact_stop"])
def test_retry_rechecks_fan_authority(supervised, change):
    world, env, model, seen, original = supervised
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append({"path":request.url.path, "body":request.content.decode()})
            if len(posts) == 1:
                return answer(503, {"error":"SERVICE_UNAVAILABLE","request_outcome":"not_sent"})
        return world.fans.handle(request)
    pilot = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=httpx.MockTransport(handler))
    result = stage(pilot, 99101)
    approve(world, result)
    states = []
    def intervening_control(_):
        with Session(world.database[1]) as s:
            revision = s.get(OfConversationState, (ACCOUNT,FAN)).revision
        world.store.control(TENANT, ACCOUNT, FAN,
            mode="human_controlled" if change=="takeover" else "no_contact",
            expected_revision=revision, command_key="audit-retry-"+change, actor=ACTOR,
            reason="Synthetic audit control between proven-not-sent attempts",
            action="request_takeover" if change=="takeover" else "mark_no_contact")
        with Session(world.database[1]) as s:
            state=s.get(OfConversationState,(ACCOUNT,FAN))
            states.append({"mode":state.mode,"takeover":state.takeover_state,"revision":state.revision})
    pilot.worker.transport.sleep = intervening_control
    outcome = pilot.worker.send_approved(TENANT,ACCOUNT,result["decisionId"])
    print("AUDIT", {"change":change,"post_count":len(posts),"control_after_first_post":states,"outcome":outcome})
    assert states, "control mutation did not run"
    assert len(posts)==1, "Provider POST repeated after fan control removed sending authority"
