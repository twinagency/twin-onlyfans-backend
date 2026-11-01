"""R5 full path: a real FASTT fan turn (NativeEngine, real ContextualAuthority contract, real SellLane/resolver/pack
pricing/pack_sender), synthetic model and provider. Proves protection AND progress:

  * standard initiative: current fan interest -> one card (unchanged), a decision row says why;
  * lower initiative with no pacing pressure: an INVITATION about real content (stored), no card;
  * the fan's short "yes" accepts THAT invitation: a card for exactly the invited media, no re-proof of interest;
  * a "yes" with no stored invitation sells nothing;
  * contextual mode off writes no decision rows.
Run directly (it needs its own database before the engine is imported); pytest runs it in a subprocess.
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
MEDIA = list(range(7001, 7008))


async def exercise():
    from sqlalchemy import select
    from db.engine import engine, get_session
    from db.models import Base, Account, AccountAiConfig, Fan, Message, VaultItem, ContentOffer
    from db.onlyfans_contextual_models import OfContextualOffer  # noqa: F401
    from db.onlyfans_sales_models import OfSalesDecision, OfSalesInvitation
    from native_pilot.content import VaultPlan
    from native_pilot.engine import NativeEngine
    from onlyfans_runtime.configuration import SALES_FLAGS
    from onlyfans_runtime.contextual_mode import KEY

    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    async def setup(account, fan, mode, extra=None, fan_text="I love your gym workout photos"):
        now = datetime.utcnow()
        async with get_session() as s:
            s.add(Account(id=account, nickname="Synthetic creator"))
            s.add(AccountAiConfig(account_id=account, ai_chatter_config_json=json.dumps(
                {**{k: True for k in SALES_FLAGS}, KEY: mode, "language": "en", **(extra or {})})))
            s.add(Fan(account_id=account, fan_id=fan))
            s.add(Message(account_id=account, fan_id=fan, message_id=5001, direction="in", body=fan_text,
                          created_at=now - timedelta(seconds=300)))
            for mid in MEDIA:
                s.add(VaultItem(account_id=account, media_id=mid, kind="photo", updated_at_of="2026-10-31T00:00:00+00:00",
                                description="gym workout mirror photo, creator alone", content_hash="h%d" % mid,
                                search_text="gym workout mirror photo, creator alone", tags="[]", created_at=now - timedelta(days=1)))
            await s.commit()

    def make_model(calls, hook=True):
        async def model(**kw):
            purpose = kw["purpose"]
            calls.append(purpose)
            if purpose == "ai_chatter":
                return SimpleNamespace(content="How did your workout go?", parsed=None)
            data = {
                "content_resolver_contract": {"is_ask": True, "same_item_requested": False, "subject": None, "company": False,
                                              "quote": "show me", "terms": []},
                "content_resolver_hook": {"hook": True, "source": "situation", "action": "send_ppv", "what": "fan is at the gym",
                    "subject": "me at the gym", "media_kind": "photo", "terms": ["gym", "workout", "mirror"],
                    "bridge": "love a good workout", "reason": "fan expresses current interest in gym photos",
                    "evidence": [{"id": 5001, "quote": "I love your gym workout photos"}], "supporting_message_ids": [5001]},
                "content_resolver_match": {"media_ids": MEDIA},
                "content_resolver_verify": {"verdicts": {str(i): True for i in MEDIA}},
                "content_resolver_action": {"phrase": "me at the gym"},
                "gather_close_answer": {"line": "love a good workout"},
            }.get(purpose)
            if data is None:
                raise AssertionError("unexpected model purpose " + purpose)
            return SimpleNamespace(content=json.dumps(data), parsed=data)
        return model

    def make_authority(mode, source, events):
        class Authority:
            source_message_id = source
            armed = None
            reply_override = None
            emit = staticmethod(events.append)

            def check(self, required):
                assert required == mode
                return {"mode": mode, "revision": 1, "binding": "fixture"}

            @contextmanager
            def arm(self, d, p, r, ids):
                self.armed = d
                try:
                    yield
                finally:
                    self.armed = None
        return Authority()

    async def turn(account, fan, mode, source, number, sent, calls, events):
        def send(fid, text, kwargs):
            sent.append((text, kwargs))
            return {"id": 8000 + len(sent) + 100 * number, "text": text, "createdAt": datetime.utcnow().isoformat(),
                    "fromUser": {"id": int(account)}, "price": kwargs.get("price", 0),
                    "media": [{"id": i, "type": "photo"} for i in kwargs.get("media_files", [])],
                    "previews": kwargs.get("previews", [])}
        with NativeEngine(account, fan, chat=make_model(calls), send=send, content=VaultPlan(account, fan),
                          contextual=make_authority(mode, source, events)) as native:
            return await native.run(number)

    async def rows(model, account):
        async with get_session() as s:
            return (await s.execute(select(model).where(model.account_id == account))).scalars().all()

    # 1. standard (default) initiative, live: today's single card, plus a decision row saying why.
    sent, calls, events = [], [], []
    await setup("900101", 910101, "live")
    result = await turn("900101", 910101, "live", 5001, 1, sent, calls, events)
    cards = [(t, k) for t, k in sent if k.get("media_files")]
    assert len(cards) == 1 and result["replies_sent"] == 1, (sent, events)
    decisions = await rows(OfSalesDecision, "900101")
    assert [(d.action, d.outcome, d.mode) for d in decisions] == [("proactive_offer", "acted", "live")], decisions
    assert json.loads(decisions[0].settings_json)["initiative"] == "standard"
    assert decisions[0].grouped_turns == 0 and decisions[0].raw_bubbles == 0
    assert not await rows(OfSalesInvitation, "900101")

    # 2. lower initiative, live, no pacing pressure: an invitation about real content, NO card.
    sent, calls, events = [], [], []
    await setup("900102", 910102, "live", {"fastt_commercial_initiative": "lower"})
    await turn("900102", 910102, "live", 5001, 2, sent, calls, events)
    assert sent and all(not k.get("media_files") for _, k in sent), sent
    assert any("want me to show you" in t.lower() for t, _ in sent), sent
    invitations = await rows(OfSalesInvitation, "900102")
    assert len(invitations) == 1 and invitations[0].state == "open" and invitations[0].source_message_id == 5001
    invited = json.loads(invitations[0].media_ids_json)
    assert set(invited) <= set(MEDIA) and invited
    decisions = await rows(OfSalesDecision, "900102")
    assert [(d.action, d.outcome, d.approach) for d in decisions] == [("invite", "acted", "invite")], decisions
    assert not await rows(ContentOffer, "900102")          # an invitation is not a paid card

    # 3. his short "yes" accepts THAT invitation: a card for exactly the invited media. The fan-interest reader is
    #    not consulted again (no hook read), and nothing is charged here: this only presents the locked card.
    async with get_session() as s:
        from sqlalchemy import delete
        await s.execute(delete(Message).where(Message.account_id == "900102", Message.message_id >= 8000))   # engine-written echo
        (await s.get(Fan, ("900102", 910102))).automation_paused_until = None                              # time has passed
        s.add(Message(account_id="900102", fan_id=910102, message_id=5002, direction="out", body=" ".join(t for t, _ in sent), automation_kind="ai_chatter",
                      created_at=datetime.utcnow() - timedelta(seconds=120),
                      ingested_at=datetime.utcnow() - timedelta(seconds=120)))
        s.add(Message(account_id="900102", fan_id=910102, message_id=5003, direction="in", body="yes",
                      created_at=datetime.utcnow() - timedelta(seconds=30),
                      ingested_at=datetime.utcnow() - timedelta(seconds=30)))
        await s.commit()
    sent2, calls2, events2 = [], [], []
    await turn("900102", 910102, "live", 5003, 3, sent2, calls2, events2)
    cards = [(t, k) for t, k in sent2 if k.get("media_files")]
    assert len(cards) == 1 and cards[0][1]["price"] > 0, (sent2, events2, calls2)
    assert set(cards[0][1]["media_files"]) <= set(invited), (cards, invited)
    assert "content_resolver_hook" not in calls2, calls2
    invitations = await rows(OfSalesInvitation, "900102")
    assert invitations[0].state == "accepted" and invitations[0].accepted_turn_id == 5003
    kinds = {(d.source_message_id, d.action, d.reason) for d in await rows(OfSalesDecision, "900102")}
    assert (5003, "direct_offer", "invitation_accepted") in kinds, kinds
    assert len(await rows(ContentOffer, "900102")) == 1

    # 4. a bare "yes" with NO stored invitation sells nothing from this path.
    sent3, calls3, events3 = [], [], []
    await setup("900103", 910103, "live", None, fan_text="yes")
    await turn("900103", 910103, "live", 5001, 4, sent3, calls3, events3)
    assert not await rows(OfSalesInvitation, "900103")
    assert not [1 for _, k in sent3 if k.get("media_files")], sent3

    # 5. contextual mode off: no decision rows, normal reply.
    sent4, calls4, events4 = [], [], []
    await setup("900104", 910104, "off")
    await turn("900104", 910104, "off", 5001, 5, sent4, calls4, events4)
    assert not await rows(OfSalesDecision, "900104") and not await rows(OfSalesInvitation, "900104")

    await engine.dispose()
    print(json.dumps({"active_selling_turn": "ok"}))


def test_active_selling_full_turn():
    done = subprocess.run([sys.executable, __file__], capture_output=True, text=True, timeout=600,
                          env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert done.returncode == 0 and "active_selling_turn" in done.stdout, done.stdout[-3000:] + done.stderr[-3000:]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="active-selling-") as tmp:
        os.environ["TWIN_FASTT_CONFIG_ISOLATED"] = "1"
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + str(Path(tmp) / "test.db")
        os.environ.pop("ONLYFANS_PROCESS_ROLE", None)

        def deny(*args, **kwargs):
            raise AssertionError("network forbidden in fixture")
        with asyncio.Runner() as runner:
            runner.get_loop()
            with patch.object(socket.socket, "connect", deny), patch.object(socket, "create_connection", deny):
                runner.run(exercise())
