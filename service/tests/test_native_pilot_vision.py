"""Fan photo vision in the native runner: FASTT's own inbound_describe, through the capability seam.

Synthetic data, fake transport / fake OnlyFans client, blocked sockets. Covers: the vision model adapter
(route, budget, fail-closed), the GET-only media client, the capability's refusals, the stock describe path
end to end, intake (flag kept, description in background), the ledger (described photo answerable, bounded
wait), the provider-history check, and that the AI prompt carries ``[he sent: ...]`` for a photo-only turn."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import socket
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COUNT = 0
ACCOUNT, FAN = "900001", 900002
PHOTO_URL = "https://cdn2.onlyfans.com/files/synthetic/photo.jpg"


def check(condition, label=""):
    global COUNT
    assert condition, label
    COUNT += 1


def tiny_jpeg():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (120, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class FakeHttp:
    def __init__(self):
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append(url)
        return SimpleNamespace(status_code=200, content=tiny_jpeg())


class FakeClient:
    """Stands in for the runner's verified client: it must only ever be asked ``http.get``."""
    timeout_s = 5

    def __init__(self):
        self.http = FakeHttp()

    def __getattr__(self, name):
        raise AssertionError("verified client touched beyond http.get: " + name)


def vision_envelope(text="a selfie of a man in a gym mirror, shirt off, sfw", model="qwen/qwen3-vl-30b-a3b-instruct"):
    return {"model": model, "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 30, "cost": 0.0002}}


async def model_and_capability(photo_vision, NativePilotModelError):
    from native_pilot.scope import AccountScope
    scope = AccountScope.parse(ACCOUNT)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=vision_envelope())

    def make(**kw):
        return photo_vision.NativeVisionModel(
            api_key="synthetic-key", account_id=scope.db_id, fan_id=FAN, deadline=asyncio.get_running_loop().time() + 0,
            should_stop=lambda: False, on_event=lambda e: None, transport=httpx.MockTransport(handler), **kw)
    return scope, seen, handler


async def exercise(tmp):
    import time
    from sqlalchemy import select
    from db.engine import engine, get_session
    from db.models import (Account, AccountAiConfig, Base, Employee, Fan, GrokCall, GrokDailyCost, Message)
    from native_pilot import photo_vision, runner
    from native_pilot.engine import NativeEngine
    from native_pilot.intake import Intake
    from native_pilot.model import NativePilotModelError
    from native_pilot.scope import AccountScope
    from telegram_runtime.capabilities import active_telegram_capabilities
    import inbound_describe
    import llm_client

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.utcnow()
    scope = AccountScope.parse(ACCOUNT)
    async with get_session() as s:
        s.add(Account(id=ACCOUNT, nickname="Synthetic"))
        s.add(Employee(id=1, display_name="Automation", is_active=True))
        s.add(Fan(account_id=ACCOUNT, fan_id=FAN))
    async with get_session() as s:
        s.add(AccountAiConfig(account_id=ACCOUNT, daily_cost_cap_cents=500))

    # 1. The adapter: route, provider, request shape, strict envelope, budget accounting.
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=vision_envelope())

    def model(**kw):
        defaults = dict(api_key="synthetic-key", account_id=scope.db_id, fan_id=FAN, deadline=time.monotonic() + 30,
                        should_stop=lambda: False, on_event=lambda e: None, max_calls=1,
                        transport=httpx.MockTransport(handler))
        defaults.update(kw)
        return photo_vision.NativeVisionModel(**defaults)

    from llm_models import MODELS
    check(photo_vision.NativeVisionModel.API_MODEL == MODELS[photo_vision.VISION_MODEL].api_model, "api model matches registry")
    content = [{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]
    messages = [{"role": "user", "content": content}]
    m = model()
    res = await m.chat(model=photo_vision.VISION_MODEL, messages=messages, purpose="describe_inbound_image",
                       account_id=scope.db_id, fan_id=FAN, temperature=0.2)
    check(res.content.startswith("a selfie") and seen[0]["model"] == "qwen/qwen3-vl-30b-a3b-instruct", "described via the vision route")
    check(seen[0]["provider"] == {"order": ["deepinfra"], "allow_fallbacks": False}, "provider pinned, no fallback")
    check(seen[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg"), "image part sent")
    for bad, why in ((dict(model="deepseek/deepseek-v4.1-flash"), "text model refused on the vision adapter"),
                     (dict(model="qwen3-vl-30b"), "legacy deepinfra name refused"),
                     (dict(fan_id=FAN + 1), "other fan refused"),
                     (dict(account_id="123"), "other account refused")):
        args = dict(model=photo_vision.VISION_MODEL, messages=messages, purpose="describe_inbound_image",
                    account_id=scope.db_id, fan_id=FAN)
        args.update(bad)
        try:
            await model().chat(**args)
        except NativePilotModelError:
            check(True, why)
        else:
            raise AssertionError(why)
    # the text adapter still refuses the vision model
    from native_pilot.model import NativePilotModel
    try:
        await NativePilotModel(api_key="synthetic-key", account_id=scope.db_id, fan_id=FAN, deadline=time.monotonic() + 30,
                               should_stop=lambda: False, on_event=lambda e: None).chat(
            model=photo_vision.VISION_MODEL, messages=[{"role": "user", "content": "x"}], purpose="ai_chatter",
            account_id=scope.db_id, fan_id=FAN)
    except NativePilotModelError:
        check(True, "text adapter refuses the vision model")
    else:
        raise AssertionError("accepted")
    # a response naming another model, and an incomplete one, fail closed
    for env in (vision_envelope(model="some/other-model"),
                {**vision_envelope(), "choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "x"}}]}):
        try:
            await model(transport=httpx.MockTransport(lambda r, env=env: httpx.Response(200, json=env))).chat(
                model=photo_vision.VISION_MODEL, messages=messages, purpose="describe_inbound_image",
                account_id=scope.db_id, fan_id=FAN)
        except NativePilotModelError:
            check(True, "bad envelope fails closed")
        else:
            raise AssertionError("accepted")
    # the larger request limit applies to the vision adapter only
    check(photo_vision.NativeVisionModel.REQUEST_LIMIT > NativePilotModel.REQUEST_LIMIT, "vision request limit")

    # 2. The media client: GET only, onlyfans.com https only.
    fake = FakeClient()
    mc = photo_vision.MediaClient(fake)
    check(mc.http.get(PHOTO_URL).status_code == 200 and fake.http.gets == [PHOTO_URL], "cdn url fetched through the client")
    for url in ("http://cdn2.onlyfans.com/x.jpg", "https://evil.example/x.jpg", "https://onlyfans.com.evil.example/x.jpg",
                "https://user@cdn2.onlyfans.com/x.jpg", "https://cdn2.onlyfans.com:8443/x.jpg", "file:///etc/passwd",
                "https://notonlyfans.com/x.jpg", ""):
        try:
            mc.http.get(url)
        except photo_vision.VisionRefused:
            check(True, "refused " + url)
        else:
            raise AssertionError("fetched " + url)
    check(not hasattr(mc, "post") and not hasattr(mc.http, "post") and not hasattr(mc.http, "request"), "no write verbs")

    # 3. Capability: only the vision model; everything else refuses.
    cap = photo_vision.VisionCapability(scope, FAN, model(), fake)
    check(await cap.resolve_model(scope.db_id, "describe_inbound_image") == photo_vision.VISION_MODEL, "vision purpose resolves")
    for coro in (cap.resolve_model(scope.db_id, "ai_chatter"), cap.resolve_model("123", "describe_inbound_image"),
                 cap.resolve_model(scope.db_id, "describe_inbound_image", "deepseek/deepseek-v4.1-flash"),
                 cap.chat(model=photo_vision.VISION_MODEL, messages=messages, purpose="ai_chatter",
                          account_id=scope.db_id, fan_id=FAN),
                 cap.chat(model="deepseek/deepseek-v4.1-flash", messages=messages, purpose="describe_inbound_image",
                          account_id=scope.db_id, fan_id=FAN),
                 cap.load_config(scope.db_id, {}), cap.enqueue_job(scope.db_id, "x"), cap.resolve_open_offers(scope.db_id, None, {}),
                 cap.hold(scope.db_id, FAN, 1)):
        try:
            await coro
        except photo_vision.VisionRefused:
            check(True, "capability refusal")
        else:
            raise AssertionError("capability accepted")
    try:
        cap.make_client("123")
    except photo_vision.VisionRefused:
        check(True, "other account's client refused")

    # 4. Stock describe path end to end. A photo-only inbound row (what the transcoder stores).
    raw = {"id": 5001, "text": "", "media": [{"type": "photo", "files": {"thumb": {"url": PHOTO_URL}}}], "mediaCount": 1}
    async with get_session() as s:
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=5001, direction="in", body="", media_count=1,
                      raw_json=json.dumps(raw), created_at=now - timedelta(seconds=40)))

    def forbid(**kw):
        raise AssertionError("legacy llm_client.chat reached")

    async def legacy_forbid(**kw):
        forbid(**kw)

    events, seen[:] = [], []
    fake = FakeClient()
    describe = photo_vision.make_describer(scope, fake, api_key="synthetic-key", emit=events.append,
                                           transport=httpx.MockTransport(handler))
    with patch.object(llm_client, "chat", legacy_forbid):
        desc = await describe(FAN, 5001)
    check(desc and desc.startswith("a selfie"), ("described", desc))
    check(active_telegram_capabilities() is None, "capability reset after describing")
    check(fake.http.gets == [PHOTO_URL], "frame fetched through the verified client")
    check(len(seen) == 1 and seen[0]["model"] == "qwen/qwen3-vl-30b-a3b-instruct", "one vision call")
    check(any(e["event"] == "model-complete" and e["purpose"] == "describe_inbound_image" for e in events), "model events audited")
    async with get_session() as s:
        row = (await s.execute(select(Message).where(Message.message_id == 5001))).scalar_one()
        calls = (await s.execute(select(GrokCall))).scalars().all()
        spent = (await s.execute(select(GrokDailyCost))).scalars().all()
    check(row.image_desc == desc, "stored on messages.image_desc (FASTT's own column)")
    check(len(calls) == 1 and calls[0].status == "done" and calls[0].model == photo_vision.VISION_MODEL, "budget row settled")
    check(len(spent) == 1 and spent[0].cost_cents == 2 and spent[0].call_count == 1, ("daily budget charged", [s.cost_cents for s in spent]))
    # already described: no second model call (stock cache gate)
    n = len(seen)
    check(await describe(FAN, 5001) == desc and len(seen) == n, "cached description is reused")
    # failure modes leave image_desc empty and never raise
    async with get_session() as s:
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=5002, direction="in", body="", media_count=1,
                      raw_json=json.dumps(raw), created_at=now - timedelta(seconds=30)))
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=5003, direction="in", body="", media_count=1,
                      raw_json=json.dumps({**raw, "media": [{"type": "photo", "files": {"thumb": {"url": "https://evil.example/a.jpg"}}}]}),
                      created_at=now - timedelta(seconds=20)))
    broken = photo_vision.make_describer(scope, FakeClient(), api_key="synthetic-key", emit=events.append,
                                         transport=httpx.MockTransport(lambda r: httpx.Response(500, json={})))
    check(await broken(FAN, 5002) is None, "provider error: no description")
    check(await describe(FAN, 5003) is None, "foreign media host: nothing fetched, no description")
    async with get_session() as s:
        left = (await s.execute(select(Message.message_id).where(Message.image_desc.is_(None), Message.message_id.in_((5002, 5003))))).all()
    check(len(left) == 2, "failed descriptions leave the row undescribed")
    # FASTT's own per-account switch (tip_reward image_describe_enabled) is honoured
    with patch("automations.tip_reward_config.image_describe_flags", lambda a: asyncio.sleep(0, (False, "", "all"))):
        off = photo_vision.make_describer(scope, FakeClient(), api_key="synthetic-key", emit=events.append,
                                          transport=httpx.MockTransport(handler))
        n = len(seen)
        check(await off(FAN, 5002) is None and len(seen) == n, "describe switched off: no call")

    # 5. Legacy path unchanged with no capability (llm_client.chat is what is called).
    called = []

    async def legacy_chat(**kw):
        called.append(kw["model"])
        return SimpleNamespace(content="a legacy described photo, sfw and plain")
    with patch.object(llm_client, "chat", legacy_chat), patch.object(inbound_describe.ax, "_make_client", lambda a: fake):
        out = await inbound_describe._vision_desc(ACCOUNT, FAN, 5002, [PHOTO_URL])
    check(out == "a legacy described photo, sfw and plain" and called == ["qwen3-vl-30b"], ("legacy path", out, called))

    # 6. Intake: the flag is kept and the description runs in the background; tip/paid/gif are never described.
    class Spy:
        def __init__(self):
            self.attention, self.stored, self.events, self.described = [], set(), [], []

        async def transcode(self, account, message):
            self.stored.add(message["id"])

        async def flag(self, account, fan, reason):
            self.attention.append(reason)
            return True

    spy = Spy()
    gate = asyncio.Event()

    async def slow_describe(fan, mid):
        spy.described.append(mid)
        await gate.wait()
        return "a described photo"

    scope2 = AccountScope.parse(ACCOUNT)
    path = Path(tmp) / "intake.db"
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("CREATE TABLE messages(account_id TEXT,fan_id INTEGER,message_id INTEGER)")

    class IntakeT(Intake):
        def _exists(self, message_id, fan=None):
            return message_id in spy.stored

    intake = IntakeT(scope2, FAN, path, transcode=spy.transcode, attention=spy.flag, emit=spy.events.append,
                     describe=slow_describe)

    def frame(mid, **extra):
        return {"api2_chat_message": {"id": mid, "createdAt": now.isoformat() + "Z", "fromUser": {"id": FAN}, **extra}}
    check(await intake.handle(frame(7001, media=[{"id": 1}], mediaCount=1)) == "unsupported", "photo is still 'unsupported'")
    check(spy.attention == ["unsupported-input:media"], "requires_attention flag raised for the photo")
    await asyncio.sleep(0)
    check(spy.described == [7001] and len(intake._describing) == 1, "description started in the background, handle returned")
    for mid, extra in ((7002, {"isTip": True}), (7003, {"price": 3.0, "media": [{"id": 2}], "mediaCount": 1}),
                       (7004, {"giphyId": "abc"}), (7005, {"text": "hi"})):
        await intake.handle(frame(mid, **extra))
    await asyncio.sleep(0)
    check(spy.described == [7001], "tip, paid, gif and plain text are never described")
    gate.set()
    await intake.drain()
    done = [e for e in spy.events if e["event"] == "media-described"]
    check(done == [{"event": "media-described", "message_id": 7001, "fan_id": FAN, "described": True, "chars": 17}],
          ("event carries no description text", done))
    # a describe that raises is reported, never fatal
    async def boom(fan, mid):
        raise RuntimeError("secret provider text")
    spy2 = Spy()
    intake2 = IntakeT(scope2, FAN, path, transcode=spy2.transcode, attention=spy2.flag, emit=spy2.events.append, describe=boom)
    spy.stored.add(7010)
    await intake2.handle(frame(7011, media=[{"id": 1}], mediaCount=1))
    spy.stored.add(7011)   # same stored-set trick as above is per-instance; _exists reads spy.stored
    await intake2.drain()
    check(not any("secret" in json.dumps(e) for e in spy2.events), "no provider text in events")
    # no describe hook = old behaviour: nothing spawned
    plain = IntakeT(scope2, FAN, path, transcode=spy.transcode, attention=spy.flag, emit=lambda e: None)
    await plain.handle(frame(7020, media=[{"id": 1}], mediaCount=1))
    check(not plain._describing, "no hook, no task")
    # stop cancels in-flight descriptions
    gate2 = asyncio.Event()

    async def hang(fan, mid):
        await gate2.wait()
    spy3 = Spy()
    intake3 = IntakeT(scope2, FAN, path, transcode=spy3.transcode, attention=spy3.flag, emit=spy3.events.append, describe=hang)
    await intake3.handle(frame(7030, media=[{"id": 1}], mediaCount=1))
    await asyncio.sleep(0)
    await intake3.stop()
    await asyncio.sleep(0)
    check(all(t.done() for t in list(intake3._describing)) or not intake3._describing, "stop cancels descriptions")

    # 7. The AI reads it: a photo-only turn, described, is answered with "[he sent: ...]" in its prompt.
    await reset_fan_rows(get_session, Message, select, FAN)
    async with get_session() as s:
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=5101, direction="in", body="", media_count=1,
                      image_desc="a selfie of a man in a gym mirror, shirt off, sfw", raw_json=json.dumps(raw),
                      created_at=datetime.utcnow() - timedelta(seconds=60)))
    prompts, sent = [], []

    async def chat(**kw):
        if kw.get("response_format"):
            return SimpleNamespace(content="{}", parsed={})
        prompts.append(json.dumps(kw["messages"]))
        return SimpleNamespace(content="whoa nice gym", parsed=None)

    def send(fid, text, kw):
        sent.append(text)
        return {"id": 9100 + len(sent), "text": text, "createdAt": datetime.utcnow().isoformat(), "fromUser": {"id": 900001}}
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=send) as eng:
        stats = await eng.run(1)
    check(stats["replies_sent"] == 1 and sent == ["whoa nice gym"], ("photo-only turn answered", stats.get("replies_sent"), sent, stats.get("skip_counts")))
    check(any("[he sent: a selfie of a man in a gym mirror" in p for p in prompts), "prompt carries FASTT's description")
    await engine.dispose()


async def reset_fan_rows(get_session, Message, select, fan):
    async with get_session() as s:
        for m in (await s.execute(select(Message).where(Message.fan_id == fan))).scalars():
            await s.delete(m)


def ledger_and_history():
    """Ledger: described photo answerable, bounded wait; runner: provider-history accepts a described photo."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_native_pilot_ledger as tl
    from native_pilot import runner
    from native_pilot.ledger import Ledger, Refused

    with tempfile.TemporaryDirectory(prefix="native-vision-ledger-") as directory:
        root = Path(directory)

        def fixture(name, wait=45):
            f = tl.Fixture(root, name)
            f.sql("ALTER TABLE messages ADD COLUMN image_desc TEXT")
            f.ledger.describe_wait_s = wait
            f.activate()
            return f

        photo = dict(body="", media_count=1, media_ids="[9]")
        # undescribed, inside the wait: hold (no snapshot) so the AI never answers around a photo mid-description
        f = fixture("wait")
        f.source(100, **photo)
        check(f.ledger.snapshot(**f.call) is None, "undescribed photo inside the window holds the chat")
        # described: photo-only input is answerable
        f.sql("UPDATE messages SET image_desc='a selfie, sfw' WHERE message_id=100")
        snap = f.ledger.snapshot(**f.call)
        check(snap is not None and snap.member_ids == (100,), "described photo-only message is an input")
        key = f.ledger.claim_input(**f.call, snapshot=snap)
        # the description landing after the claim does not change the claimed source (image_desc is not hashed)
        g = fixture("late")
        g.source(100)
        gk = g.claim()
        g.source(101, created_at="2026-10-16 12:00:04", **photo)
        g.sql("UPDATE messages SET image_desc='late desc' WHERE message_id=101")
        a = g.ledger.snapshot(**g.call)
        check(a is not None and a.member_ids == (100, 101), "described photo after text joins the burst")
        # past the wait with no description: skipped exactly as before
        h = fixture("expired")
        h.source(100, **photo)
        late = dict(h.call, now=tl.NOW + timedelta(seconds=2 + 46))
        check(h.ledger.snapshot(**late) is None, "undescribed after the window: no input (as before)")
        h.source(101, created_at="2026-10-16 12:00:04")
        late_snap = h.ledger.snapshot(**late)
        check(late_snap is not None and late_snap.member_ids == (101,), "undescribed photo skipped, his text answered")
        # waiting is not forever for others: described=0 (service off) keeps the old rule untouched
        o = fixture("off", wait=0)
        o.source(100, **photo)
        o.sql("UPDATE messages SET image_desc='a selfie'")
        check(o.ledger.snapshot(**o.call) is None, "describe_wait_s=0: descriptions are not used at all")
        # tip/paid/gif-ish rows are never held or promoted by a description
        for n, extra in enumerate(({"is_tip": 1}, {"price_cents": 5})):
            t = fixture("money" + str(n))
            t.source(100, body="", media_count=1, media_ids="[9]", **extra)
            t.sql("UPDATE messages SET image_desc='x'")
            check(t.ledger.snapshot(**t.call) is None, f"money row never becomes an input {extra}")

    # provider history: a described photo frame is accepted, everything risky still refused.
    src = {100: {"message_id": 100, "body": "", "created_at": "2026-10-16 12:00:01.000", "described": True}}

    def item(**extra):
        base = {"id": 100, "createdAt": "2026-10-16T12:00:01.000Z", "fromUser": {"id": FAN_ID}, "text": None,
                "media": [{"id": 1, "type": "photo"}], "mediaCount": 1, "price": 0}
        base.update(extra)
        return base
    FAN_ID = tl.FAN
    runner.check_provider_history([item()], src, set(), account=tl.ACCT, fan=tl.FAN)
    check(True, "described photo frame accepted")
    runner.check_provider_history([item(text="")], src, set(), account=tl.ACCT, fan=tl.FAN)
    undescribed = {100: {**src[100], "described": False}}
    for label, history, sources in (("undescribed photo", [item()], undescribed),
                                    ("tip", [item(isTip=True)], src), ("locked", [item(lockedText=True)], src),
                                    ("gif", [item(giphyId="a", media=[], mediaCount=0)], src),
                                    ("previews", [item(previews=[1])], src), ("mediaFiles", [item(mediaFiles=[1])], src)):
        try:
            runner.check_provider_history(history, sources, set(), account=tl.ACCT, fan=tl.FAN)
        except Refused as exc:
            check(exc.reason == "source-not-plain-text", (label, exc.reason))
        else:
            raise AssertionError("accepted " + label)
    try:
        runner.check_provider_history([item(price=3)], src, set(), account=tl.ACCT, fan=tl.FAN)
    except Refused as exc:
        check(exc.reason == "source-not-free-text", "priced frame refused")
    else:
        raise AssertionError("accepted priced")
    try:
        runner.check_provider_history([item(text="hi")], src, set(), account=tl.ACCT, fan=tl.FAN)
    except Refused as exc:
        check(exc.reason == "source-text-changed", "text appearing on a photo source refused")
    else:
        raise AssertionError("accepted changed text")


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="native-vision-") as tmp:
        env = {"DATABASE_URL": "sqlite+aiosqlite:///" + (Path(tmp) / "fixture.db").as_posix(), "PYTHON_DOTENV_DISABLED": "1"}
        with patch.dict(os.environ, env, clear=True):
            ledger_and_history()

            def deny(*a, **kw):
                raise AssertionError("network forbidden")

            async def run():
                with patch.object(socket.socket, "connect", deny), patch.object(socket.socket, "connect_ex", deny), \
                        patch.object(socket, "create_connection", deny):
                    await exercise(tmp)
            asyncio.run(run())
    print(f"{COUNT} checks; all cases passed")


if __name__ == "__main__":
    main()
