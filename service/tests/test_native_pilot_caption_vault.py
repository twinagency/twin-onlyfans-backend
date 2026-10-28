"""caption_vault CLI: runs FASTT's own _describe_one + its writer; no network. Synthetic data only."""
import asyncio, io, json, os, sys, tempfile, contextlib
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TMP = tempfile.mkdtemp()
os.environ.pop("ONLYFANS_PROCESS_ROLE", None)
os.environ["TWIN_FASTT_CONFIG_ISOLATED"] = "1"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP}/t.db"

import automation_executor as ax
import llm_client, vault_frames, vault_ai_api as api
from db.engine import engine, get_session
from db.models import Account, VaultItem, VaultFolder, VaultFolderItem
from sqlalchemy import select, delete
from native_pilot.tools import caption_vault as cv

ACCOUNT = "of-900001"
COUNT = 0


def check(cond, label):
    global COUNT
    assert cond, label
    COUNT += 1


NOW = datetime(2026, 1, 1)


async def seed():
    async with engine.begin() as c:
        await c.run_sync(lambda sc: [t.create(sc) for t in (Account.__table__, VaultItem.__table__,
                                                          VaultFolder.__table__, VaultFolderItem.__table__)])
    async with get_session() as s:
        s.add(Account(id=ACCOUNT)); s.add(Account(id="of-900009"))
        rows = [  # (account, media, kind, age days, status, removed)
            (ACCOUNT, 1, "photo", 1, None, False), (ACCOUNT, 2, "video", 2, None, False),
            (ACCOUNT, 3, "photo", 3, "described", False), (ACCOUNT, 4, "photo", 4, None, True),
            (ACCOUNT, 5, "audio", 5, None, False), (ACCOUNT, 6, "photo", 6, None, False),
            (ACCOUNT, 7, "photo", 7, "refused", False), ("of-900009", 8, "photo", 0, None, False)]
        for a, m, k, d, st, rm in rows:
            s.add(VaultItem(account_id=a, media_id=m, kind=k, created_at=NOW - timedelta(days=d),
                            describe_status=st, raw_json="{}", removed_at=NOW if rm else None))
        await s.commit()


calls = {"collect": [], "chat": []}


async def fake_collect(account_id, item, raw, want=3):
    calls["collect"].append(item.media_id)
    return vault_frames.Collected(["data:image/jpeg;base64,AAAA"], "ok")


async def fake_chat(**kw):
    calls["chat"].append((kw["model"], kw["purpose"], kw["account_id"]))
    body = json.dumps({"description": "a synthetic caption", "tags": ["Test"], "explicitness": "sfw",
                       "nsfw": False, "sellable": True})
    return SimpleNamespace(content=body, call_id=77, cost_cents=12)


def args(**kw):
    base = dict(account=ACCOUNT, limit=50, model=cv.DEFAULT_MODEL, dry_run=False, fresh_only=False, with_flags=False)
    base.update(kw)
    return SimpleNamespace(**base)


async def main():
    await seed()
    out = io.StringIO()
    # dry run: no connection, no model, nothing written
    with contextlib.redirect_stdout(out):
        r = await cv.run(args(dry_run=True))
    check(r["would_caption"] == 4 and r["dry_run"], r)
    ids = [json.loads(l)["media_id"] for l in out.getvalue().splitlines()]
    check(ids == [1, 2, 6, 7], ids)
    check(calls["chat"] == [] and calls["collect"] == [], "dry run made no calls")

    # limit + newest first, fresh-only excludes the refused row
    check(await cv.candidates(api, ACCOUNT, 2) == [1, 2], "limit, newest first")
    check(await cv.candidates(api, ACCOUNT, 50) == [1, 2, 6, 7], "removed/described/audio/other account excluded")
    check(await cv.candidates(api, ACCOUNT, 50, fresh_only=True) == [1, 2, 6], "fresh-only")

    # real run through FASTT's _describe_one with only frames + model faked
    vault_frames.collect = fake_collect
    llm_client.chat = fake_chat
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        summary = await cv.caption(api, ACCOUNT, await cv.candidates(api, ACCOUNT, 2), model=cv.DEFAULT_MODEL)
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    check([l["media_id"] for l in lines] == [1, 2] and all(l["ok"] and l["chars"] == 19 for l in lines), lines)
    check(lines[0]["cost_millicents"] == 12 and summary["described"] == 2 and summary["cost_millicents"] == 24, summary)
    check(calls["collect"] == [1, 2] and [c[0] for c in calls["chat"]] == [cv.DEFAULT_MODEL] * 2, calls)
    check(calls["chat"][0][1:] == ("describe_media", ACCOUNT), "FASTT purpose + account passed")
    async with get_session() as s:
        photo = await s.get(VaultItem, (ACCOUNT, 1))
        video = await s.get(VaultItem, (ACCOUNT, 2))
    check(photo.describe_status == "described" and photo.description == "a synthetic caption", "photo persisted")
    check(video.video_description == "a synthetic caption" and video.description is None, "video column")
    check(json.loads(photo.tags) == ["test"] and "synthetic" in photo.search_text, "tags + search_text")
    check(json.loads(photo.ai_fields_json)["_prompt_version"] == "v2" and photo.describe_model == cv.DEFAULT_MODEL
          and photo.describe_call_id == 77, "ai_fields_json / model / call id")
    check(json.loads(photo.ai_fields_json)["_auto_folders_v1"] is True, "description hook categorised photo")
    async with get_session() as s:
        folders = list(await s.scalars(select(VaultFolder)))
        members = list(await s.scalars(select(VaultFolderItem)))
        check(len(folders) == 1 and folders[0].name == "Uncategorised", "existing folder owner reused")
        check(sorted(m.media_id for m in members) == [1, 2], "photo and video filed without duplicate folders")
        await s.execute(delete(VaultFolderItem).where(VaultFolderItem.account_id == ACCOUNT,
                                                     VaultFolderItem.media_id == 1))
        await s.commit()
    await api._describe_one(ACCOUNT, 1, model=cv.DEFAULT_MODEL)
    async with get_session() as s:
        check(await s.get(VaultFolderItem, (ACCOUNT, folders[0].id, 1)) is None,
              "reanalysis preserves manual category removal")

    # idempotent: described items are skipped on the next run
    check(await cv.candidates(api, ACCOUNT, 50) == [6, 7], "described items skipped")
    calls["chat"].clear(); calls["collect"].clear()
    with contextlib.redirect_stdout(io.StringIO()):
        await cv.caption(api, ACCOUNT, await cv.candidates(api, ACCOUNT, 1), model=cv.DEFAULT_MODEL)
    check(calls["collect"] == [6], "limit honoured on second run")

    # a capped item stops the run
    async def capped(**kw):
        raise llm_client.LLMCapExceeded("openrouter", 1, 2)
    llm_client.chat = capped
    async def cap_state(account_id, provider):
        return {"capped": True, "spent_millicents": 5, "cap_millicents": 5}
    llm_client.cap_state = cap_state
    cap_out = io.StringIO()
    with contextlib.redirect_stdout(cap_out):
        s2 = await cv.caption(api, ACCOUNT, [7, 6], model=cv.DEFAULT_MODEL)
    check(s2["stopped"] == "capped" and s2["attempted"] == 1, s2)

    # adapters
    sentinel = object()
    cv.install_client_adapter(sentinel, ACCOUNT)
    check(ax._make_client(ACCOUNT) is sentinel, "client seam returns verified client")
    try:
        ax._make_client("of-900009")
    except RuntimeError:
        check(True, "other account refused")
    else:
        raise AssertionError("other account accepted")
    os.environ["ONLYFANS_PROCESS_ROLE"] = "api"
    try:
        cv.install_llm_adapter()
    except RuntimeError:
        check(os.environ.get("ONLYFANS_PROCESS_ROLE") == "api", "api role refused, untouched")
    else:
        raise AssertionError("api role accepted")
    os.environ["ONLYFANS_PROCESS_ROLE"] = "worker"
    cv.install_llm_adapter()
    check("ONLYFANS_PROCESS_ROLE" not in os.environ, "worker role dropped for the legacy caller")
    os.environ.pop("OPENROUTER_API_KEY", None)
    prov = SimpleNamespace(name="openrouter", api_key_env="OPENROUTER_API_KEY")
    try:
        await llm_client._tenant_api_key(ACCOUNT, prov, "describe_media")
    except llm_client.LLMConfigError:
        check(True, "no env key refuses")
    else:
        raise AssertionError("keyless call allowed")
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    check(await llm_client._tenant_api_key(ACCOUNT, prov, "describe_media") == "test-key", "env key used")

    # Automatic imports revalidate their frozen account generation before reads
    # and every write, including failure stamps. A stale result cannot land.
    async with get_session() as session:
        session.add(VaultItem(account_id=ACCOUNT, media_id=10, kind="photo", created_at=NOW, raw_json="{}"))
        await session.commit()
    llm_client.chat = fake_chat
    vault_frames.collect = fake_collect
    for fail_on in (1, 2):
        guards = []
        async def changed(session, media_id, **kwargs):
            guards.append(media_id)
            if len(guards) == fail_on:
                raise RuntimeError("synthetic-generation-changed")
        calls["collect"].clear(); calls["chat"].clear()
        try:
            await api._describe_one(ACCOUNT, 10, model=cv.DEFAULT_MODEL, scope_guard=changed)
        except RuntimeError as exc:
            check(str(exc) == "synthetic-generation-changed", "scope refusal preserved")
        else:
            raise AssertionError("stale generation persisted")
        async with get_session() as session:
            row = await session.get(VaultItem, (ACCOUNT, 10))
            check(row.description is None and row.describe_status is None, "stale caption/failure not written")
        if fail_on == 1:
            check(calls["collect"] == calls["chat"] == [], "revoked scope does no media/model work")
    guards = []
    async def current(session, media_id, **kwargs): guards.append(media_id)
    result = await api._describe_one(ACCOUNT, 10, model=cv.DEFAULT_MODEL, scope_guard=current)
    check(result["ok"] and len(guards) == 3, "current generation writes caption and categories")


asyncio.run(main())
print(f"ok {COUNT} checks")

