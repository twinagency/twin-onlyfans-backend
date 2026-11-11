"""Authentication challenge recovery: synthetic provider only, no send authority from the probe."""
import pytest
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from conversation_engine.pilot_scope import ProviderRequestGuard
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEnginePilotScope
from db.onlyfans_fans_intake_models import OfFansLink
from fans_api_adapter.auth_recovery import recover_current_challenge
from fans_api_adapter.https_transport import FansHttpsTransport
from fans_api_adapter.protocol import FansApiClient
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world, PROVIDER, CREATOR, answer
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised, stage
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT
from test_plan_c_scope_pg import ACTOR
from test_offline_completion_pg import normal_pilot
from test_plan_c_offline_operations_pg import http_event


def client_for(w, pilot, callback=None):
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        if callback:
            callback()
        return answer(200,[{"id":PROVIDER,"onlyfans_id":CREATOR,"is_authenticated":True}])
    guard=ProviderRequestGuard(w.store,TENANT,ACCOUNT,pilot.scope_revision)
    wire=FansHttpsTransport(enabled=True,transport=httpx.MockTransport(handler))
    client=FansApiClient(account=PROVIDER,creator_id=CREATOR,epoch=1,token="fixture-token",
        http=wire,timeout=5,enabled=True,request_guard=guard)
    return client,calls


def challenge(pilot,key):
    body={"event":"accounts.otp_code_required","account_id":PROVIDER,"payload":{}}
    assert http_event(pilot,body,key)[0].status_code==202


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_verified_recovery_audits_once_keeps_send_block_and_requires_operator_release(supervised):
    w,env,_,_,_=supervised
    pilot,seen,_,model=normal_pilot(w,env,selecting=False)
    challenge(pilot,"challenge-before-proof")
    client,calls=client_for(w,pilot)
    with Session(w.database[1]) as s:
        before=s.get(OfEnginePilotScope,1).provider_requests_used
    kw=dict(expected_revision=pilot.scope_revision,command_key="a"*64,actor=ACTOR,reason="verified provider identity")
    first=recover_current_challenge(w.store,TENANT,ACCOUNT,client,**kw)
    assert first["status"]=="provider_verified" and first["sendBlocked"] and first["replayed"] is False
    assert recover_current_challenge(w.store,TENANT,ACCOUNT,client,**kw)["replayed"] is True
    assert calls==["/api/accounts"] and w.fans.posts==[]
    with Session(w.database[1]) as s:
        link=s.get(OfFansLink,(ACCOUNT,str(CREATOR)))
        assert link.state=="connected" and link.send_blocked
        assert s.get(OfDeliveryPolicy,ACCOUNT).enabled is False
        assert s.get(OfEnginePilotScope,1).provider_requests_used==before+1
        assert s.get(OfLaunchCommand,(ACCOUNT,"a"*64)).kind=="fans_auth_recovery"
    pilot.worker.intake.release_send_block(TENANT,ACCOUNT,str(CREATOR),actor=ACTOR,reason="operator restores verified link")
    fresh=build_supervised_pilot(w.store,env,model_transport=model,fans_transport=w.fans.transport)
    stage(fresh,23200,"hello");fresh.tick()
    assert len(w.fans.posts)==1


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_recovery_rejects_wrong_actor_and_unbudgeted_client_without_network(supervised):
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"challenge-authz")
    client,calls=client_for(w,pilot)
    with pytest.raises(Refused):
        recover_current_challenge(w.store,TENANT,ACCOUNT,client,expected_revision=pilot.scope_revision,
            command_key="b"*64,actor="operator-not-approved",reason="unauthorized")
    client._request_guard=None
    with pytest.raises(Refused):
        recover_current_challenge(w.store,TENANT,ACCOUNT,client,expected_revision=pilot.scope_revision,
            command_key="c"*64,actor=ACTOR,reason="no budget guard")
    assert calls==[] and w.fans.posts==[]
    with Session(w.database[1]) as s:
        assert s.get(OfFansLink,(ACCOUNT,str(CREATOR))).send_blocked


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_new_challenge_during_provider_probe_fences_old_proof(supervised):
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"challenge-before-race")
    client,calls=client_for(w,pilot,lambda:challenge(pilot,"challenge-during-proof"))
    with pytest.raises(Refused,match="changed"):
        recover_current_challenge(w.store,TENANT,ACCOUNT,client,expected_revision=pilot.scope_revision,
            command_key="d"*64,actor=ACTOR,reason="race test")
    assert calls==["/api/accounts"] and w.fans.posts==[]
    with Session(w.database[1]) as s:
        link=s.get(OfFansLink,(ACCOUNT,str(CREATOR)))
        assert link.state=="auth_failed" and link.send_blocked
        assert s.get(OfLaunchCommand,(ACCOUNT,"d"*64)) is None


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_false_provider_authentication_does_not_release_challenge(supervised):
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"challenge-before-failed-proof")
    client,calls=client_for(w,pilot)
    client._http=FansHttpsTransport(enabled=True,transport=httpx.MockTransport(
        lambda req:answer(200,[{"id":PROVIDER,"onlyfans_id":CREATOR,"is_authenticated":False}])))
    with pytest.raises(Refused,match="could not be verified"):
        recover_current_challenge(w.store,TENANT,ACCOUNT,client,expected_revision=pilot.scope_revision,
            command_key="e"*64,actor=ACTOR,reason="provider false")
    with Session(w.database[1]) as s:
        link=s.get(OfFansLink,(ACCOUNT,str(CREATOR)))
        assert link.state=="auth_failed" and link.send_blocked
    assert w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
@pytest.mark.parametrize("disconnected",[False,True])
def test_unverified_new_account_reconnect_is_durably_held(supervised,disconnected):
    from db.onlyfans_engine_models import OfEnginePilotWebhookDigest, OfFansConnection
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"challenge-new-account")
    if disconnected:
        assert http_event(pilot,{"event":"accounts.disconnected","account_id":PROVIDER,
            "payload":{"onlyfans_id":str(CREATOR)}},"old-disconnect")[0].status_code==202
    newbody={"event":"accounts.reconnected","account_id":"acct_new_challenged",
             "payload":{"onlyfans_id":str(CREATOR),"state":"authenticated",
                        "latestAuthAttempt":{"success":True}}}
    response,raw=http_event(pilot,newbody,"new-account-reconnect")
    assert response.status_code==202
    import hashlib
    with Session(w.database[1]) as s:
        retained=s.scalar(select(OfEnginePilotWebhookDigest).where(
            OfEnginePilotWebhookDigest.event_key=="new-account-reconnect"))
        assert retained is not None and retained.safety_hold
        assert retained.payload_sha256==hashlib.sha256(raw).hexdigest()
        assert s.scalar(select(OfFansConnection).where(
            OfFansConnection.provider_account_id=="acct_new_challenged")) is None
        assert s.get(OfFansLink,(ACCOUNT,str(CREATOR))).send_blocked
    assert w.fans.posts==[]


def new_candidate(pilot,key="new-account-candidate",account="acct_new_challenged"):
    from test_engine_real_worker_pg import SECRET
    import hmac,hashlib
    body={"event":"accounts.reconnected","account_id":account,
          "payload":{"onlyfans_id":str(CREATOR),"state":"authenticated",
                     "latestAuthAttempt":{"success":True}}}
    response,raw=http_event(pilot,body,key)
    assert response.status_code==202
    signature=hmac.new(SECRET,raw,hashlib.sha256).hexdigest()
    return raw,signature


def candidate_client(w,pilot,callback=None):
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        if callback:callback()
        return answer(200,[{"id":"acct_new_challenged","onlyfans_id":CREATOR,"is_authenticated":True}])
    guard=ProviderRequestGuard(w.store,TENANT,ACCOUNT,pilot.scope_revision)
    return FansApiClient(account="acct_new_challenged",creator_id=CREATOR,epoch=1,token="fixture-token",
        http=FansHttpsTransport(enabled=True,transport=httpx.MockTransport(handler)),
        timeout=5,enabled=True,request_guard=guard),calls


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
@pytest.mark.parametrize("disconnected",[False,True])
def test_verified_new_account_reconnect_resolves_only_exact_digest(supervised,disconnected):
    from db.onlyfans_engine_models import OfFansConnection
    from conversation_engine.pilot_scope import unresolved_webhook_digests
    from fans_api_adapter.auth_recovery import _recover_reconnected_evidence as recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"candidate-challenge")
    if disconnected:
        assert http_event(pilot,{"event":"accounts.disconnected","account_id":PROVIDER,
            "payload":{"onlyfans_id":str(CREATOR)}},"candidate-disconnect")[0].status_code==202
    raw,sig=new_candidate(pilot)
    client,calls=candidate_client(w,pilot)
    kw=dict(expected_revision=pilot.scope_revision,event_epoch=1,event_key="new-account-candidate",
        raw_body=raw,signature=sig,secret=SECRET,command_key="f"*64,actor=ACTOR,reason="verified replacement account")
    first=recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**kw)
    assert first["status"]=="provider_verified_reconnect" and first["sendBlocked"] and first["connectionEpoch"]==2
    assert recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**kw)["replayed"] is True
    assert calls==["/api/accounts"] and w.fans.posts==[]
    with Session(w.database[1]) as s:
        old=s.get(OfFansConnection,(ACCOUNT,1));new=s.get(OfFansConnection,(ACCOUNT,2))
        link=s.get(OfFansLink,(ACCOUNT,str(CREATOR)))
        assert old.status=="retired" and new.status=="active"
        assert new.provider_account_id=="acct_new_challenged" and new.binding_id==old.binding_id
        assert link.state=="connected" and link.send_blocked and s.get(OfDeliveryPolicy,ACCOUNT).enabled is False
        runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
        assert not list(s.scalars(unresolved_webhook_digests(runtime)))


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_reconnect_requires_exact_original_signature_and_no_conflict(supervised):
    from fans_api_adapter.auth_recovery import _recover_reconnected_evidence as recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"candidate-conflict-challenge")
    raw,sig=new_candidate(pilot,key="conflicting-candidate")
    client,calls=candidate_client(w,pilot)
    kw=dict(expected_revision=pilot.scope_revision,event_epoch=1,event_key="conflicting-candidate",
        raw_body=raw,signature="0"*64,secret=SECRET,command_key="1"*64,actor=ACTOR,reason="exact evidence")
    with pytest.raises(Refused,match="invalid reconnect"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**kw)
    body={"event":"accounts.reconnected","account_id":"acct_new_challenged",
          "payload":{"onlyfans_id":str(CREATOR),"different":True}}
    assert http_event(pilot,body,"conflicting-candidate")[0].status_code==202
    kw["signature"]=sig
    with pytest.raises(Refused,match="conflicting event key"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**kw)
    assert calls==[] and w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_reconnect_with_unknown_old_send_does_not_probe_or_rebind(supervised):
    from db.onlyfans_delivery_models import OfTextIntent
    from fans_api_adapter.auth_recovery import _recover_reconnected_evidence as recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    def provider(req):
        if req.method=="POST" and req.url.path.endswith("/messages"):
            w.fans.posts.append({"path":req.url.path})
            return answer(503,{"error":"ambiguous"})
        return w.fans.handle(req)
    base,_,_,model=normal_pilot(w,env,selecting=False)
    pilot=build_supervised_pilot(w.store,env,model_transport=model,
        fans_transport=httpx.MockTransport(provider))
    stage(pilot,23300,"hello");assert pilot.tick()["dispatched"]==["unknown"]
    challenge(pilot,"challenge-unknown-send")
    raw,sig=new_candidate(pilot)
    client,calls=candidate_client(w,pilot)
    with pytest.raises(Refused,match="unresolved delivery"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,
            expected_revision=pilot.scope_revision,event_epoch=1,event_key="new-account-candidate",
            raw_body=raw,signature=sig,secret=SECRET,command_key="2"*64,actor=ACTOR,reason="unknown send")
    assert calls==[]
    with Session(w.database[1]) as s:
        assert s.scalar(select(OfTextIntent).where(OfTextIntent.status=="unknown")) is not None
        assert s.get(OfFansLink,(ACCOUNT,str(CREATOR))).send_blocked


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_new_challenge_during_reconnect_probe_prevents_rebind(supervised):
    from db.onlyfans_engine_models import OfFansConnection
    from fans_api_adapter.auth_recovery import _recover_reconnected_evidence as recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"challenge-reconnect-race-before")
    raw,sig=new_candidate(pilot)
    client,calls=candidate_client(w,pilot,lambda:challenge(pilot,"challenge-reconnect-race-after"))
    with pytest.raises(Refused,match="changed"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,
            expected_revision=pilot.scope_revision,event_epoch=1,event_key="new-account-candidate",
            raw_body=raw,signature=sig,secret=SECRET,command_key="3"*64,actor=ACTOR,reason="racing challenge")
    assert calls==["/api/accounts"] and w.fans.posts==[]
    with Session(w.database[1]) as s:
        assert s.get(OfFansConnection,(ACCOUNT,2)) is None
        assert s.get(OfFansLink,(ACCOUNT,str(CREATOR))).send_blocked


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_unknown_new_account_challenge_during_reconnect_remains_held(supervised):
    from conversation_engine.pilot_scope import delivery_block, unresolved_webhook_digests
    from fans_api_adapter.auth_recovery import _recover_reconnected_evidence as recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    from test_onlyfans_text_delivery_pg import FAN
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"unknown-account-challenge-before")
    raw,sig=new_candidate(pilot)
    def second_challenge():
        response,_=http_event(pilot,{"event":"accounts.face_otp_required",
            "account_id":"acct_new_challenged","payload":{}},"unknown-account-challenge")
        assert response.status_code==202
    client,calls=candidate_client(w,pilot,second_challenge)
    result=recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,
        expected_revision=pilot.scope_revision,event_epoch=1,event_key="new-account-candidate",
        raw_body=raw,signature=sig,secret=SECRET,command_key="4"*64,actor=ACTOR,reason="reconnect proof")
    assert result["sendBlocked"] and calls==["/api/accounts"]
    pilot.worker.intake.release_send_block(TENANT,ACCOUNT,str(CREATOR),actor=ACTOR,reason="operator release")
    with Session(w.database[1]) as s:
        runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
        remaining=list(s.scalars(unresolved_webhook_digests(runtime)))
        assert [x.event_key for x in remaining]==["unknown-account-challenge"]
        assert delivery_block(s,runtime,FAN)=="pilot_evidence_pending"
    assert w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_exact_unknown_challenge_replay_projects_stop_and_resolves_only_that_digest(supervised):
    from conversation_engine.pilot_scope import unresolved_webhook_digests
    from fans_api_adapter.auth_recovery import _recover_reconnected_evidence as recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"unknown-replay-challenge-before")
    raw,sig=new_candidate(pilot)
    body={"event":"accounts.face_otp_required","account_id":"acct_new_challenged","payload":{}}
    def second_challenge():
        assert http_event(pilot,body,"unknown-challenge-replay")[0].status_code==202
    client,_=candidate_client(w,pilot,second_challenge)
    recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,
        expected_revision=pilot.scope_revision,event_epoch=1,event_key="new-account-candidate",
        raw_body=raw,signature=sig,secret=SECRET,command_key="5"*64,actor=ACTOR,reason="reconnect proof")
    assert http_event(pilot,body,"unknown-challenge-replay")[0].status_code==202
    with Session(w.database[1]) as s:
        runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
        assert not list(s.scalars(unresolved_webhook_digests(runtime)))
        link=s.get(OfFansLink,(ACCOUNT,str(CREATOR)))
        assert link.state=="auth_failed" and link.send_blocked
        assert s.get(OfDeliveryPolicy,ACCOUNT).enabled is False
    assert w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_challenge_replay_does_not_clear_conflicts_or_unrelated_holds(supervised):
    from conversation_engine.pilot_scope import unresolved_webhook_digests
    from fans_api_adapter.auth_recovery import _recover_reconnected_evidence as recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"conflict-replay-before")
    raw,sig=new_candidate(pilot)
    face={"event":"accounts.face_otp_required","account_id":"acct_new_challenged","payload":{}}
    otp={"event":"accounts.otp_code_required","account_id":"acct_new_challenged","payload":{}}
    def conflicting_challenges():
        assert http_event(pilot,face,"conflict-replay")[0].status_code==202
        assert http_event(pilot,otp,"conflict-replay")[0].status_code==202
        assert http_event(pilot,face,"unrelated-hold")[0].status_code==202
    client,_=candidate_client(w,pilot,conflicting_challenges)
    recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,
        expected_revision=pilot.scope_revision,event_epoch=1,event_key="new-account-candidate",
        raw_body=raw,signature=sig,secret=SECRET,command_key="6"*64,actor=ACTOR,reason="reconnect proof")
    assert http_event(pilot,face,"conflict-replay")[0].status_code==202
    with Session(w.database[1]) as s:
        runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
        remaining=list(s.scalars(unresolved_webhook_digests(runtime)))
        assert sorted(x.event_key for x in remaining)==["conflict-replay","conflict-replay","unrelated-hold"]
        assert s.get(OfFansLink,(ACCOUNT,str(CREATOR))).send_blocked
    assert w.fans.posts==[]


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
@pytest.mark.parametrize("replacement",[False,True])
def test_worker_owned_recovery_command_keeps_provider_secret_private(supervised,tmp_path,monkeypatch,capsys,replacement):
    import json
    from fans_api_adapter.real_transport import RealFansConfig
    from fans_api_adapter.https_transport import FansHttpsTransport as RealWire
    from test_engine_real_worker_pg import SECRET
    from tools import plan_c_recover_auth as command
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"command-challenge")
    target=PROVIDER
    evidence=[]
    if replacement:
        raw,sig=new_candidate(pilot)
        target="acct_new_challenged"
        import hashlib
        secret=tmp_path/"webhook-secret";secret.write_bytes(SECRET);secret.chmod(0o600)
        monkeypatch.setenv("TWIN_PLAN_C_WEBHOOK_SECRET_FILE",str(secret))
        evidence=["--event-epoch","1","--event-key","new-account-candidate",
                  "--payload-sha256",hashlib.sha256(raw).hexdigest()]
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        return answer(200,[{"id":target,"onlyfans_id":CREATOR,"is_authenticated":True}])
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    monkeypatch.setenv("DATABASE_URL","fixture-placeholder")
    monkeypatch.setattr(command,"Store",lambda _:w.store)
    monkeypatch.setattr(command,"load_real_config",lambda *a,**k:RealFansConfig(
        "fixture-secret-never-print",target,"https://api.fansapi.com",5))
    monkeypatch.setattr(command,"FansHttpsTransport",lambda **kw:RealWire(
        enabled=True,transport=httpx.MockTransport(handler)))
    command.main(["--tenant",TENANT,"--account",ACCOUNT,"--actor",ACTOR,
        "--command-key","7"*64,"--reason","fixture verified recovery",
        "--expected-pilot-revision",str(pilot.scope_revision),*evidence])
    output=capsys.readouterr().out
    assert "fixture-secret-never-print" not in output
    result=json.loads(output)
    assert result["sendBlocked"] and result["status"] in ("provider_verified","provider_verified_reconnect")
    assert calls==["/api/accounts"] and w.fans.posts==[]
    with Session(w.database[1]) as s:
        assert s.get(OfDeliveryPolicy,ACCOUNT).enabled is False
        assert s.get(OfFansLink,(ACCOUNT,str(CREATOR))).send_blocked


@pytest.mark.parametrize("supervised",["autonomous"],indirect=True)
def test_verified_rebind_fences_old_worker_and_new_worker_can_reply(supervised):
    from fans_api_adapter.auth_recovery import _recover_reconnected_evidence as recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,model=normal_pilot(w,env,selecting=False)
    old=stage(pilot,23500,"hello before reconnect")
    challenge(pilot,"rebind-fence-challenge")
    raw,sig=new_candidate(pilot)
    client,_=candidate_client(w,pilot)
    recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,
        expected_revision=pilot.scope_revision,event_epoch=1,event_key="new-account-candidate",
        raw_body=raw,signature=sig,secret=SECRET,command_key="8"*64,actor=ACTOR,reason="verified rebind")
    pilot.worker.intake.release_send_block(TENANT,ACCOUNT,str(CREATOR),actor=ACTOR,reason="operator release")
    try:
        pilot.worker.send_approved(TENANT,ACCOUNT,old["decisionId"])
    except Refused:
        pass
    assert w.fans.posts==[]
    credentials=env["TWIN_ENGINE_FANSAPI_ENV_FILE"]
    from pathlib import Path
    Path(credentials).write_text("FANSAPI_KEY=fixture-key\nFANSAPI_ACCT=acct_new_challenged\n")
    def provider(req):
        if req.method=="GET" and req.url.path=="/api/accounts":
            return answer(200,[{"id":"acct_new_challenged","onlyfans_id":CREATOR,"is_authenticated":True}])
        return w.fans.handle(req)
    fresh=build_supervised_pilot(w.store,env,model_transport=model,fans_transport=httpx.MockTransport(provider))
    stage(fresh,23501,"hello after reconnect");fresh.tick()
    assert len(w.fans.posts)==1 and "/acct_new_challenged/" in w.fans.posts[0]["path"]


@pytest.mark.parametrize("supervised", ["autonomous"], indirect=True)
def test_retained_reconnect_receipt_recovers_and_preserves_unrelated_hold(supervised):
    import hashlib
    from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt
    from conversation_engine.pilot_scope import unresolved_webhook_digests
    from fans_api_adapter.auth_recovery import recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"receipt-challenge")
    raw,sig=new_candidate(pilot,key="receipt-candidate")
    new_candidate(pilot,key="other-receipt",account="acct_other_unknown")
    digest=hashlib.sha256(raw).hexdigest()
    # Duplicate receipt is idempotent. It does not bind the new account.
    assert http_event(pilot,{"event":"accounts.reconnected","account_id":"acct_new_challenged",
        "payload":{"onlyfans_id":str(CREATOR),"state":"authenticated",
                   "latestAuthAttempt":{"success":True}}},"receipt-candidate")[0].status_code==202
    with Session(w.database[1]) as s:
        row=s.get(OfEnginePilotReconnectReceipt,(ACCOUNT,1,"receipt-candidate",digest))
        assert bytes(row.raw_body)==raw and row.signature==sig
        assert len(list(s.scalars(select(OfEnginePilotReconnectReceipt).where(
            OfEnginePilotReconnectReceipt.event_key=="receipt-candidate"))))==1
    client,calls=candidate_client(w,pilot)
    kwargs=dict(expected_revision=pilot.scope_revision,event_epoch=1,event_key="receipt-candidate",
        payload_sha256=digest,secret=SECRET,command_key="d"*64,actor=ACTOR,reason="verified retained receipt")
    result=recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**kwargs)
    assert result["sendBlocked"] and result["connectionEpoch"]==2
    assert recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**kwargs)["replayed"]
    with Session(w.database[1]) as s:
        runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
        assert [x.event_key for x in s.scalars(unresolved_webhook_digests(runtime))]==["other-receipt"]
        assert s.get(OfDeliveryPolicy,ACCOUNT).enabled is False
    assert calls==["/api/accounts"] and w.fans.posts==[]


@pytest.mark.parametrize("supervised", ["autonomous"], indirect=True)
def test_retained_reconnect_rejects_conflict_wrong_epoch_and_creator(supervised):
    import hashlib
    from fans_api_adapter.auth_recovery import recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"receipt-negative-challenge")
    raw,_=new_candidate(pilot,key="receipt-negative")
    client,calls=candidate_client(w,pilot)
    args=dict(expected_revision=pilot.scope_revision,event_epoch=1,event_key="receipt-negative",
        payload_sha256=hashlib.sha256(raw).hexdigest(),secret=SECRET,
        command_key="e"*64,actor=ACTOR,reason="test evidence")
    with pytest.raises(Refused,match="receipt is unavailable"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**{**args,"event_epoch":2})
    client.creator_id="different-creator"
    with pytest.raises(Refused,match="identity conflicts"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**args)
    client.creator_id=CREATOR
    assert http_event(pilot,{"event":"accounts.reconnected","account_id":"acct_new_challenged",
        "payload":{"onlyfans_id":str(CREATOR),"different":True}},"receipt-negative")[0].status_code==202
    with pytest.raises(Refused,match="conflicting event key"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**args)
    assert calls==[] and w.fans.posts==[]


@pytest.mark.parametrize("supervised", ["autonomous"], indirect=True)
def test_reconnect_receipt_transaction_failure_does_not_ack_or_leave_partial_evidence(supervised):
    import hashlib,hmac,json
    from sqlalchemy import event
    from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt,OfEnginePilotWebhookDigest
    from model_transport.pilot_supervised import _record_outside_digest
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"receipt-crash-challenge")
    raw=json.dumps({"event":"accounts.reconnected","account_id":"acct_new_challenged",
        "payload":{"onlyfans_id":str(CREATOR)}}).encode()
    sig=hmac.new(SECRET,raw,hashlib.sha256).hexdigest()
    def fail(*_): raise RuntimeError("injected receipt write crash")
    event.listen(OfEnginePilotReconnectReceipt,"before_insert",fail)
    try:
        with pytest.raises(RuntimeError,match="injected receipt"):
            _record_outside_digest(w.store,TENANT,ACCOUNT,raw,"receipt-crash",
                signature=sig,secret=SECRET,safety_hold=True)
    finally:
        event.remove(OfEnginePilotReconnectReceipt,"before_insert",fail)
    with Session(w.database[1]) as s:
        assert not s.scalar(select(OfEnginePilotWebhookDigest).where(OfEnginePilotWebhookDigest.event_key=="receipt-crash"))
        assert not s.scalar(select(OfEnginePilotReconnectReceipt).where(OfEnginePilotReconnectReceipt.event_key=="receipt-crash"))
    _record_outside_digest(w.store,TENANT,ACCOUNT,raw,"receipt-crash",
        signature=sig,secret=SECRET,safety_hold=True)
    with Session(w.database[1]) as s:
        assert s.scalar(select(OfEnginePilotReconnectReceipt).where(OfEnginePilotReconnectReceipt.event_key=="receipt-crash"))
        assert s.scalar(select(OfEnginePilotWebhookDigest).where(OfEnginePilotWebhookDigest.event_key=="receipt-crash")).safety_hold


@pytest.mark.parametrize("supervised", ["autonomous"], indirect=True)
def test_reconnect_receipt_survives_restart_and_serializes_concurrent_recovery(supervised):
    import hashlib,threading
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt
    from fans_api_adapter.auth_recovery import recover_reconnected_challenge
    from onlyfans_runtime.store import Store
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    challenge(pilot,"receipt-race-challenge")
    raw,_=new_candidate(pilot,key="receipt-race")
    digest=hashlib.sha256(raw).hexdigest()
    key=(ACCOUNT,1,"receipt-race",digest)
    with w.database[1].begin() as c:
        from alembic import command
        from db.schema import _config
        for sql in ["DELETE FROM of_engine_pilot_reconnect_receipt",
                    "UPDATE of_engine_pilot_reconnect_receipt SET raw_body=raw_body"]:
            with pytest.raises(DBAPIError):
                with c.begin_nested(): c.execute(text(sql))
        with pytest.raises(RuntimeError,match="Preserve reconnect receipts"):
            with c.begin_nested(): command.downgrade(_config(c),"0128_model_billing_evidence")
    # Construct a fresh store/client as a restarted worker would; receipt comes only from DB.
    # SQLAlchemy str(URL) deliberately masks a password. Recreate the real URL, preserving password auth in the
    # restarted store. This is test setup only; no product authentication or concurrency assertion is changed.
    restarted=Store(w.database[1].url.render_as_string(hide_password=False))
    entered,release=threading.Event(),threading.Event()
    calls=[]
    def wire(req):
        calls.append(req.url.path)
        entered.set()
        assert release.wait(20)
        return answer(200,[{"id":"acct_new_challenged","onlyfans_id":CREATOR,"is_authenticated":True}])
    guard=ProviderRequestGuard(restarted,TENANT,ACCOUNT,pilot.scope_revision)
    client=FansApiClient(account="acct_new_challenged",creator_id=CREATOR,epoch=1,token="fixture-token",
        http=FansHttpsTransport(enabled=True,transport=httpx.MockTransport(wire)),
        timeout=5,enabled=True,request_guard=guard)
    args=dict(expected_revision=pilot.scope_revision,event_epoch=1,event_key="receipt-race",
        payload_sha256=digest,secret=SECRET,command_key="a"*64,actor=ACTOR,reason="restart recovery")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(recover_reconnected_challenge,restarted,TENANT,ACCOUNT,client,**args)
            assert entered.wait(20)
            second=pool.submit(recover_reconnected_challenge,restarted,TENANT,ACCOUNT,client,**args)
            with pytest.raises(Refused,match="already in progress"):second.result(timeout=20)
            release.set()
            assert first.result(timeout=20)["sendBlocked"]
        assert recover_reconnected_challenge(restarted,TENANT,ACCOUNT,client,**args)["replayed"]
        with Session(restarted.engine) as s:
            assert bytes(s.get(OfEnginePilotReconnectReceipt,key).raw_body)==raw
            assert s.get(OfDeliveryPolicy,ACCOUNT).enabled is False
        assert calls==["/api/accounts"] and w.fans.posts==[]
    finally:
        release.set()
        restarted.close()


def stop_for_reconnect(w):
    from onlyfans_runtime.text_delivery import TextAuthority
    with Session(w.database[1]) as s:
        policy=s.get(OfDeliveryPolicy,ACCOUNT)
        revision,limit,throttle=policy.revision,policy.limit_units,policy.throttle_seconds
    TextAuthority(w.store).configure(TENANT,ACCOUNT,expected_revision=revision,enabled=False,
        limit_units=limit,throttle_seconds=throttle,actor=ACTOR,reason="verify changed provider account")


@pytest.mark.parametrize("supervised", ["autonomous"], indirect=True)
def test_unchallenged_changed_account_is_held_then_verified_and_activated(supervised):
    import hashlib
    from pathlib import Path
    from fans_api_adapter.auth_recovery import recover_reconnected_challenge
    from db.onlyfans_engine_models import OfFansConnection,OfEnginePilotReconnectReceipt
    from conversation_engine.pilot_scope import unresolved_webhook_digests
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,model=normal_pilot(w,env,selecting=False)
    old=stage(pilot,23800,"hello before account changed")
    raw,sig=new_candidate(pilot,key="unchallenged-verified")
    # Repeat the exact receipt; it cannot mint a binding or duplicate a receipt.
    raw_again,sig_again=new_candidate(pilot,key="unchallenged-verified")
    assert raw_again==raw and sig_again==sig
    digest=hashlib.sha256(raw).hexdigest()
    with Session(w.database[1]) as s:
        assert s.get(OfFansConnection,(ACCOUNT,1)).status=="active"
        assert s.get(OfFansConnection,(ACCOUNT,2)) is None
        assert s.get(OfEnginePilotReconnectReceipt,(ACCOUNT,1,"unchallenged-verified",digest)) is not None
        runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
        assert [x.event_key for x in s.scalars(unresolved_webhook_digests(runtime))]==["unchallenged-verified"]
    try: pilot.worker.send_approved(TENANT,ACCOUNT,old["decisionId"])
    except Refused: pass
    assert w.fans.posts==[]
    stop_for_reconnect(w)
    client,calls=candidate_client(w,pilot)
    kwargs=dict(expected_revision=pilot.scope_revision,event_epoch=1,event_key="unchallenged-verified",
        payload_sha256=digest,secret=SECRET,command_key="b"*64,actor=ACTOR,reason="verify changed account")
    result=recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**kwargs)
    assert result["sendBlocked"] and result["connectionEpoch"]==2
    assert recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**kwargs)["replayed"]
    with Session(w.database[1]) as s:
        link=s.get(OfFansLink,(ACCOUNT,str(CREATOR)))
        assert link.send_blocked and link.block_reason=="provider_reconnect_verified"
        runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
        assert not list(s.scalars(unresolved_webhook_digests(runtime)))
    pilot.worker.intake.release_send_block(TENANT,ACCOUNT,str(CREATOR),actor=ACTOR,reason="operator activates verified account")
    try: pilot.worker.send_approved(TENANT,ACCOUNT,old["decisionId"])
    except Refused: pass
    assert w.fans.posts==[]
    Path(env["TWIN_ENGINE_FANSAPI_ENV_FILE"]).write_text("FANSAPI_KEY=fixture-key\nFANSAPI_ACCT=acct_new_challenged\n")
    def provider(req):
        if req.method=="GET" and req.url.path=="/api/accounts":
            return answer(200,[{"id":"acct_new_challenged","onlyfans_id":CREATOR,"is_authenticated":True}])
        return w.fans.handle(req)
    fresh=build_supervised_pilot(w.store,env,model_transport=model,fans_transport=httpx.MockTransport(provider))
    stage(fresh,23801,"hello after verified account");fresh.tick()
    assert len(w.fans.posts)==1 and "/acct_new_challenged/" in w.fans.posts[0]["path"]
    assert calls==["/api/accounts"]


@pytest.mark.parametrize("supervised", ["autonomous"], indirect=True)
def test_unchallenged_reconnect_rejects_wrong_provider_and_new_challenge_during_proof(supervised):
    import hashlib
    from fans_api_adapter.auth_recovery import recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    raw,_=new_candidate(pilot,key="unchallenged-proof-race")
    stop_for_reconnect(w)
    client,calls=candidate_client(w,pilot,callback=lambda:challenge(pilot,"unchallenged-racing-challenge"))
    kwargs=dict(expected_revision=pilot.scope_revision,event_epoch=1,event_key="unchallenged-proof-race",
        payload_sha256=hashlib.sha256(raw).hexdigest(),secret=SECRET,command_key="c"*64,
        actor=ACTOR,reason="verify unchanged challenge state")
    wrong,_=candidate_client(w,pilot)
    wrong.creator_id="wrong-creator"
    with pytest.raises(Refused,match="identity conflicts"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,wrong,**kwargs)
    with pytest.raises(Refused,match="challenge|binding|reconnect hold"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**kwargs)
    from db.onlyfans_engine_models import OfFansConnection
    with Session(w.database[1]) as s:
        assert s.get(OfFansConnection,(ACCOUNT,2)) is None
        assert s.get(OfFansLink,(ACCOUNT,str(CREATOR))).send_blocked
        assert s.get(OfDeliveryPolicy,ACCOUNT).enabled is False
    assert calls==["/api/accounts"] and w.fans.posts==[]


@pytest.mark.parametrize("supervised", ["autonomous"], indirect=True)
def test_unchallenged_reconnect_concurrent_proof_preserves_other_hold(supervised):
    import hashlib,threading
    from concurrent.futures import ThreadPoolExecutor
    from fans_api_adapter.auth_recovery import recover_reconnected_challenge
    from conversation_engine.pilot_scope import unresolved_webhook_digests
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    raw,_=new_candidate(pilot,key="concurrent-unchallenged")
    new_candidate(pilot,key="unrelated-unchallenged",account="acct_other_unknown")
    stop_for_reconnect(w)
    entered,release=threading.Event(),threading.Event()
    def block():
        entered.set()
        assert release.wait(20)
    client,calls=candidate_client(w,pilot,callback=block)
    kwargs=dict(expected_revision=pilot.scope_revision,event_epoch=1,event_key="concurrent-unchallenged",
        payload_sha256=hashlib.sha256(raw).hexdigest(),secret=SECRET,command_key="9"*64,
        actor=ACTOR,reason="verify concurrent changed account")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(recover_reconnected_challenge,w.store,TENANT,ACCOUNT,client,**kwargs)
            assert entered.wait(20)
            second=pool.submit(recover_reconnected_challenge,w.store,TENANT,ACCOUNT,client,**kwargs)
            with pytest.raises(Refused,match="already in progress"):second.result(timeout=20)
            release.set()
            assert first.result(timeout=20)["sendBlocked"]
        assert recover_reconnected_challenge(w.store,TENANT,ACCOUNT,client,**kwargs)["replayed"]
        with Session(w.database[1]) as s:
            runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
            assert [x.event_key for x in s.scalars(unresolved_webhook_digests(runtime))]==["unrelated-unchallenged"]
            assert s.get(OfDeliveryPolicy,ACCOUNT).enabled is False
        assert calls==["/api/accounts"] and w.fans.posts==[]
    finally:
        release.set()


@pytest.mark.parametrize("supervised", ["autonomous"], indirect=True)
def test_unchallenged_reconnect_rejects_stale_epoch_and_wrong_provider_creator(supervised):
    import hashlib
    from fans_api_adapter.auth_recovery import recover_reconnected_challenge
    from test_engine_real_worker_pg import SECRET
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    raw,_=new_candidate(pilot,key="stale-unchallenged")
    stop_for_reconnect(w)
    calls=[]
    def wrong_wire(req):
        calls.append(req.url.path)
        return answer(200,[{"id":"acct_new_challenged","onlyfans_id":999999,"is_authenticated":True}])
    guard=ProviderRequestGuard(w.store,TENANT,ACCOUNT,pilot.scope_revision)
    wrong=FansApiClient(account="acct_new_challenged",creator_id=CREATOR,epoch=1,token="fixture-token",
        http=FansHttpsTransport(enabled=True,transport=httpx.MockTransport(wrong_wire)),
        timeout=5,enabled=True,request_guard=guard)
    kwargs=dict(expected_revision=pilot.scope_revision,event_epoch=1,event_key="stale-unchallenged",
        payload_sha256=hashlib.sha256(raw).hexdigest(),secret=SECRET,command_key="6"*64,
        actor=ACTOR,reason="verify provider creator")
    with pytest.raises(Refused,match="authentication could not be verified"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,wrong,**kwargs)
    good,good_calls=candidate_client(w,pilot)
    assert recover_reconnected_challenge(w.store,TENANT,ACCOUNT,good,**kwargs)["sendBlocked"]
    assert http_event(pilot,{"event":"accounts.disconnected","account_id":"acct_new_challenged",
        "payload":{"onlyfans_id":str(CREATOR)}},"new-account-disconnect")[0].status_code==202
    with pytest.raises(Refused,match="epoch is stale"):
        recover_reconnected_challenge(w.store,TENANT,ACCOUNT,good,**{**kwargs,"command_key":"5"*64})
    assert calls==["/api/accounts"] and good_calls==["/api/accounts"] and w.fans.posts==[]


@pytest.mark.parametrize("supervised", ["autonomous"], indirect=True)
def test_unchallenged_same_account_refresh_does_not_require_recovery(supervised):
    from db.onlyfans_engine_models import OfFansConnection,OfEnginePilotWebhookDigest
    w,env,_,_,_=supervised
    pilot,_,_,_=normal_pilot(w,env,selecting=False)
    response,_=http_event(pilot,{"event":"accounts.reconnected","account_id":PROVIDER,
        "payload":{"onlyfans_id":str(CREATOR),"state":"authenticated"}},"same-account-refresh")
    assert response.status_code==202
    with Session(w.database[1]) as s:
        assert s.get(OfFansConnection,(ACCOUNT,1)).status=="active"
        assert s.get(OfFansConnection,(ACCOUNT,2)) is None
        assert s.get(OfDeliveryPolicy,ACCOUNT).enabled is True
        assert not list(s.scalars(select(OfEnginePilotWebhookDigest).where(
            OfEnginePilotWebhookDigest.event_key=="same-account-refresh")))
    assert w.fans.posts==[]
