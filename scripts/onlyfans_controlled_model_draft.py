"""Prepare the one-account model check; inference requires the explicit CLI flag.

No OnlyFans client, delivery transport, sender process or activation is created.
Use only after the bounded test approval. The default performs read-only checks.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

ACCOUNT="of-176774528"
FAN=318022380
MODEL_ID="openrouter/deepseek-v4.1-flash"
PERSONA="dec738d49f12d094c23caa39fdc791f6817bd20bbdba39fd49f9da11f3f02c4e"
NETWORK="0e91513be4a23a5279d7c39c2dc34a9b6f186eaafec11851b1696c441d0317fc"

async def run(args):
    from native_pilot.scope import AccountScope
    from native_pilot.connection import load_verified_connection
    from native_pilot.model import NativePilotModel, MODEL, ENDPOINT
    from native_pilot.budget import DailyBudget
    from onlyfans_runtime.connections import capture_network_fingerprint
    from db.engine import get_session, engine
    from db.models import AccountAiConfig
    scope=AccountScope.parse(ACCOUNT)
    try:
        connection=await load_verified_connection(scope)
        if (connection.tenant_id!="twin-internal" or connection.owner_id!=176774528
                or connection.revision!=9 or connection.model_id!=MODEL_ID
                or capture_network_fingerprint(connection.proxy_url)!=NETWORK):
            raise RuntimeError("controlled connection changed; refresh the review packet")
        async with get_session() as session:
            config=await session.get(AccountAiConfig,ACCOUNT)
            persona=config.persona or ""
            if hashlib.sha256(persona.encode()).hexdigest()!=PERSONA:
                raise RuntimeError("saved persona changed")
        messages=[{"role":"system","content":persona},{"role":"user","content":
            "This is a private integration test. Reply with a short greeting."}]
        proof={"account":ACCOUNT,"recipient":FAN,"model":MODEL_ID,"wireModel":MODEL,
               "endpoint":ENDPOINT,"personaSha256":PERSONA,"networkFingerprint":NETWORK,
               "promptSha256":hashlib.sha256(json.dumps(messages,sort_keys=True).encode()).hexdigest(),
               "maxCalls":1,"maxOutputTokens":128,"fanTransportCreated":False}
        if not args.execute_model_draft:
            print(json.dumps(dict(proof,inferenceRequested=False)))
            return
        directory=Path(args.evidence_dir).resolve()
        directory.mkdir(parents=True,exist_ok=True)
        marker=directory/"astra-model-draft-20261027-attempt.json"
        # The fixed exclusive marker forbids an automatic repeat after uncertainty.
        fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,"w") as file:
            json.dump(dict(proof,status="reserved"),file)
        deadline=time.monotonic()+45
        events=[]
        model=NativePilotModel(api_key=connection.model_api_key,account_id=ACCOUNT,fan_id=FAN,
            deadline=deadline,should_stop=lambda: (directory/"astra-model-draft.stop").exists()
                or datetime.now(timezone.utc)>=connection.expires_at,
            on_event=events.append,max_calls=1,max_tokens=128,
            budget=DailyBudget(scope,MODEL_ID,endpoint=ENDPOINT))
        started=time.monotonic()
        try:
            result=await model.chat(model=MODEL,messages=messages,purpose="ai_chatter",
                account_id=ACCOUNT,fan_id=FAN,response_format={"type":"text"})
            output={"status":"completed","latencySeconds":time.monotonic()-started,
                    "content":result.content,"events":events}
        except Exception as error:
            output={"status":"held","errorType":type(error).__name__,"events":events}
            marker.write_text(json.dumps(dict(proof,**output),indent=2)+"\n")
            raise RuntimeError("draft outcome held; do not retry") from None
        marker.write_text(json.dumps(dict(proof,**output),indent=2)+"\n")
        print(json.dumps({"status":"completed","evidence":str(marker),"fanTransportCreated":False}))
    finally:
        await engine.dispose()

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-model-draft",action="store_true")
    parser.add_argument("--evidence-dir",default="/tmp/twin-controlled-provider-evidence")
    asyncio.run(run(parser.parse_args()))
