"""Ordinary-chat prompt and real FASTT output shaping; synthetic boundaries."""
import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from onlyfans_runtime.reply_policy import FORMAT_INSTRUCTION, FORMAT_KEY, PACING_KEY


def build(*, enabled=False, kind="", content_ask=False):
    from automations import ai_chatter as native
    from db.models import Fan
    fan=Fan(account_id="900001",fan_id=900002,real_name="Sam",his_age="29",home_city="Harbor")
    cand=native._Cand(900002)
    cand.messages=[("in","Thanks, that helped")]
    cand.last_in_text=cand.last_body="Thanks, that helped"
    cand.last_in_at=datetime(2026,10,30,1)
    with patch.object(native.random,"choice",lambda items:items[0]),patch.object(native.random,"random",lambda:0.99):
        return native._build_messages("Approved creator enjoys hiking.",fan,cand,set(),
            twin_context=True,native_reply_format=enabled,kind=kind,content_ask=content_ask)


def test_off_prompt_matches_unchanged_baseline_golden():
    # Generated from attribution PR55's unchanged _build_messages with this
    # exact fixture/random seam, not from the format-enabled implementation.
    digest=hashlib.sha256(json.dumps(build(),sort_keys=True,default=str).encode()).hexdigest()
    assert digest=="153d4623d8366995f68258d46aa58f30883e78c33d421f19906b4007481b4ac7"


def test_format_instruction_replaces_compulsory_interview_and_bubble_style_only():
    messages,presented=build(enabled=True)
    system=messages[0]["content"]
    assert FORMAT_INSTRUCTION in system and presented==[]
    assert "YOUR GOAL THIS MESSAGE: find out ONE" not in system
    assert "No paragraphs." not in system
    assert "Use the approved creator facts" in system
    assert "Never mention AI, bots, clones or automation" in system
    assert messages[1]["content"]==build()[0][1]["content"]


@pytest.mark.parametrize("kind,ask",[("content_ask",True),("rate_pic",False),("pic_offer",False),("brush_off",False)])
def test_transactional_and_mandatory_turn_instructions_remain_byte_equal(kind,ask):
    assert build(enabled=True,kind=kind,content_ask=ask)==build(enabled=False,kind=kind,content_ask=ask)


@pytest.mark.parametrize("bad",[None,0,1,"true",[],{}])
def test_invalid_switch_writes_are_rejected(bad):
    from onlyfans_runtime.configuration import validate_patch,InvalidConfiguration
    with pytest.raises(InvalidConfiguration):validate_patch({"nativeReplyFormatEnabled":bad})
    with pytest.raises(InvalidConfiguration):validate_patch({"nativeReplyPacingEnabled":bad})


async def exercise_native_formats():
    from db.engine import engine,get_session
    from db.models import Base,Account,AccountAiConfig,Fan,Message
    from native_pilot.engine import NativeEngine
    from automations import ai_chatter as native

    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    examples=[
        ("Thanks for explaining", "You're welcome."),
        ("Explain the hiking route in detail", "Start at the marked trail near the lake. "*20+"Finish at the lookout."),
        ("Actually I live in Brisbane now", "Got it, Brisbane now."),
        ("Can you help me choose a day?", "Which day works for you?"),
        ("Please keep this confirmation", "Your reference is ABC123. Details: https://example.test/receipt?id=42."),
    ]
    results=[]
    for pacing in (False,True):
        for index,(inbound,output) in enumerate(examples):
            account=str(910001+index+100*int(pacing));fan=920001+index
            async with get_session() as s:
                s.add(Account(id=account,nickname="Synthetic"))
                s.add(AccountAiConfig(account_id=account,ai_chatter_config_json=json.dumps({FORMAT_KEY:True,PACING_KEY:pacing,
                    "language":"en","style_on":False,"nonnative_ai_chatter":False,"typo_ai_chatter":False})))
                s.add(Fan(account_id=account,fan_id=fan))
                s.add(Message(account_id=account,fan_id=fan,message_id=5001,direction="in",body=inbound,
                    created_at=datetime.utcnow()-timedelta(seconds=60)))
                await s.commit()
            sent=[];calls=[]
            async def model(**kw):
                calls.append(kw["purpose"])
                if kw["purpose"]=="ai_chatter":
                    assert FORMAT_INSTRUCTION in kw["messages"][0]["content"]
                    return SimpleNamespace(content=output,parsed=None)
                if kw.get("response_format"):
                    return SimpleNamespace(content="{}",parsed={})
                raise AssertionError("unexpected extra inference "+kw["purpose"])
            def send(fid,text,kwargs):
                assert fid==fan and kwargs=={}
                sent.append(text)
                return {"id":8000+len(sent),"text":text,"createdAt":datetime.utcnow().isoformat(),"fromUser":{"id":int(account)}}
            with NativeEngine(account,fan,chat=model,send=send,reply_format_enabled=True) as current:
                result=await current.run(index+int(pacing)*100+1)
            assert len(sent)==1,(result,sent,calls)
            # Existing texting cleanup may remove the final full stop. All
            # words, references, URLs, amounts and long-answer tail must survive.
            assert output.rstrip(".")==sent[0].rstrip("."),(output,sent[0])
            assert calls.count("ai_chatter")==1
            results.append({"pacing":pacing,"case":index,"characters":len(sent[0]),"bubbles":len(sent),"model_calls":calls})
    await engine.dispose()
    return results


def test_real_native_examples_preserve_brief_long_correction_clarification_and_reference(tmp_path):
    # Run in a fresh interpreter because the application owns one process-local
    # async engine. This fixture has no credentials, live account or provider.
    import subprocess,sys
    env={**os.environ,"DATABASE_URL":"sqlite+aiosqlite:///"+str(tmp_path/"format.db"),"TWIN_FASTT_CONFIG_ISOLATED":"1",
         "PYTHON_DOTENV_DISABLED":"1","PYTHONDONTWRITEBYTECODE":"1"}
    env.pop("ONLYFANS_PROCESS_ROLE",None)
    code="import asyncio,socket,json; from unittest.mock import patch; from test_native_reply_format import exercise_native_formats\ndef deny(*a,**k): raise AssertionError('network forbidden')\nwith patch.object(socket.socket,'connect',deny), patch.object(socket,'create_connection',deny): print(json.dumps(asyncio.run(exercise_native_formats())))"
    run=subprocess.run([sys.executable,"-c",code],env=env,capture_output=True,text=True,timeout=120)
    assert run.returncode==0,run.stdout+run.stderr


def test_complete_response_split_preserves_prices_links_and_required_clarifications():
    from automations.welcome_chatter_for_info import split_for_bubbles
    text="Which date? Which time? Reference ABC123. Price $12.50. https://example.test/receipt?id=42. " + "Required detail. "*50
    assert split_for_bubbles(text,2,force_single=True,preserve_complete=True)==[text.strip()]
