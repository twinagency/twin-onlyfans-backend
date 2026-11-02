"""Automatic description coverage through the REAL bounded child on disposable PostgreSQL.
Only the frame collector and the model call are synthetic: no provider, no network,
no real credential."""
import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401  (fixture)
import test_automatic_vault_descriptions_pg as base
from test_automatic_vault_descriptions_pg import ACCOUNT, MID, TENANT, BINDING, child
from db.models import Account, VaultItem
from db.onlyfans_vault_models import OfVaultObservation
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.vault_management import digest

IMG = "data:image/jpeg;base64,AAAA"
PHOTO_JSON = json.dumps({"description": "A woman in a red dress on a balcony.", "tags": ["balcony"], "explicitness": "sfw"})


class Rig:
    """setup() from the base module plus scripted frames/model and extra items."""

    def __init__(self, database, monkeypatch, limit=4):
        self.database, self.mp = database, monkeypatch
        self.args, _calls, self.engine, self.client = base.setup(database, monkeypatch)
        self.args.limit = limit
        self.collects, self.prompts = [], []
        self.frames = {}                       # media_id -> Collected
        self.chat_error = None
        self.chat_text = PHOTO_JSON
        import vault_frames, llm_client
        self.vf = vault_frames

        async def collect(account, item, raw, want):
            self.collects.append((account, item.media_id))
            return self.frames.get(item.media_id) or vault_frames.Collected([IMG], "ok", source="photo")

        async def chat(**kw):
            assert kw["account_id"] == ACCOUNT and kw["purpose"] == "describe_media"
            self.prompts.append(kw["messages"][0]["content"][0]["text"])
            if self.chat_error:
                raise self.chat_error
            from types import SimpleNamespace
            return SimpleNamespace(content=self.chat_text, call_id=None, cost_cents=3)
        monkeypatch.setattr(vault_frames, "collect", collect)
        monkeypatch.setattr(llm_client, "chat", chat)

    def add(self, media_id, kind="photo", account=ACCOUNT, observe=True, **fields):
        now = utcnow()
        raw = canonical({"id": media_id, "type": kind, "userId": 900001})
        with Session(self.database[1]) as s, s.begin():
            s.add(VaultItem(account_id=account, media_id=media_id, kind=kind, created_at=now, raw_json=raw, **fields))
            s.flush()
            if observe:
                s.add(OfVaultObservation(account_id=account, media_id=media_id, job_id="caption-sync",
                                         raw_hash=digest(raw), observed_at=now))

    def arun(self, coro):
        """One event loop per call; pooled asyncpg connections must not outlive it."""
        async def go():
            try:
                return await coro
            finally:
                await self.engine.dispose()
        return asyncio.run(go())

    def run(self):
        return self.arun(child.run(self.args))

    def row(self, media_id=MID):
        with Session(self.database[1]) as s:
            v = s.get(VaultItem, (ACCOUNT, media_id))
            s.expunge(v)
            return v

    def update(self, media_id=MID, **fields):
        with Session(self.database[1]) as s, s.begin():
            v = s.get(VaultItem, (ACCOUNT, media_id))
            for k, val in fields.items():
                setattr(v, k, val)

    def done(self):
        pass


@pytest.fixture
def rig(database, monkeypatch):
    r = Rig(database, monkeypatch)
    yield r
    r.done()


def test_photo_and_video_success_records_provenance_and_sampling_limits(rig):
    rig.update(content_hash="p1", content_hash_kind="sha")
    rig.add(11002, "video", duration_seconds=74, content_hash="v1", content_hash_kind="sha")
    rig.frames[11002] = rig.vf.Collected([IMG] * 9, "ok", source="storyboard")
    rig.chat_text = PHOTO_JSON
    out = rig.run()
    assert out["described"] == 2 and out["failed"] == 0 and out["costMillicents"] == 6
    photo, video = rig.row(MID), rig.row(11002)
    assert photo.describe_status == video.describe_status == "described"
    assert photo.description and video.video_description and photo.describe_model
    pf, vf = json.loads(photo.ai_fields_json), json.loads(video.ai_fields_json)
    assert pf["_frame_source"] == "photo" and pf["_content_stamp"] == "sha:p1"
    assert vf["_frame_source"] == "storyboard" and vf["_frames"] == 9 and video.frames_sampled == 9
    assert "9 frames sampled in order" in vf["_sampling_note"] and "audio" in vf["_sampling_note"]
    # the video prompt carries the limit; the photo prompt is unchanged
    assert sum("SAMPLING LIMIT" in p for p in rig.prompts) == 1


def test_poster_stills_are_not_presented_as_analysis_of_a_video(rig):
    rig.add(11003, "video", duration_seconds=60)
    rig.update(describe_status="described", description="done")             # photo already fine
    rig.frames[11003] = rig.vf.Collected([IMG], "ok", source="poster")        # a single thumbnail
    out = rig.run()
    video = rig.row(11003)
    assert out["described"] == 0 and out["failures"] == {"unsupported": 1}
    assert video.describe_status == "poster_only" and not video.video_description and not video.description
    assert rig.prompts == []                                                 # no model spend on a thumbnail
    assert json.loads(video.ai_fields_json)["_describe_failure"]["category"] == "unsupported"
    from moxy_replacement.media_descriptions import item_state
    assert item_state(video) == "unsupported"                                 # explicitly unresolved


def test_fetch_failure_recovers_after_bounded_backoff_and_clears_failure(rig):
    rig.frames[MID] = rig.vf.Collected([], "fetch_failed", "network")
    out = rig.run()
    assert out["failed"] == 1 and out["failures"] == {"fetching": 1}
    row = rig.row()
    assert row.describe_status == "fetch_failed" and not row.description
    assert json.loads(row.ai_fields_json)["_describe_failure"]["attempts"] == 1
    assert row.describe_generated_at >= utcnow() - timedelta(seconds=5)
    n = len(rig.collects)
    assert rig.run()["described"] == 0 and len(rig.collects) == n           # cooling off: nothing retried yet
    del rig.frames[MID]
    rig.update(describe_generated_at=utcnow() - timedelta(hours=1))         # backoff elapsed
    assert rig.run()["described"] == 1
    row = rig.row()
    assert row.describe_status == "described" and "_describe_failure" not in json.loads(row.ai_fields_json)


def test_retries_are_bounded_then_item_stays_explicitly_unresolved(rig):
    import vault_describe_state as vds
    rig.frames[MID] = rig.vf.Collected([], "fetch_failed", "network")
    for _ in range(vds.MAX_ATTEMPTS + 3):
        rig.run()
        rig.update(describe_generated_at=utcnow() - timedelta(days=30))     # skip the waiting only
    assert len(rig.collects) == vds.MAX_ATTEMPTS
    row = rig.row()
    f = json.loads(row.ai_fields_json)["_describe_failure"]
    assert f["exhausted"] is True and not row.description and row.describe_status == "fetch_failed"
    # new content gives it a fresh budget of attempts
    rig.update(content_hash="new", content_hash_kind="sha")
    del rig.frames[MID]
    assert rig.run()["described"] == 1


@pytest.mark.parametrize("error,category,item_status,state,reason", [
    ("config", "auth", None, "retry_pending", "auth"),
    ("http401", "auth", None, "retry_pending", "auth"),
    ("http402", "budget", None, "budget_paused", "budget"),
    ("http500", "model", "failed", "complete", None),
])
def test_failure_categories_are_separate_and_account_level_ones_do_not_mark_items(rig, error, category, item_status, state, reason):
    from llm_client import LLMConfigError, LLMHTTPError
    rig.chat_error = {"config": LLMConfigError("no key"), "http401": LLMHTTPError("x", status=401),
                      "http402": LLMHTTPError("x", status=402), "http500": LLMHTTPError("x", status=500)}[error]
    out = rig.run()
    assert out["failures"] == {category: 1} and out["state"] == state and out.get("reason") == reason
    row = rig.row()
    assert row.describe_status == item_status and not row.description
    if item_status is None:
        assert "_describe_failure" not in json.loads(row.ai_fields_json or "{}")   # still pending, not "failed"
    else:
        assert json.loads(row.ai_fields_json)["_describe_failure"]["category"] == category


def test_refusal_is_recorded_as_model_failure_and_scoped_path_never_escalates(rig):
    import vault_ai_api as api
    rig.chat_text = "I cannot help with that."
    res = rig.arun(api._describe_one(ACCOUNT, MID, model="qwen3-vl-30b", escalate=False))
    assert res["status"] == "refused" and res["category"] == "model"
    assert len(rig.prompts) == 1                      # one model, no second provider/model
    row = rig.row()
    assert row.describe_status == "refused" and not row.description
    assert json.loads(row.ai_fields_json)["_describe_failure"]["category"] == "model"
    rig.update(describe_status=None)
    rig.arun(api._describe_one(ACCOUNT, MID, model="qwen3-vl-30b"))   # legacy global path may escalate once
    assert len(rig.prompts) == 3


def test_unchanged_asset_is_never_analysed_again_but_changed_content_is(rig):
    rig.update(content_hash="h1", content_hash_kind="sha")
    assert rig.run()["described"] == 1
    n = len(rig.prompts)
    # expiring URLs, tags, prices and timestamps are not content
    rig.update(thumb_url="https://cdn.example/new?sig=2", full_url="https://cdn.example/new2?sig=2",
               tags='["a"]', suggested_price_cents=900, updated_at=utcnow(), notes="n")
    out = rig.run()
    assert out["described"] == 0 and len(rig.prompts) == n and len(rig.collects) == 1
    rig.chat_text = json.dumps({"description": "Now a blue dress.", "tags": [], "explicitness": "sfw"})
    rig.update(content_hash="h2")
    assert rig.run()["described"] == 1
    row = rig.row()
    assert row.description == "Now a blue dress." and json.loads(row.ai_fields_json)["_content_stamp"] == "sha:h2"


def test_operator_corrections_are_never_overwritten_by_reanalysis(rig):
    rig.update(description="Operator wording", describe_status="described", content_hash="h2", content_hash_kind="sha",
               ai_fields_json=json.dumps({"_content_stamp": "sha:h1"}),
               operator_overrides_json='{"description": "Operator wording"}', locked_fields_json='["description"]')
    out = rig.run()
    assert out["described"] == 0 and rig.prompts == [] and rig.collects == []
    assert rig.row().description == "Operator wording"


def test_videos_are_not_starved_by_a_photo_backlog(rig):
    for i in range(12):
        rig.add(12000 + i, "photo")
    rig.add(13000, "video", duration_seconds=30)
    rig.frames[13000] = rig.vf.Collected([IMG] * 9, "ok", source="storyboard")
    with Session(rig.database[1]) as s:
        ids = child.select_batch(s, ACCOUNT, 4)
        assert 13000 in ids and len(ids) == 4
        assert child.select_batch(s, ACCOUNT, 1)[0] != 13000      # limit 1 stays photo-first (documented)
    out = rig.run()
    assert out["described"] == 4 and rig.row(13000).describe_status == "described"


def test_video_only_and_photo_only_backlogs_fill_the_batch(rig):
    rig.update(describe_status="described", description="done")
    rig.add(13001, "video"); rig.add(13002, "video")
    with Session(rig.database[1]) as s:
        assert child.select_batch(s, ACCOUNT, 4) == [13001]       # one clip per batch
    rig.update(13001, describe_status="described", video_description="x")
    rig.update(13002, describe_status="described", video_description="x")
    for i in range(6):
        rig.add(14000 + i, "photo")
    with Session(rig.database[1]) as s:
        assert len(child.select_batch(s, ACCOUNT, 4)) == 4


def test_manual_and_automatic_jobs_do_not_duplicate_one_asset(rig):
    import vault_ai_api as api
    lease = api._ItemLease(ACCOUNT, MID)
    with rig.database[1].connect() as other:
        other.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lease.key})   # another job holds it
        res = rig.arun(api._describe_one(ACCOUNT, MID))
        other.rollback()
    assert res["status"] == "in_progress" and rig.collects == [] and rig.prompts == []
    # once free, the same call proceeds; a second automatic-style call skips the unchanged asset
    assert rig.arun(api._describe_one(ACCOUNT, MID, skip_unchanged=True))["ok"]
    again = rig.arun(api._describe_one(ACCOUNT, MID, skip_unchanged=True))
    assert again["status"] == "unchanged" and len(rig.prompts) == 1


def test_other_account_media_is_never_selected_or_described(rig):
    other = "of-900002"
    with Session(rig.database[1]) as s, s.begin():
        s.add(Account(id=other))
    rig.add(15001, "photo", account=other, observe=False)
    out = rig.run()
    assert out["described"] == 1
    assert rig.collects == [(ACCOUNT, MID)]
    with Session(rig.database[1]) as s:
        v = s.get(VaultItem, (other, 15001))
        assert v.describe_status is None and not v.description
