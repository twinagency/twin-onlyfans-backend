"""Restored requested-media captions through the real planner and native boundaries.

Every case has a fresh synthetic SQLite database, blocked sockets and an injected
model/transport. No credentials, real media, paid calls or production settings.
The model is synthetic: these tests prove wiring and deterministic controls,
not the naturalness or factual accuracy of a real model's output.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ACCOUNT, OTHER, FAN = "of-900001", "of-900009", 900002
SELECTED = [7002, 7001, 7003, 7004]
POOL = [7001, 7002, 7003, 7004, 7005]
PHRASE = "me relaxing like we talked about"
CASES = (
    "context", "no_descriptions", "partial_descriptions", "model_failure",
    "malformed", "too_long", "price", "count", "media_word", "link",
    "offplatform", "mixed_media", "removed_before_send", "audience",
    "wire_failure", "voice_guard", "substitute", "unnamed", "native_turn",
    "text_only", "hook_unchanged",
)


@pytest.mark.parametrize("case", CASES)
def test_requested_caption_boundary(case):
    # Modules cache the database engine at import, so each case owns an interpreter.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), case],
        capture_output=True, text=True, timeout=90,
        env={"PATH": os.environ.get("PATH", os.defpath), "PYTHON_DOTENV_DISABLED": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"case": case, "passed": True}


async def exercise(case, tmp):
    from sqlalchemy import select
    from db.engine import engine, get_session
    from db.models import Account, AccountAiConfig, Base, ContentOffer, Employee, Fan, Message, VaultItem
    from automations import content_resolver, pack_sender
    from native_pilot.content import VaultPlan
    from native_pilot.engine import NativeEngine, MODEL
    import audience_include
    import llm_client

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.utcnow()
    cfg = {"pack_send_enabled": True, "pack_on_ask_enabled": True,
           "qualification_gate_enabled": True}
    async with get_session() as session:
        session.add_all([Account(id=ACCOUNT, nickname="Synthetic creator"),
                         Account(id=OTHER, nickname="Other synthetic creator"),
                         Employee(id=1, display_name="Automation", is_active=True)])
        session.add(AccountAiConfig(account_id=ACCOUNT, voice="him", persona="Synthetic saved persona",
                                    ai_chatter_config_json=json.dumps(cfg)))
        session.add_all([Fan(account_id=ACCOUNT, fan_id=FAN),
                         Fan(account_id=ACCOUNT, fan_id=FAN + 1),
                         Fan(account_id=OTHER, fan_id=FAN)])
        messages = [
            (ACCOUNT, FAN, 5000, "out", "we were talking about a relaxed afternoon", False),
            (ACCOUNT, FAN, 5001, "in", "<p>send me relaxing pics in your bedroom</p>", False),
            (OTHER, FAN, 5002, "in", "OTHER_ACCOUNT_SECRET", False),
            (ACCOUNT, FAN + 1, 5003, "in", "OTHER_FAN_SECRET", False),
            (ACCOUNT, FAN, 5004, "in", "UNSENT_SECRET", True),
        ]
        for index, (account, fan, mid, direction, body, unsent) in enumerate(messages):
            session.add(Message(account_id=account, fan_id=fan, message_id=mid,
                                direction=direction, body=body, is_unsent=unsent,
                                automation_kind="ai_chatter" if direction == "out" else None,
                                created_at=now - timedelta(seconds=60 - index)))
        for mid in POOL:
            description = ("POOL_ONLY_BEACH_DESCRIPTION" if mid == 7005 else
                           f"SELECTED_{mid}: A single creator is relaxing in a chair indoors.")
            if case == "no_descriptions" or (case == "partial_descriptions" and mid == 7004):
                description = None
            session.add(VaultItem(account_id=ACCOUNT, media_id=mid,
                                  kind="video" if case == "mixed_media" and mid == 7003 else "photo",
                                  description=description, search_text=description, created_at=now, updated_at_of="2026-10-31T00:00:00+00:00",
                                  ai_fields_json=json.dumps({"people_count": 1, "partner_visible": False})))
        session.add(VaultItem(account_id=OTHER, media_id=7002, kind="photo",
                              description="OTHER_ACCOUNT_MEDIA_SECRET", created_at=now))
        session.add(VaultItem(account_id=ACCOUNT, media_id=7999, kind="photo",
                              description="REMOVED_MEDIA_SECRET", created_at=now, removed_at=now))

    calls, sent = [], []
    invalid = {
        "malformed": ["not a string"], "too_long": "me " + "relaxing " * 12,
        "price": "me relaxing for $5", "count": "me with 9 surprises",
        "media_word": "me with extra photos", "link": "visit example.com",
        "offplatform": "meet me tomorrow",
    }

    async def fake_chat(**kw):
        calls.append(kw)
        assert kw["account_id"] == ACCOUNT and kw["fan_id"] == FAN
        assert kw["model"] == MODEL
        purpose = kw.get("purpose")
        if purpose == "content_resolver_action":
            if case == "model_failure":
                raise RuntimeError("synthetic provider failure")
            data = {"phrase": invalid.get(case, PHRASE)}
        elif purpose == "content_resolver_contract":
            data = {"is_ask": True, "subject": "relaxing", "terms": ["relaxing"],
                    "media_kind": "photo", "company": False, "strict": False,
                    "custom_request": False, "quote": "send me relaxing pics"}
        elif purpose in ("content_resolver_match", "content_resolver_nearest"):
            data = {"media_ids": POOL}
        elif purpose == "content_resolver_verify":
            data = {"verdicts": {str(mid): True for mid in POOL}}
        elif kw.get("response_format"):
            data = {}
        else:
            return SimpleNamespace(content="a relaxed afternoon sounds lovely", parsed=None)
        return SimpleNamespace(content=json.dumps(data), parsed=data)

    async def forbid_legacy(**kw):
        raise AssertionError("model bypassed the native capability")

    async def resolved(account, fan, **kw):
        assert (account, fan) == (ACCOUNT, FAN)
        return content_resolver.Resolution(POOL, kw["contract"])

    async def priced(account, fan, item, avail_ids, cfg, **kw):
        assert (account, fan) == (ACCOUNT, FAN)
        if case == "partial_descriptions":
            # R4 product readiness: an undescribed item is never offered.
            assert 7004 not in avail_ids and {7001, 7002, 7003} <= set(avail_ids)
            return SimpleNamespace(media=[m for m in SELECTED if m != 7004], price_cents=500, value_cents=500, refusal=None)
        assert set(SELECTED) <= set(avail_ids)
        return SimpleNamespace(media=list(SELECTED), price_cents=500, value_cents=500, refusal=None)

    content = VaultPlan(ACCOUNT, FAN, str(Path(tmp) / "fixture.db"))

    def send(fan, caption, kwargs):
        assert fan == FAN
        sent.append((fan, caption, kwargs))
        if case == "wire_failure":
            raise RuntimeError("synthetic uncertain transport")
        receipt = {"id": 9000 + len(sent), "text": caption}
        if kwargs:
            terms = content.terms(kwargs)
            assert terms["price_cents"] == 500 and terms["media_ids"] == SELECTED
            assert terms["preview_ids"] == []
            assert kwargs == {"price": 5.0, "locked_text": False, "media_files": SELECTED}
        return receipt

    async def audience(account, fan, **kw):
        assert (account, fan) == (ACCOUNT, FAN)
        return case != "audience", "synthetic-audience"

    # Real selected-media/thread reads, claim composition, audits and persistence.
    # Only selection/pricing decisions and external model/transport are injected.
    with patch.object(llm_client, "chat", forbid_legacy), \
            patch.object(pack_sender, "negotiate_pack", priced), \
            patch.object(audience_include, "audience_allows_fan", audience):
        with NativeEngine(ACCOUNT, FAN, chat=fake_chat, send=send,
                          content=None if case == "text_only" else content) as native:
            if case == "native_turn":
                result = await native.run(1)
                assert not result.get("errors"), result
                cards = [row for row in sent if row[2]]
                assert len(cards) == 1, (result, sent, [c.get("purpose") for c in calls])
                assert PHRASE not in cards[0][1], cards
                assert not any(c.get("purpose") == "content_resolver_action" for c in calls)
            elif case == "text_only":
                assert await content_resolver.action_phrase(ACCOUNT, FAN, SELECTED) == ""
                assert not calls and not sent
            elif case in ("substitute", "unnamed"):
                from automations.pack_claim import ask_clause, substitute_clause
                claim = await pack_sender._ask_claim(
                    ACCOUNT, FAN, SELECTED, subject="relaxing" if case == "substitute" else None,
                    substitute=case == "substitute",
                    clause=substitute_clause if case == "substitute" else ask_clause)
                assert PHRASE in claim.text and claim.n == 4
                if case == "substitute":
                    assert claim.text.startswith("not exactly what you asked for but")
                assert all("CONVERSATION" not in c["messages"][1]["content"] for c in calls)
            else:
                with patch.object(content_resolver, "resolve", resolved):
                    contract = content_resolver.Contract(is_ask=True, subject="relaxing", media_kind="photo")
                    delivery, refusal = await pack_sender.plan_ask_delivery(
                        ACCOUNT, FAN, contract, cfg=cfg,
                        item_tag=pack_sender.HOOK_CATEGORY if case == "hook_unchanged" else pack_sender.ASK_ITEM_TAG)
                if case == "no_descriptions":
                    # R4 product readiness: nothing described means nothing offerable (contract change, F05/F08).
                    assert delivery is None and refusal["reason"] == pack_sender.REFUSE_TOO_THIN, refusal
                    assert not sent and not calls
                    return
                if case == "partial_descriptions":
                    assert delivery is not None, refusal
                    assert 7004 not in delivery.plan.media and delivery.plan.claim.n == 3, delivery.plan
                    assert not sent
                    return
                assert delivery is not None, refusal
                assert delivery.plan.media == SELECTED and delivery.plan.price_cents == 500
                assert delivery.plan.previews == [] and delivery.plan.claim.n == 4
                basic = "3 pics + 1 vid of relaxing" if case == "mixed_media" else "4 pics of relaxing"
                # A named matching request never invokes optional caption generation.
                assert delivery.plan.claim.text == basic
                assert not calls
                if case == "removed_before_send":
                    async with get_session() as session:
                        row = await session.get(VaultItem, (ACCOUNT, SELECTED[0]))
                        row.removed_at = now
                result = await pack_sender.deliver(
                    native.make_client(ACCOUNT), delivery,
                    voice_line="meet me tomorrow" if case == "voice_guard" else None)
                if case in ("removed_before_send", "audience"):
                    assert result["status"] == "refused" and not sent, result
                elif case == "wire_failure":
                    assert result["status"] == "error" and len(sent) == 1, result
                else:
                    assert result["status"] == "ok" and len(sent) == 1, result
                    assert result["caption"] == delivery.plan.claim.text
                    async with get_session() as session:
                        offers = (await session.execute(select(ContentOffer))).scalars().all()
                        assert len(offers) == 1 and offers[0].fan_id == FAN and offers[0].account_id == ACCOUNT

    actions = [c for c in calls if c.get("purpose") == "content_resolver_action"]
    if case in ("substitute", "unnamed"):
        assert len(actions) == 1, [c.get("purpose") for c in calls]
        system, facts = [m["content"] for m in actions[0]["messages"]]
        assert all(f"SELECTED_{mid}" in facts for mid in SELECTED)
        assert facts.index("SELECTED_7002") < facts.index("SELECTED_7001")
        assert "CONVERSATION" not in facts and "SAVED TEXTING STYLE" not in facts
        assert "bedroom" not in facts
        assert all(secret not in facts for secret in (
            "OTHER_ACCOUNT_SECRET", "OTHER_FAN_SECRET", "UNSENT_SECRET",
            "OTHER_ACCOUNT_MEDIA_SECRET", "POOL_ONLY_BEACH_DESCRIPTION", "REMOVED_MEDIA_SECRET"))
    else:
        assert not actions, [c.get("purpose") for c in calls]
    # None of the caption work edits the account's saved model/persona/sales settings.
    async with get_session() as session:
        row = await session.get(AccountAiConfig, ACCOUNT)
        assert row.voice == "him" and row.persona == "Synthetic saved persona"
        assert json.loads(row.ai_chatter_config_json) == cfg
    await engine.dispose()


def main(case):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="requested-caption-") as tmp:
        env = {"DATABASE_URL": "sqlite+aiosqlite:///" + (Path(tmp) / "fixture.db").as_posix(),
               "PYTHON_DOTENV_DISABLED": "1"}
        with patch.dict(os.environ, env, clear=True):
            def deny(*a, **kw):
                raise AssertionError("network forbidden")
            with patch.object(socket.socket, "connect", deny), \
                    patch.object(socket.socket, "connect_ex", deny), \
                    patch.object(socket, "create_connection", deny):
                asyncio.run(exercise(case, tmp))
    print(json.dumps({"case": case, "passed": True}))


if __name__ == "__main__":
    main(sys.argv[1])

