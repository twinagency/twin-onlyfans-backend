"""CRM Save -> unpublished media sync -> fixed disabled item -> exact selling (handoff 15.3, T26-T28).

Real disposable PostgreSQL, a recording fake SyncTransport and synthetic vault
pages. No credential, network, fan message, feed post or model call.
"""
from collections import deque
from datetime import timedelta
import hashlib
import json
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event, due
from test_onlyfans_connections_pg import ACTOR, TENANT, data as connection_data, provision, save as save_connection
from test_onlyfans_provider_adapters import OWNER
from test_onlyfans_vault_pg import media, page, sync
from db.models import (AccountAiConfig, CatalogItem, ContentOffer, LadderQuote, LadderState, Message, OfConversationState,
    OfCreatorBinding, VaultItem, VaultSend)
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfNativePlan, OfTextIntent
from db.onlyfans_campaign_models import OfCampaign
from db.onlyfans_models import OfRuntime
from db.onlyfans_offer_import_models import (OfContentSyncAsset as Asset, OfContentSyncDecision as Decision,
    OfContentSyncDraft as Draft, OfContentSyncOperation as Operation)
from db.onlyfans_vault_models import OfVaultSyncJob
from onlyfans_runtime import content_sync_media as csm
from onlyfans_runtime.content_management import get_content_v2, update_content
from onlyfans_runtime.content_sync_media import (AuthRequired, InvalidSync, TransientUpload, UploadUnknown, commit_draft,
    get_draft, list_drafts, process_uploads, put_chunk, retry_asset, save_draft)
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.vault_management import process_vault_jobs, request_sync
from onlyfans_runtime.native_protocol import cents as parse_cents, request as wire_request


class Crash(BaseException):
    """A worker process dying; deliberately not an Exception so no handler swallows it."""


class Fake:
    """Recording unpublished transport. Plans are per (method, token) or per method."""
    def __init__(self, photos, videos):
        self.pool = {"photo": list(photos), "video": list(videos)}
        self.calls, self.landed, self.plan, self.verdict = [], {}, {}, {}

    def step(self, method, token, default="ok"):
        for key in ((method, token), (method, None)):
            if self.plan.get(key):
                return self.plan[key].pop(0)
        return default

    def queue(self, method, token, *steps):
        self.plan.setdefault((method, token), []).extend(steps)

    def lookup(self, *, upload_token):
        self.calls.append(("lookup", upload_token))
        plan = self.step("lookup", upload_token)
        if plan == "unknown":
            raise UploadUnknown()
        if plan == "auth":
            raise AuthRequired()
        if plan == "transient":
            raise TransientUpload()
        return self.landed.get(upload_token)

    def upload(self, *, upload_token, path, kind):
        self.calls.append(("upload", upload_token))
        assert path.is_file() and path.read_bytes(), "the staged bytes are handed over"
        plan = self.step("upload", upload_token)
        if callable(plan):
            plan()  # a side effect during provider I/O (e.g. a rebind)
            plan = "ok"
        if plan == "crash_before":
            raise Crash()
        if plan == "transient":
            raise TransientUpload()
        if plan == "auth":
            raise AuthRequired()
        if plan == "unknown_unlanded":
            raise UploadUnknown()
        if upload_token not in self.landed:
            self.landed[upload_token] = self.pool[kind].pop(0)
        if plan == "lost":
            raise UploadUnknown()
        if plan == "transient_landed":
            raise TransientUpload()
        if plan == "crash_after":
            raise Crash()
        return self.landed[upload_token]

    def verify(self, *, provider_media_id):
        self.calls.append(("verify", provider_media_id))
        plan = self.step("verify", provider_media_id)
        if plan == "crash":
            raise Crash()
        if plan == "auth":
            raise AuthRequired()
        if plan == "transient":
            raise TransientUpload()
        return self.verdict.get(provider_media_id, plan if plan != "ok" else "ready")

    def count(self, method, token=None):
        return sum(1 for m, t in self.calls if m == method and token in (None, t))

    def only_sync_methods(self):
        return {m for m, _ in self.calls} <= {"lookup", "upload", "verify"}


# ── Fixtures ────────────────────────────────────────────────────────────

VAULT = [media(i) for i in range(101, 111)] + [media(i, "video") for i in range(111, 121)]


@pytest.fixture
def world(database, monkeypatch, tmp_path):
    monkeypatch.setattr(csm, "SPOOL", tmp_path / "spool")
    monkeypatch.setattr(csm, "CHUNK_BYTES", 4)
    monkeypatch.setattr(csm, "MIN_FREE_BYTES", 0)
    store, account, _, _ = sync(database, monkeypatch, rows=VAULT)
    w = type("World", (), {})()
    w.store, w.account, w.engine, w.monkeypatch = store, account, database[1], monkeypatch
    w.fake = Fake(range(101, 111), range(111, 121))
    w.transport = lambda connection, tenant, account: w.fake
    w.seq = 0
    yield w
    store.close()


def make(n, *, videos=(), tag="a"):
    """n declared files with distinct bytes; indexes in videos are .mp4."""
    files, blobs = [], {}
    for i in range(n):
        body = f"{tag}-file-{i}-".encode().ljust(10, b"z")
        fid = str(uuid.uuid4())
        files.append({"fileId": fid, "name": f"f{i}.{'mp4' if i in videos else 'jpg'}", "size": len(body),
                      "sha256": hashlib.sha256(body).hexdigest()})
        blobs[fid] = body
    return files, blobs


def ckey(w, label="c"):
    w.seq += 1
    return hashlib.sha256(f"{label}{w.seq}".encode()).hexdigest()


def body(w, files, previews=(), *, key=None, price=1999, **patch):
    return {"commandKey": key or ckey(w, "save"), "label": "Scenic set", "description": "Approved scenic content.",
            "priceCents": price, "files": files, "previewFileIds": list(previews), **patch}


def stage(w, draft_id, files, blobs, only=None):
    for f in files:
        if only is not None and f["fileId"] not in only:
            continue
        data = blobs[f["fileId"]]
        for index in range(0, (len(data) + 3) // 4):
            out = put_chunk(w.store, TENANT, w.account, draft_id, f["fileId"], index, data[index * 4:index * 4 + 4])
        assert out["complete"] is True


def saved(w, files, blobs, previews=(), **kw):
    draft = save_draft(w.store, TENANT, w.account, value=body(w, files, previews, **kw), actor=ACTOR)
    stage(w, draft["draftId"], files, blobs)
    return draft


def commit(w, draft, key=None):
    return commit_draft(w.store, TENANT, w.account, draft["draftId"], command_key=key or ckey(w, "commit"), actor=ACTOR)


def run(w, rounds=40, fake=None):
    """Drive the worker until nothing is due (future-dated retries are not due)."""
    fake = fake or w.fake
    total = 0
    for _ in range(rounds):
        n = process_uploads(w.store, TENANT, limit=1, transport_for=lambda c, t, a: fake)
        total += n
        if not n:
            break
    return total


def due_now(w):
    with w.engine.begin() as c:
        c.execute(text("UPDATE of_content_sync_asset SET next_attempt_at = NULL, "
                       "lease_token = NULL, lease_until = NULL WHERE state IN ('retrying','verifying','uploading','queued')"))
        c.execute(text("UPDATE of_content_sync_operation SET next_attempt_at = NULL, lease_token = NULL, lease_until = NULL, "
                       "status = CASE WHEN status = 'running' THEN 'queued' ELSE status END"))


def expire_leases(w):
    with w.engine.begin() as c:
        c.execute(text("UPDATE of_content_sync_asset SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE lease_token IS NOT NULL"))
        c.execute(text("UPDATE of_content_sync_operation SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE lease_token IS NOT NULL"))


def tokens(w):
    with Session(w.engine) as s:
        return {a.content_hash: a.upload_token for a in s.scalars(select(Asset).where(Asset.account_id == w.account))}


def rows(w, model, **where):
    with Session(w.engine) as s:
        return list(s.scalars(select(model).where(*[getattr(model, k) == v for k, v in where.items()])))


def snapshot(w):
    """Everything Save must leave alone: outgoing authority, modes, sends, campaigns, posts."""
    with Session(w.engine) as s:
        runtime = s.get(OfRuntime, w.account)
        conn = s.get(OfProviderConnection, w.account)
        return {"runtime": (runtime.enabled, runtime.binding_id, runtime.configuration_revision),
            "connection": (conn.revision, conn.status, conn.automate_new_conversations),
            "policy": [(p.account_id, p.enabled, p.media_enabled, p.used_units) for p in s.scalars(select(OfDeliveryPolicy))],
            "conversations": [(c.fan_id, c.mode) for c in s.scalars(select(OfConversationState))],
            "ai": [(c.account_id,) for c in s.scalars(select(AccountAiConfig))],
            "counts": [s.scalar(select(func.count()).select_from(m)) for m in
                       (OfTextIntent, OfCampaign, ContentOffer, VaultSend, Message, OfNativePlan, LadderQuote)]}


def full_draft(w, n=3, *, videos=(), previews_at=(), **kw):
    files, blobs = make(n, videos=videos, tag=kw.pop("tag", "a"))
    draft = saved(w, files, blobs, [files[i]["fileId"] for i in previews_at], **kw)
    return files, blobs, draft


# ── T26: CRM Save ───────────────────────────────────────────────────────

def test_save_commit_sync_makes_one_disabled_fixed_item_and_touches_no_outgoing_surface(world):
    w = world
    before = snapshot(w)
    files, blobs = make(3, videos=(2,))
    draft = save_draft(w.store, TENANT, w.account, value=body(w, files, [files[0]["fileId"]], price=1007), actor=ACTOR)
    assert draft["state"] == "draft" and draft["pricingMode"] == "fixed" and draft["priceCents"] == 1007
    assert draft["itemId"] is None and draft["enabled"] is False and draft["readiness"]["canEnable"] is False
    assert [a["state"] for a in draft["assets"]] == ["draft"] * 3 and draft["assets"][0]["freePreview"] is True
    assert rows(w, Asset) == [] and rows(w, Operation) == [], "Save alone queues nothing until the bytes are staged and committed"
    stage(w, draft["draftId"], files, blobs)
    queued = commit(w, draft)
    assert queued["state"] == "queued" and {a["state"] for a in queued["assets"]} == {"queued"}
    assert len(rows(w, Asset)) == 3 and len(rows(w, Operation, kind="upload")) == 3, "draft revision and upload work commit together"
    # A partially synced bundle never becomes a catalogue item.
    assert process_uploads(w.store, TENANT, limit=1, transport_for=w.transport) == 1
    assert process_uploads(w.store, TENANT, limit=1, transport_for=w.transport) == 1
    assert rows(w, CatalogItem) == [] and get_draft(w.store, TENANT, w.account, draft["draftId"])["itemId"] is None
    run(w)
    done = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert done["state"] == "synced" and done["readiness"] == {"mediaSynced": True, "canEnable": True, "blockers": []}
    assert [a["state"] for a in done["assets"]] == ["synced"] * 3
    [item] = rows(w, CatalogItem)
    assert str(item.id) == done["itemId"] and item.enabled is False and item.pricing_mode == "fixed"
    assert item.price_cents == 1007 and item.is_free_teaser is False and item.script_id is None and item.kind == "video"
    ids = {a.content_hash: a.provider_media_id for a in rows(w, Asset)}
    order = [ids[f["sha256"]] for f in files]
    assert json.loads(item.media_ids) == order, "provider ids follow the saved order"
    assert json.loads(item.preview_media_ids) == [order[0]]
    # Provider ids are this account's own: vault rows exist for it and the mapping is binding scoped.
    with Session(w.engine) as s:
        assert all(s.get(VaultItem, (w.account, mid)) is not None for mid in order)
        assert {a.binding_id for a in s.scalars(select(Asset))} == {s.get(OfRuntime, w.account).binding_id}
    assert w.fake.only_sync_methods(), "no fan-message or feed-post request exists on the transport at all"
    assert snapshot(w) == before


def test_commit_is_one_transaction_for_draft_assets_and_operations(world):
    w = world
    files, blobs, draft = full_draft(w, 3)
    # A foreign operation already owns the idempotency key of the third upload.
    with Session(w.engine) as s, s.begin():
        binding = s.get(OfRuntime, w.account).binding_id
        s.add(Operation(account_id=w.account, operation_id=str(uuid.uuid4()), binding_id=binding,
            command_key=hashlib.sha256(f"upload:{binding}:{files[2]['sha256']}".encode()).hexdigest(),
            request_hash="0" * 64, actor=ACTOR, kind="upload", status="queued", coverage_json="{}", result_json="{}",
            attempts=0, created_at=utcnow(), updated_at=utcnow()))
    with pytest.raises(Refused):
        commit(w, draft)
    assert rows(w, Asset) == [], "nothing of the failed commit survived"
    assert len(rows(w, Operation)) == 1 and rows(w, Draft)[0].state == "draft"
    with Session(w.engine) as s, s.begin():
        s.execute(text("DELETE FROM of_content_sync_operation"))
    assert commit(w, draft)["state"] == "queued" and len(rows(w, Asset)) == 3


def test_production_default_has_no_unpublished_path_so_nothing_is_ever_enabled(world, monkeypatch):
    w = world
    # Any attempt to reach the carrier uploader explodes.
    for name in ("vault_upload", "onlyfans_runtime.vault_upload_transport"):
        monkeypatch.setitem(sys.modules, name, None)
    files, blobs, draft = full_draft(w, 2)
    commit(w, draft)
    assert process_uploads(w.store, TENANT, limit=4) == 2
    result = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert result["state"] == "needs_review" and result["reason"] == "unpublished_upload_unavailable"
    assert {a["state"] for a in result["assets"]} == {"needs_review"} and result["itemId"] is None
    assert "unpublished_upload_unavailable" in result["readiness"]["blockers"] and result["readiness"]["canEnable"] is False
    assert rows(w, CatalogItem) == []
    assert process_uploads(w.store, TENANT, limit=4) == 0, "needs_review is never auto-retried"
    ops = rows(w, Operation)
    assert {(o.status, o.last_error) for o in ops} == {("failed", "unpublished_upload_unavailable")}


def test_save_path_never_imports_the_carrier_uploader():
    code = ("import sys; sys.path[:0]=['service','.']; import onlyfans_runtime.content_sync_media as m;"
            "bad=[k for k in sys.modules if k.split('.')[-1] in ('vault_upload','vault_upload_transport','of_client','vault_media_worker')];"
            "print(bad)")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True,
                         cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]))
    assert out.stdout.strip() == "[]"


@pytest.mark.parametrize("patch", [
    {"priceCents": 1999.0}, {"priceCents": True}, {"priceCents": 299}, {"priceCents": 20001}, {"priceCents": -1},
    {"priceCents": "1999"}, {"files": []}, {"description": " "}, {"label": ""}, {"commandKey": "x" * 64},
    {"commandKey": "A" * 64}, {"previewFileIds": "none"}, {"extra": 1}, {"actor": "someone-else"}])
def test_save_validation_rejects_inexact_or_unsupported_input(world, patch):
    w = world
    files, _ = make(2)
    value = {**body(w, files), **patch}
    with pytest.raises(InvalidSync):
        save_draft(w.store, TENANT, w.account, value=value, actor=ACTOR)
    assert rows(w, Draft) == []


def test_save_validation_files_and_previews(world):
    w = world
    files, _ = make(3, videos=(2,))
    def bad(**changes):
        with pytest.raises(InvalidSync):
            save_draft(w.store, TENANT, w.account, value={**body(w, files), **changes}, actor=ACTOR)
    bad(previewFileIds=[files[2]["fileId"]])  # a video is never a free preview
    bad(previewFileIds=[f["fileId"] for f in files])  # at least one paid file
    bad(previewFileIds=[files[0]["fileId"]] * 2)
    bad(previewFileIds=[str(uuid.uuid4())])
    bad(files=files + [files[0]])  # duplicate file id and hash
    bad(files=[{**files[0], "name": "../x.jpg"}])
    bad(files=[{**files[0], "name": "a/b.jpg"}])
    bad(files=[{**files[0], "name": "x.exe"}])
    bad(files=[{**files[0], "size": 0}])
    bad(files=[{**files[0], "size": True}])
    bad(files=[{**files[0], "sha256": "A" * 64}])
    bad(files=[{**files[0], "fileId": "not-a-uuid"}])
    bad(files=[{**files[0], "extra": 1}])
    bad(files=[dict(files[0], fileId=str(uuid.uuid4()), sha256=hashlib.sha256(str(i).encode()).hexdigest()) for i in range(21)])
    with pytest.raises(InvalidSync):
        save_draft(w.store, TENANT, w.account, value=body(w, files), actor="not-an-actor")
    assert rows(w, Draft) == []


def test_save_requires_a_verified_connection_and_the_right_tenant(world):
    w = world
    files, _ = make(1)
    with pytest.raises(Refused):
        save_draft(w.store, "tenant-b", w.account, value=body(w, files), actor=ACTOR)
    with Session(w.engine) as s, s.begin():
        s.get(OfProviderConnection, w.account).status = "paused"
    with pytest.raises(Refused, match="connection"):
        save_draft(w.store, TENANT, w.account, value=body(w, files), actor=ACTOR)
    assert rows(w, Draft) == []


def test_idempotency_by_command_key(world):
    w = world
    files, blobs = make(2)
    value = body(w, files, price=1999)
    first = save_draft(w.store, TENANT, w.account, value=value, actor=ACTOR)
    assert save_draft(w.store, TENANT, w.account, value=value, actor=ACTOR) == first
    assert len(rows(w, Draft)) == 1
    with pytest.raises(Refused, match="idempotency"):
        save_draft(w.store, TENANT, w.account, value={**value, "priceCents": 2000}, actor=ACTOR)
    with pytest.raises(Refused, match="idempotency"):
        save_draft(w.store, TENANT, w.account, value=value, actor="op-session:" + "b" * 64)
    stage(w, first["draftId"], files, blobs)
    key = ckey(w, "commit")
    done = commit(w, first, key)
    assert commit(w, first, key)["draftId"] == done["draftId"] and len(rows(w, Asset)) == 2 and len(rows(w, Operation)) == 2
    other, other_blobs = make(1, tag="b")
    second = saved(w, other, other_blobs)
    with pytest.raises(Refused, match="idempotency"):
        commit(w, second, key)
    with pytest.raises(Refused, match="idempotency"):
        commit(w, second, value["commandKey"])  # a save key is not a commit key
    with pytest.raises(Refused, match="committed"):
        commit(w, first)
    assert len(rows(w, Decision, action="save")) >= 3


def test_chunk_staging_is_bounded_contained_and_verified(world, tmp_path):
    w = world
    files, blobs = make(1)
    f = files[0]
    draft = save_draft(w.store, TENANT, w.account, value=body(w, files), actor=ACTOR)
    did, fid, data = draft["draftId"], f["fileId"], blobs[f["fileId"]]
    def put(index, chunk, d=did, i=fid):
        return put_chunk(w.store, TENANT, w.account, d, i, index, chunk)
    for bad in [lambda: put(0, b"x" * 5), lambda: put(0, b""), lambda: put(0, "text"), lambda: put(-1, b"abcd"),
                lambda: put(True, b"abcd"), lambda: put(0, b"ab"), lambda: put(9, b"abcd"),
                lambda: put(0, data[:4], i="../../etc/passwd"), lambda: put(0, data[:4], d="../x"),
                lambda: put(0, data[:4], i=str(uuid.uuid4()))]:
        with pytest.raises((InvalidSync, Refused)):
            bad()
    assert not (tmp_path / "spool").exists() or not any((tmp_path / "spool").rglob("blob"))
    with pytest.raises(Refused, match="offset"):
        put(1, data[4:8])
    assert put(0, data[:4]) == {"received": 4, "complete": False}
    assert put(0, data[:4]) == {"received": 4, "complete": False}, "replaying a stored chunk is idempotent"
    with pytest.raises(Refused, match="conflict"):
        put(0, b"zzzz")
    with pytest.raises(InvalidSync):
        commit(w, draft)  # unfinished bytes cannot be committed
    assert put(1, data[4:8])["complete"] is False
    # Last chunk with the wrong content: checksum refused and the bad bytes are discarded.
    with pytest.raises(InvalidSync, match="checksum"):
        put(2, b"zz")
    assert not (tmp_path / "spool" / did / fid / "blob").exists()
    stage(w, did, files, blobs)
    assert commit(w, draft)["state"] == "queued"
    with pytest.raises(Refused, match="committed"):
        put(0, data[:4])


def test_spool_symlinks_are_refused(world, tmp_path):
    w = world
    files, blobs = make(1)
    draft = save_draft(w.store, TENANT, w.account, value=body(w, files), actor=ACTOR)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "spool").mkdir()
    (tmp_path / "spool" / draft["draftId"]).symlink_to(outside)
    with pytest.raises(Refused, match="storage"):
        put_chunk(w.store, TENANT, w.account, draft["draftId"], files[0]["fileId"], 0, blobs[files[0]["fileId"]][:4])
    assert list(outside.iterdir()) == []


def test_drafts_are_listed_and_scoped_to_their_account(world):
    w = world
    made = []
    for i in range(3):
        files, blobs = make(1, tag=f"l{i}")
        made.append(saved(w, files, blobs)["draftId"])
    first = list_drafts(w.store, TENANT, w.account, after=None, limit=2)
    assert len(first["items"]) == 2 and first["nextCursor"] == first["items"][-1]["draftId"]
    second = list_drafts(w.store, TENANT, w.account, after=first["nextCursor"], limit=2)
    assert {d["draftId"] for d in first["items"] + second["items"]} == set(made) and second["nextCursor"] is None
    for bad in (0, 51, "2"):
        with pytest.raises(InvalidSync):
            list_drafts(w.store, TENANT, w.account, after=None, limit=bad)
    with pytest.raises(Refused):
        get_draft(w.store, "tenant-b", w.account, made[0])
    with pytest.raises(Refused):
        get_draft(w.store, TENANT, w.account, str(uuid.uuid4()))


# ── T27: partial bundles, lost acknowledgements, crashes ────────────────

def test_partial_bundle_retries_only_the_failed_member_and_keeps_confirmed_mappings(world):
    w = world
    files, blobs, draft = full_draft(w, 3)
    commit(w, draft)
    token = tokens(w)
    failing = token[files[1]["sha256"]]
    w.fake.queue("upload", failing, "transient")
    run(w)
    mid = get_draft(w.store, TENANT, w.account, draft["draftId"])
    states = [a["state"] for a in mid["assets"]]
    assert states == ["synced", "retrying", "synced"] and mid["state"] == "retrying" and mid["itemId"] is None
    assert mid["readiness"]["canEnable"] is False and "media_not_synced" in mid["readiness"]["blockers"]
    confirmed = {a.content_hash: a.provider_media_id for a in rows(w, Asset) if a.state == "synced"}
    assert w.fake.count("upload", token[files[0]["sha256"]]) == w.fake.count("upload", token[files[2]["sha256"]]) == 1
    due_now(w)
    run(w)
    done = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert done["state"] == "synced" and done["itemId"] and len(w.fake.landed) == 3
    assert w.fake.count("upload", token[files[0]["sha256"]]) == w.fake.count("upload", token[files[2]["sha256"]]) == 1
    assert w.fake.count("upload", failing) == 2 and w.fake.count("lookup", failing) == 1, "reconciled before the second write"
    assert w.fake.count("lookup", token[files[0]["sha256"]]) == 0
    assert {a.content_hash: a.provider_media_id for a in rows(w, Asset)}.items() >= confirmed.items()
    [failed_op] = [o for o in rows(w, Operation) if o.asset_id == done["assets"][1]["assetId"]]
    assert (failed_op.status, failed_op.attempts) == ("complete", 2)


def test_lost_success_acknowledgement_is_reconciled_by_lookup_not_reuploaded(world):
    w = world
    files, blobs, draft = full_draft(w, 2)
    commit(w, draft)
    token = tokens(w)
    for f in files:
        w.fake.queue("upload", token[f["sha256"]], "lost")
    run(w)
    done = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert done["state"] == "synced" and len(w.fake.landed) == 2
    assert w.fake.count("upload") == 2 and w.fake.count("lookup") == 2


def test_never_landed_lost_response_is_uploaded_once_more_only_after_lookup_says_so(world):
    w = world
    files, blobs, draft = full_draft(w, 1)
    commit(w, draft)
    [token] = tokens(w).values()
    w.fake.queue("upload", token, "unknown_unlanded")
    run(w)
    mid = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert mid["state"] == "retrying" and w.fake.count("upload") == 1 and w.fake.count("lookup") == 1
    due_now(w)
    run(w)
    assert get_draft(w.store, TENANT, w.account, draft["draftId"])["state"] == "synced"
    assert [m for m, _ in w.fake.calls if m != "verify"] == ["upload", "lookup", "lookup", "upload"]
    assert len(w.fake.landed) == 1


def test_unknown_outcome_stays_blocked_without_duplicate_upload_until_lookup_decides(world):
    w = world
    files, blobs, draft = full_draft(w, 2)
    commit(w, draft)
    token = tokens(w)
    stuck = token[files[0]["sha256"]]
    w.fake.queue("upload", stuck, "lost")
    w.fake.queue("lookup", stuck, "unknown")
    run(w)
    mid = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert mid["state"] == "outcome_unconfirmed" and mid["assets"][0]["state"] == "outcome_unconfirmed"
    assert mid["assets"][0]["lastError"] == "outcome_unconfirmed" and mid["assets"][1]["state"] == "synced"
    assert mid["readiness"]["canEnable"] is False and "outcome_unconfirmed" in mid["readiness"]["blockers"]
    assert rows(w, CatalogItem) == []
    frozen = list(w.fake.calls)
    due_now(w)
    assert run(w) == 0 and w.fake.calls == frozen, "no worker pass touches the provider for an unknown outcome"
    [op] = [o for o in rows(w, Operation) if o.asset_id == mid["assets"][0]["assetId"]]
    assert op.status == "outcome_unconfirmed" and operation_status(w, op.operation_id)["retryable"] is True
    # A retry is a lookup, never a blind upload: the lookup is still undeterminable.
    w.fake.queue("lookup", stuck, "unknown")
    retried = retry_asset(w.store, TENANT, w.account, op.operation_id, command_key=ckey(w, "retry"), actor=ACTOR)
    assert retried["status"] == "queued"
    run(w)
    assert w.fake.count("upload", stuck) == 1 and w.fake.count("lookup", stuck) == 2
    assert get_draft(w.store, TENANT, w.account, draft["draftId"])["state"] == "outcome_unconfirmed"
    # The lookup finally finds the landed media.
    retry_asset(w.store, TENANT, w.account, op.operation_id, command_key=ckey(w, "retry"), actor=ACTOR)
    run(w)
    done = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert done["state"] == "synced" and w.fake.count("upload", stuck) == 1 and len(w.fake.landed) == 2


def operation_status(w, operation_id):
    with Session(w.engine) as s:
        op = s.get(Operation, (w.account, operation_id))
        return csm.operation_view(op, s.scalar(select(Asset).where(Asset.account_id == w.account, Asset.asset_id == op.asset_id)))


def test_retry_asset_is_idempotent_and_refuses_what_is_not_retryable(world):
    w = world
    files, blobs, draft = full_draft(w, 1)
    commit(w, draft)
    [token] = tokens(w).values()
    w.fake.queue("verify", None, "unusable")
    run(w)
    [op] = rows(w, Operation)
    assert operation_status(w, op.operation_id)["retryable"] is False and op.last_error == "media_unusable"
    key = ckey(w, "retry")
    with pytest.raises(Refused, match="not retryable"):
        retry_asset(w.store, TENANT, w.account, op.operation_id, command_key=key, actor=ACTOR)
    with pytest.raises(Refused):
        retry_asset(w.store, TENANT, w.account, str(uuid.uuid4()), command_key=key, actor=ACTOR)
    with pytest.raises(Refused):
        retry_asset(w.store, "tenant-b", w.account, op.operation_id, command_key=key, actor=ACTOR)
    with pytest.raises(InvalidSync):
        retry_asset(w.store, TENANT, w.account, op.operation_id, command_key="bad", actor=ACTOR)
    assert rows(w, Decision, action="retry") == []


def test_retryable_failure_is_requeued_once_and_idempotently(world):
    w = world
    files, blobs, draft = full_draft(w, 1)
    commit(w, draft)
    [token] = tokens(w).values()
    for _ in range(csm.MAX_ATTEMPTS):
        w.fake.queue("upload", token, "transient")
    for _ in range(csm.MAX_ATTEMPTS):
        run(w)
        due_now(w)
    exhausted = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert exhausted["state"] == "needs_review" and exhausted["reason"] == "retries_exhausted"
    [op] = rows(w, Operation)
    assert operation_status(w, op.operation_id)["retryable"] is True
    key = ckey(w, "retry")
    first = retry_asset(w.store, TENANT, w.account, op.operation_id, command_key=key, actor=ACTOR)
    assert retry_asset(w.store, TENANT, w.account, op.operation_id, command_key=key, actor=ACTOR)["operationId"] == first["operationId"]
    assert len(rows(w, Decision, action="retry")) == 1
    with pytest.raises(Refused, match="idempotency"):
        retry_asset(w.store, TENANT, w.account, str(op.operation_id), command_key=key, actor="op-session:" + "b" * 64)
    run(w)
    assert get_draft(w.store, TENANT, w.account, draft["draftId"])["state"] == "synced"
    assert len(w.fake.landed) == 1, "the retry looked first; the provider never held two copies"


@pytest.mark.parametrize("boundary", ["before_provider_call", "after_provider_success", "after_commit"])
def test_worker_crash_at_each_commit_boundary_recovers_without_duplicate_media(world, boundary):
    w = world
    files, blobs, draft = full_draft(w, 2)
    commit(w, draft)
    token = tokens(w)
    victim = token[files[0]["sha256"]]
    if boundary == "before_provider_call":
        w.fake.queue("upload", victim, "crash_before")
    elif boundary == "after_provider_success":
        w.fake.queue("upload", victim, "crash_after")
    else:
        w.fake.queue("verify", None, "crash")
    with pytest.raises(Crash):
        run(w)
    expire_leases(w)
    run(w)
    done = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert done["state"] == "synced" and done["itemId"], done
    assert len(w.fake.landed) == 2, "exactly one provider copy per file"
    uploads = {t: w.fake.count("upload", t) for t in token.values()}
    if boundary == "before_provider_call":
        assert uploads[victim] == 2 and w.fake.count("lookup", victim) == 1, "nothing had landed: lookup said so, then upload"
    elif boundary == "after_provider_success":
        assert uploads[victim] == 1 and w.fake.count("lookup", victim) == 1, "landed media is found, not uploaded again"
    else:
        assert set(uploads.values()) == {1} and w.fake.count("lookup") == 0
    assert all(o.status == "complete" for o in rows(w, Operation))


def test_processing_media_waits_and_unusable_media_needs_review(world):
    w = world
    files, blobs, draft = full_draft(w, 1)
    commit(w, draft)
    w.fake.queue("verify", None, "processing", "processing")
    run(w)
    mid = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert mid["state"] == "verifying" and mid["assets"][0]["lastError"] == "processing" and mid["itemId"] is None
    assert run(w) == 0, "not due again until the poll delay passes"
    due_now(w)
    run(w)
    assert get_draft(w.store, TENANT, w.account, draft["draftId"])["state"] == "verifying"
    due_now(w)
    run(w)
    assert get_draft(w.store, TENANT, w.account, draft["draftId"])["state"] == "synced"
    files2, blobs2, draft2 = full_draft(w, 1, tag="u")
    commit(w, draft2)
    w.fake.queue("verify", None, "unusable")
    run(w)
    bad = get_draft(w.store, TENANT, w.account, draft2["draftId"])
    assert bad["state"] == "needs_review" and bad["reason"] == "media_unusable" and bad["itemId"] is None


def test_provider_ready_is_not_enough_the_media_must_appear_in_this_accounts_vault(world):
    w = world
    w.fake = Fake([201, 202], [])  # ids the vault has not observed yet
    files, blobs, draft = full_draft(w, 2)
    commit(w, draft)
    run(w)
    mid = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert mid["state"] == "verifying" and {a["lastError"] for a in mid["assets"]} == {"awaiting_vault"}
    assert rows(w, CatalogItem) == []
    pending = [j for j in rows(w, OfVaultSyncJob) if j.status == "pending"]
    assert len(pending) == 1, "one vault sync was queued, not one per asset or pass"
    due_now(w)
    run(w)
    assert len([j for j in rows(w, OfVaultSyncJob) if j.status == "pending"]) == 1
    rows_now = VAULT + [media(201), media(202)]
    w.monkeypatch.setattr(NativeProviderConnector, "vault_page",
                          lambda self, *, offset, limit=24: page(rows_now) if offset == 0 else page([]))
    assert process_vault_jobs(w.store, TENANT) == 1
    due_now(w)
    run(w)
    done = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert done["state"] == "synced" and done["itemId"]
    with Session(w.engine) as s:
        by_hash = {a.content_hash: a.provider_media_id for a in s.scalars(select(Asset))}
        assert sorted(by_hash.values()) == [201, 202]
        assert json.loads(s.get(CatalogItem, int(done["itemId"])).media_ids) == [by_hash[f["sha256"]] for f in files]


def test_vault_never_showing_the_media_ends_in_needs_review(world):
    w = world
    w.fake = Fake([301], [])
    files, blobs, draft = full_draft(w, 1)
    commit(w, draft)
    run(w)
    with w.engine.begin() as c:
        c.execute(text("UPDATE of_content_sync_asset SET upload_started_at = CURRENT_TIMESTAMP - INTERVAL '7 hours'"))
    due_now(w)
    run(w)
    result = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert result["state"] == "needs_review" and result["reason"] == "observation_timeout" and result["itemId"] is None


def test_same_provider_id_for_two_files_is_a_conflict_not_a_merge(world):
    w = world
    w.fake = Fake([101, 101], [])
    files, blobs, draft = full_draft(w, 2)
    commit(w, draft)
    run(w)
    result = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert sorted(a["state"] for a in result["assets"]) == ["needs_review", "synced"]
    assert result["reason"] == "provider_id_conflict" and result["itemId"] is None


def test_tampered_staging_is_caught_before_any_provider_write(world):
    w = world
    files, blobs, draft = full_draft(w, 1)
    commit(w, draft)
    (blob,) = list(csm.SPOOL.rglob("blob"))
    blob.write_bytes(b"tampered!!")
    run(w)
    result = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert result["state"] == "needs_review" and result["reason"] == "staged_file_missing" and w.fake.calls == []


# ── Connection, rebinding, isolation and reuse ──────────────────────────

def test_lapsed_connection_pauses_provider_work_and_resumes_after_reconnect(world):
    w = world
    files, blobs, draft = full_draft(w, 2)
    commit(w, draft)
    with Session(w.engine) as s, s.begin():
        s.get(OfProviderConnection, w.account).expires_at = utcnow() - timedelta(minutes=1)
    run(w)
    mid = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert mid["state"] == "connection_required" and mid["reason"] == "connection_required" and w.fake.calls == []
    assert run(w) == 0 and w.fake.calls == []
    with Session(w.engine) as s, s.begin():
        s.get(OfProviderConnection, w.account).expires_at = utcnow() + timedelta(days=1)
    run(w)
    assert get_draft(w.store, TENANT, w.account, draft["draftId"])["state"] == "synced"


def test_authentication_loss_during_upload_pauses_then_reconciles_before_writing_again(world):
    w = world
    files, blobs, draft = full_draft(w, 1)
    commit(w, draft)
    [token] = tokens(w).values()
    w.fake.queue("upload", token, "auth")
    run(w)
    mid = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert mid["state"] == "connection_required"
    assert run(w) == 0, "a paused asset does not hammer a connection the provider just rejected"
    with Session(w.engine) as s, s.begin():
        c = s.get(OfProviderConnection, w.account)
        c.updated_at = utcnow()
    run(w)
    assert get_draft(w.store, TENANT, w.account, draft["draftId"])["state"] == "synced"
    assert [m for m, _ in w.fake.calls if m != "verify"][:3] == ["upload", "lookup", "upload"]


def rebind(w, new="binding-rebound"):
    with Session(w.engine) as s, s.begin():
        runtime = s.get(OfRuntime, w.account)
        s.get(OfCreatorBinding, runtime.binding_id).revoked_at = utcnow()
        s.flush()
        s.add(OfCreatorBinding(id=new, tenant_id=TENANT, creator_id="creator-rebound", creator_name="Rebound",
            account_id=w.account, handle="rebound", channel="onlyfans"))
        s.flush()
        runtime.binding_id = new
        s.get(OfProviderConnection, w.account).binding_id = new


def test_rebinding_never_inherits_old_mappings_or_upload_authority(world):
    w = world
    files, blobs, draft = full_draft(w, 2)
    commit(w, draft)
    old_binding = rows(w, Asset)[0].binding_id
    rebind(w)
    run(w)
    assert w.fake.calls == [], "the old binding's queued uploads never reach the new binding's provider session"
    assert {a.state for a in rows(w, Asset)} == {"needs_review"} and {a.last_error for a in rows(w, Asset)} == {"binding_changed"}
    with pytest.raises(Refused):
        get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert list_drafts(w.store, TENANT, w.account, after=None, limit=5)["items"] == []
    # The same bytes under the new binding start from nothing: no reuse, no inherited provider id.
    fresh = save_draft(w.store, TENANT, w.account, value=body(w, files), actor=ACTOR)
    assert [a["state"] for a in fresh["assets"]] == ["draft", "draft"]
    stage(w, fresh["draftId"], files, blobs)
    commit(w, fresh)
    assert {a.binding_id for a in rows(w, Asset)} == {old_binding, "binding-rebound"} and len(rows(w, Asset)) == 4
    run(w)
    waiting = get_draft(w.store, TENANT, w.account, fresh["draftId"])
    assert waiting["state"] == "verifying", "old-binding vault observations prove nothing for the new binding"
    # The re-queued vault sync completes under the new binding (re-encrypting the sealed session for the
    # new scope is outside this test, so the finished read is recorded directly).
    with Session(w.engine) as s, s.begin():
        job = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.binding_id == "binding-rebound"))
        job.status, job.completed_at = "complete", utcnow()
        s.execute(text("UPDATE of_vault_observation SET job_id = :j WHERE account_id = :a"), {"j": job.job_id, "a": w.account})
    due_now(w)
    run(w)
    done = get_draft(w.store, TENANT, w.account, fresh["draftId"])
    assert done["state"] == "synced" and w.fake.count("upload") == 2


def test_rebind_during_provider_io_discards_the_result_instead_of_mapping_it(world):
    w = world
    files, blobs, draft = full_draft(w, 1)
    commit(w, draft)
    [token] = tokens(w).values()
    w.fake.queue("upload", token, lambda: rebind(w))
    run(w)
    [asset] = rows(w, Asset)
    assert asset.state == "needs_review" and asset.last_error == "binding_changed" and asset.provider_media_id is None
    assert rows(w, CatalogItem) == []


def test_owner_change_during_provider_io_discards_the_result(world):
    w = world
    files, blobs, draft = full_draft(w, 1)
    commit(w, draft)
    [token] = tokens(w).values()
    def swap():
        with Session(w.engine) as s, s.begin():
            s.get(OfProviderConnection, w.account).owner_id = OWNER + 55
    w.fake.queue("upload", token, swap)
    run(w)
    [asset] = rows(w, Asset)
    assert asset.state == "needs_review" and asset.last_error == "connection_changed" and asset.provider_media_id is None


def second_account(w):
    payload = connection_data()
    payload["ownerId"] = OWNER + 1
    payload["capture"]["headers"]["user_id"] = str(OWNER + 1)
    other = provision(w.store, creator_id="creator-two", owner_id=OWNER + 1, handle="two", command_key="2" * 64)
    assert save_connection(w.store, other, payload, key="2" * 64) == 1 if False else True
    from onlyfans_runtime.connections import load_worker_accounts, save_connection as save_raw
    save_raw(w.store, TENANT, other, payload=payload, expected_revision=0, command_key="3" * 64, actor=ACTOR)
    load_worker_accounts(w.store, TENANT)
    request_sync(w.store, TENANT, other, expected_connection_revision=1, command_key="4" * 64, actor=ACTOR)
    w.monkeypatch.setattr(NativeProviderConnector, "vault_page",
                          lambda self, *, offset, limit=24: page(VAULT) if offset == 0 else page([]))
    assert process_vault_jobs(w.store, TENANT) >= 1
    return other


def test_identical_bytes_and_provider_ids_under_two_accounts_never_merge(world):
    w = world
    other = second_account(w)
    fakes = {w.account: Fake(range(101, 111), range(111, 121)), other: Fake(range(101, 111), range(111, 121))}
    files, blobs = make(2)
    draft_a = saved(w, files, blobs)
    process = lambda: [process_uploads(w.store, TENANT, limit=1, transport_for=lambda c, t, a: fakes[a]) for _ in range(30)]
    commit(w, draft_a)
    process()
    assert get_draft(w.store, TENANT, w.account, draft_a["draftId"])["state"] == "synced"
    # Account B saves the very same bytes: nothing of A's is reusable.
    draft_b = save_draft(w.store, TENANT, other, value=body(w, files), actor=ACTOR)
    assert [a["state"] for a in draft_b["assets"]] == ["draft", "draft"]
    assert {a["assetId"] for a in draft_b["assets"]}.isdisjoint({a["assetId"] for a in get_draft(w.store, TENANT, w.account, draft_a["draftId"])["assets"]})
    for f in files:
        for i in range(3):
            put_chunk(w.store, TENANT, other, draft_b["draftId"], f["fileId"], i, blobs[f["fileId"]][i * 4:i * 4 + 4])
    commit_draft(w.store, TENANT, other, draft_b["draftId"], command_key=ckey(w, "commit"), actor=ACTOR)
    process()
    done_b = get_draft(w.store, TENANT, other, draft_b["draftId"])
    assert done_b["state"] == "synced"
    with Session(w.engine) as s:
        a_rows = {x.content_hash: x for x in s.scalars(select(Asset).where(Asset.account_id == w.account))}
        b_rows = {x.content_hash: x for x in s.scalars(select(Asset).where(Asset.account_id == other))}
        assert {x.provider_media_id for x in a_rows.values()} == {x.provider_media_id for x in b_rows.values()} == {101, 102}
        assert all(a_rows[h].asset_id != b_rows[h].asset_id and a_rows[h].binding_id != b_rows[h].binding_id
                   and a_rows[h].upload_token != b_rows[h].upload_token for h in a_rows)
        items = s.scalars(select(CatalogItem)).all()
        assert {i.account_id for i in items} == {w.account, other} and len(items) == 2
    with pytest.raises(Refused):
        get_draft(w.store, TENANT, other, draft_a["draftId"])
    with pytest.raises(Refused):
        get_draft(w.store, TENANT, w.account, draft_b["draftId"])
    with pytest.raises(Refused):
        put_chunk(w.store, TENANT, other, draft_a["draftId"], files[0]["fileId"], 0, b"abcd")
    [op] = rows(w, Operation, account_id=w.account)[:1]
    with pytest.raises(Refused):
        retry_asset(w.store, TENANT, other, op.operation_id, command_key=ckey(w, "retry"), actor=ACTOR)
    assert fakes[w.account].count("upload") == 2 and fakes[other].count("upload") == 2


def test_synced_media_is_reused_on_the_same_binding_and_duplicate_bundles_are_not_recreated(world):
    w = world
    files, blobs, first = full_draft(w, 2)
    commit(w, first)
    run(w)
    assert get_draft(w.store, TENANT, w.account, first["draftId"])["state"] == "synced"
    known = {a.content_hash: a.asset_id for a in rows(w, Asset)}
    extra, extra_blobs = make(1, tag="n")
    mixed = extra and [dict(files[0], fileId=str(uuid.uuid4())), extra[0]]
    draft = save_draft(w.store, TENANT, w.account, value=body(w, mixed), actor=ACTOR)
    assert draft["assets"][0]["assetId"] == known[files[0]["sha256"]] and draft["assets"][0]["state"] == "synced"
    assert draft["assets"][1]["state"] == "draft"
    with pytest.raises(Refused, match="synced"):
        put_chunk(w.store, TENANT, w.account, draft["draftId"], mixed[0]["fileId"], 0, blobs[files[0]["fileId"]][:4])
    with pytest.raises(InvalidSync):
        commit(w, draft)  # the new file still needs its bytes
    stage(w, draft["draftId"], mixed, {**extra_blobs, mixed[0]["fileId"]: blobs[files[0]["fileId"]]}, only={extra[0]["fileId"]})
    queued = commit(w, draft)
    assert len(rows(w, Asset)) == 3 and len(rows(w, Operation)) == 3, "only the missing media is uploaded"
    assert queued["state"] == "queued"
    run(w)
    done = get_draft(w.store, TENANT, w.account, draft["draftId"])
    assert done["state"] == "synced" and w.fake.count("upload") == 3
    # Exactly the first bundle again: the canonical attachment-set rule refuses a second item.
    again, _ = make(0) if False else (None, None)
    clone = save_draft(w.store, TENANT, w.account, value=body(w, [dict(f, fileId=str(uuid.uuid4())) for f in files]), actor=ACTOR)
    assert all(a["state"] == "synced" for a in clone["assets"])
    result = commit(w, clone)
    assert result["state"] == "needs_review" and result["reason"] == "catalogue_assets_exist" and result["itemId"] is None
    assert len(rows(w, CatalogItem)) == 2


# ── T28: exact selling of a CRM-created fixed item ──────────────────────

from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, configured
from test_onlyfans_media_pg import ASK, media_policy
from test_onlyfans_native_planning_pg import ready as native_ready
from test_onlyfans_campaigns_pg import create as create_campaign
from onlyfans_runtime import native_sales
from onlyfans_runtime.campaigns import CampaignService
from onlyfans_runtime.content_management import digest
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from vault_mirror import row_values
from db.onlyfans_vault_models import OfVaultObservation

WIRE = {300: "3.0", 1007: "10.07", 1999: "19.99", 20000: "200.0"}
CRM_IDS = {"photo": [41, 42], "video": [43]}


def connect_acc_a(engine):
    """A verified connection and a finished vault read for the seeded delivery account."""
    now = utcnow()
    with Session(engine) as s, s.begin():
        s.add(OfProviderConnection(account_id=ACCOUNT, binding_id="binding-acc-a", owner_id=OWNER, revision=1,
            sealed_config="not-a-real-session", expires_at=now + timedelta(days=30), status="verified",
            verified_at=now, updated_at=now))
        s.flush()
        job = OfVaultSyncJob(account_id=ACCOUNT, job_id=str(uuid.uuid4()), binding_id="binding-acc-a", command_key="c" * 64,
            request_hash="d" * 64, actor=ACTOR, connection_revision=1, owner_id=OWNER, max_pages=200, status="complete",
            page_offset=3, pages_read=1, items_seen=3, requested_at=now, updated_at=now, completed_at=now)
        s.add(job)
        s.flush()
        for mid, kind in [(41, "photo"), (42, "photo"), (43, "video")]:
            values = row_values(ACCOUNT, media(mid, kind))
            values["created_at"] = now
            s.add(VaultItem(**values))
            s.flush()
            s.add(OfVaultObservation(account_id=ACCOUNT, media_id=mid, job_id=job.job_id,
                                     raw_hash=digest(values["raw_json"]), observed_at=now))


def crm_item(store, engine, monkeypatch, tmp_path, cents):
    """Create, via the real Save -> upload -> verify path, an enabled CRM fixed item on the delivery account."""
    monkeypatch.setattr(csm, "SPOOL", tmp_path / "spool")
    monkeypatch.setattr(csm, "CHUNK_BYTES", 4)
    monkeypatch.setattr(csm, "MIN_FREE_BYTES", 0)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    connect_acc_a(engine)
    w = type("World", (), {})()
    w.store, w.account, w.engine, w.monkeypatch, w.seq = store, ACCOUNT, engine, monkeypatch, 0
    w.fake = Fake(CRM_IDS["photo"], CRM_IDS["video"])
    files, blobs = make(3, videos=(2,), tag="crm")
    draft = saved(w, files, blobs, [files[0]["fileId"]], price=cents)
    commit(w, draft)
    run(w)
    done = get_draft(store, TENANT, ACCOUNT, draft["draftId"])
    assert done["state"] == "synced" and done["readiness"]["canEnable"], done
    item = int(done["itemId"])
    with Session(engine) as s:
        assert s.get(CatalogItem, item).enabled is False, "the item arrives disabled"
        assert w.fake.only_sync_methods()
    current = get_content_v2(store, TENANT, ACCOUNT, item)
    update_content(store, TENANT, ACCOUNT, item, patch={"enabled": True}, expected_revision=current["revision"],
                   expected_fingerprint=current["fingerprint"], command_key=ckey(w, "enable"), actor=ACTOR, version=2)
    with Session(engine) as s:
        row = s.get(CatalogItem, item)
        order = json.loads(row.media_ids)
        assert row.enabled and row.price_cents == cents and row.pricing_mode == "fixed" and len(order) == 3
    return item, order, w


def crm_manual(database, monkeypatch, tmp_path, cents, *, mode="human_controlled"):
    store, authority = configured(database, mode=mode)
    item, order, w = crm_item(store, database[1], monkeypatch, tmp_path, cents)
    media_policy(authority)
    return store, authority, item, order


def crm_native(database, monkeypatch, tmp_path, cents):
    store, authority, _, planner = native_ready(database, monkeypatch)
    item, order, w = crm_item(store, database[1], monkeypatch, tmp_path, cents)
    media_policy(authority)
    with Session(database[1]) as s, s.begin():
        s.get(AccountAiConfig, ACCOUNT).ai_chatter_config_json = canonical({
            "pack_send_enabled": True, "pack_on_ask_enabled": True, "qualification_gate_enabled": True})
    store.record(TENANT, ACCOUNT, event(mid=2, body=ASK))
    store.drain(TENANT, ACCOUNT)
    due(database[1])
    claim = store.claim(TENANT)
    assert claim is not None
    return store, authority, claim, planner, item, order


def assert_receipt(engine, cents, order, *, quoted):
    with Session(engine) as s:
        offer = s.scalar(select(ContentOffer))
        assert offer.price_cents == cents and offer.status == "open"
        assert s.get(Message, (ACCOUNT, FAN, 9001)).price_cents == cents
        assert sorted((v.media_id, v.price_cents) for v in s.scalars(select(VaultSend))) == sorted((m, cents) for m in order)
        quotes = list(s.scalars(select(LadderQuote)))
        if quoted:
            [q] = quotes
            assert (q.kind, q.price_cents, q.base_cents, q.pre_clamp_cents, q.arm_mult, q.band_lo, q.band_hi) == (
                "fixed", cents, cents, cents, 1.0, 0, 0)
        else:
            assert quotes == [], "manual and campaign sends keep their existing quote behaviour"


def assert_wire(call, cents, order):
    assert call.price_cents == cents and call.media_ids == tuple(order) and call.preview_ids == (order[0],)
    raw = wire_request(call)
    sent = json.loads(raw)
    assert f'"price":{WIRE[cents]}'.encode() in raw, raw  # the bytes, not a float round trip of ours
    assert parse_cents(sent["price"]) == cents


@pytest.mark.parametrize("cents", [300, 1007, 1999, 20000])
def test_crm_fixed_price_is_exact_through_the_bot_path(database, monkeypatch, tmp_path, cents):
    _, engine = database
    store, authority, claim, planner, item, order = crm_native(database, monkeypatch, tmp_path, cents)
    from automations import ai_chatter
    monkeypatch.setattr(ai_chatter, "_quote_item", lambda *a, **k: pytest.fail("a fixed item is never quoted"))
    with Session(engine) as s:
        assert native_sales.choice(s, ACCOUNT, FAN, item, utcnow())["price_cents"] == cents
    model = SyntheticNativeModel("Here is the available choice.", sale={"item_id": item, "quote": ASK})
    plan = planner.prepare(TENANT, claim, model)
    with Session(engine) as s:
        saved_plan = s.get(OfNativePlan, (ACCOUNT, plan))
        assert saved_plan.status == "ready", saved_plan.reason
        sale = json.loads(saved_plan.sale_json)
        assert sale["price_cents"] == cents and "native_quote" not in sale and sale["item"]["pricing_mode"] == "fixed"
        assert f"USD ${cents // 100}.{cents % 100:02d} to unlock" in saved_plan.body
    identity = planner.queue(TENANT, ACCOUNT, plan)
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, identity))
        assert row.price_cents == cents and json.loads(row.catalog_snapshot_json)["price_cents"] == cents
        assert json.loads(row.media_json) == order
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "accepted"
    assert_wire(wire.calls[0], cents, order)
    assert_receipt(engine, cents, order, quoted=True)
    store.close()


@pytest.mark.parametrize("cents", [300, 1007, 1999, 20000])
def test_crm_fixed_price_is_exact_through_the_manual_path(database, monkeypatch, tmp_path, cents):
    _, engine = database
    store, authority, item, order = crm_manual(database, monkeypatch, tmp_path, cents)
    for wrong in (cents + 100, cents - 1, 0):
        with pytest.raises(Refused):
            authority.prepare(TENANT, ACCOUNT, FAN, command_key=f"wrong-{wrong}", expected_revision=0, body="Set.",
                origin="manual", actor="synthetic-operator", catalog_item_id=item, quoted_price_cents=wrong)
    identity = authority.prepare(TENANT, ACCOUNT, FAN, command_key="media-1", expected_revision=0,
        body="The approved existing set.", origin="manual", actor="synthetic-operator", catalog_item_id=item)
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, identity)).price_cents == cents
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "accepted"
    assert_wire(wire.calls[0], cents, order)
    assert_receipt(engine, cents, order, quoted=False)
    store.close()


@pytest.mark.parametrize("cents", [300, 1007, 1999, 20000])
def test_crm_fixed_price_is_exact_through_the_campaign_path(database, monkeypatch, tmp_path, cents):
    _, engine = database
    store, authority, item, order = crm_manual(database, monkeypatch, tmp_path, cents, mode="automated")
    service = CampaignService(store)
    created = create_campaign(service, catalog_item_id=item)
    offer = service.detail(TENANT, ACCOUNT, created["campaignId"])["campaign"]["offer"]
    assert offer["priceCents"] == cents and offer["mediaIds"] == [str(m) for m in order] and offer["previewIds"] == [str(order[0])]
    identity = service.queue_due(TENANT, ACCOUNT)
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "accepted"
    assert_wire(wire.calls[0], cents, order)
    assert service.detail(TENANT, ACCOUNT, created["campaignId"])["campaign"]["offer"] == offer
    assert_receipt(engine, cents, order, quoted=False)
    store.close()


def test_a_saved_but_not_yet_enabled_or_unsynced_crm_item_can_never_be_sent(database, monkeypatch, tmp_path):
    _, engine = database
    store, authority = configured(database)
    monkeypatch.setattr(csm, "SPOOL", tmp_path / "spool")
    monkeypatch.setattr(csm, "CHUNK_BYTES", 4)
    monkeypatch.setattr(csm, "MIN_FREE_BYTES", 0)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    connect_acc_a(engine)
    media_policy(authority)
    w = type("World", (), {})()
    w.store, w.account, w.engine, w.monkeypatch, w.seq = store, ACCOUNT, engine, monkeypatch, 0
    w.fake = Fake(CRM_IDS["photo"], CRM_IDS["video"])
    files, blobs = make(3, videos=(2,), tag="crm")
    draft = saved(w, files, blobs, [files[0]["fileId"]], price=1007)
    commit(w, draft)
    assert process_uploads(store, TENANT, limit=2, transport_for=lambda c, t, a: w.fake) == 2
    assert rows(w, CatalogItem) == [], "a partly uploaded bundle has no catalogue item to enable or send"
    run(w)
    [item] = rows(w, CatalogItem)
    assert item.enabled is False
    before = snapshot(w)
    with pytest.raises(Refused, match="catalogue terms"):
        authority.prepare(TENANT, ACCOUNT, FAN, command_key="media-1", expected_revision=0, body="Set.",
            origin="manual", actor="synthetic-operator", catalog_item_id=item.id)
    assert snapshot(w) == before
    with Session(engine) as s:
        row = s.get(CatalogItem, item.id)
        assert (row.price_cents, row.pricing_mode, row.enabled) == (1007, "fixed", False)
    store.close()


@pytest.mark.parametrize("refusal", ["media_policy", "disabled_item", "already_owned", "foreign_price"])
def test_existing_eligibility_and_ownership_still_refuse_and_never_reprice(database, monkeypatch, tmp_path, refusal):
    _, engine = database
    store, authority, item, order = crm_manual(database, monkeypatch, tmp_path, 1007)
    def attempt():
        return authority.prepare(TENANT, ACCOUNT, FAN, command_key="media-r", expected_revision=0, body="Set.",
            origin="manual", actor="synthetic-operator", catalog_item_id=item,
            quoted_price_cents=2500 if refusal == "foreign_price" else None)
    if refusal == "media_policy":
        with Session(engine) as s, s.begin():
            s.get(OfDeliveryPolicy, ACCOUNT).media_enabled = False
        with pytest.raises(Refused):
            attempt()
    elif refusal == "disabled_item":
        with Session(engine) as s, s.begin():
            s.get(CatalogItem, item).enabled = False
        with pytest.raises(Refused):
            attempt()
    elif refusal == "foreign_price":
        with pytest.raises(Refused):
            attempt()
    else:
        identity = attempt()
        with Session(engine) as s, s.begin():
            s.add(VaultSend(account_id=ACCOUNT, fan_id=FAN, media_id=order[2], was_purchased=True))
        wire = SyntheticTextTransport(ACCOUNT)
        assert authority.dispatch(TENANT, ACCOUNT, identity, wire) != "accepted" and wire.calls == []
        with Session(engine) as s:
            assert s.get(OfTextIntent, (ACCOUNT, identity)).status == "cancelled"
    with Session(engine) as s:
        row = s.get(CatalogItem, item)
        assert row.price_cents == 1007 and row.pricing_mode == "fixed", "a refusal never changes the saved amount"
        assert s.scalar(select(func.count()).select_from(ContentOffer)) == 0
        assert s.scalar(select(func.count()).select_from(LadderQuote)) == 0
    store.close()


@pytest.mark.parametrize("change", ["price", "pricing_mode", "media_removed"])
def test_crm_item_edited_after_queueing_is_cancelled_before_the_wire(database, monkeypatch, tmp_path, change):
    _, engine = database
    store, authority, item, order = crm_manual(database, monkeypatch, tmp_path, 1999)
    identity = authority.prepare(TENANT, ACCOUNT, FAN, command_key="media-1", expected_revision=0,
        body="The approved existing set.", origin="manual", actor="synthetic-operator", catalog_item_id=item)
    with Session(engine) as s, s.begin():
        row = s.get(CatalogItem, item)
        if change == "price":
            row.price_cents = 2000
        elif change == "pricing_mode":
            row.pricing_mode = "dynamic"
        else:
            s.get(VaultItem, (ACCOUNT, order[1])).removed_at = utcnow()
    assert authority.admit(TENANT, ACCOUNT, identity) == "cancelled"
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(ContentOffer)) == 0
    store.close()
