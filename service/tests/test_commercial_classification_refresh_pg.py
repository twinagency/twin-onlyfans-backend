"""R3: commercial classification is re-evaluated from CURRENT evidence on every assignment.

Isolated PostgreSQL, no model/provider calls. One-time folder markers keep their purpose
(operator removals, renames, archives, locks, prices) but never freeze the classification.
"""
import asyncio
import json
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from db.models import CatalogItem, VaultItem, VaultFolder, VaultFolderItem
from test_onlyfans_foundation_pg import database
from test_commercial_vault_pg import setup, ACCOUNT, TENANT, ACTOR, _item
from fans_api_adapter.vault_sync import sync_vault
from vault_auto_folders import assign_sync, OWNER_PREFIX
from vault_describe_state import stamp_of
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.vault_editing import (get_category_review, get_category_price, set_category_price,
                                            assert_category_review)
from onlyfans_runtime.vault_management import create_category_drafts

MISC = OWNER_PREFIX + "miscellaneous"
TEXT = "A casual outdoor photograph."


@pytest.fixture
def store(database):
    store, client, calls, state = setup(database, [_item(11), _item(12)])
    assert sync_vault(store, TENANT, ACCOUNT, client)["complete"]
    yield store
    store.close()


def analyse(s, mid=11, *, confidence=99, text=TEXT, stamped=True):
    item = s.get(VaultItem, (ACCOUNT, mid))
    fields = json.loads(item.ai_fields_json)
    fields.update({"description": text, "confidence": confidence,
                   "_content_stamp": stamp_of(item) if stamped else "prior-content"})
    if text is None:
        fields.pop("description")
    item.ai_fields_json = json.dumps(fields)
    return assign_sync(s, ACCOUNT, mid)


def cls(s, mid=11):
    return json.loads(s.get(VaultItem, (ACCOUNT, mid)).ai_fields_json)["_commercial_classification_v1"]


def members(s, mid=11):
    rows = s.execute(select(VaultFolder.created_by, VaultFolder.name, VaultFolder.deleted_at)
        .join(VaultFolderItem, (VaultFolderItem.account_id == VaultFolder.account_id)
              & (VaultFolderItem.folder_id == VaultFolder.id))
        .where(VaultFolderItem.account_id == ACCOUNT, VaultFolderItem.media_id == mid)).all()
    return sorted((r[0], r[1]) for r in rows)


def auto_keys(s, mid=11):
    return sorted(c[len(OWNER_PREFIX):] for c, _ in members(s, mid) if c.startswith(OWNER_PREFIX))


def snapshot(s, mid=11):
    item = s.get(VaultItem, (ACCOUNT, mid))
    return item.ai_fields_json, members(s, mid)


def operator_folder(s, mid, name="Operator set"):
    folder = VaultFolder(account_id=ACCOUNT, name=name, created_by=ACTOR)
    s.add(folder); s.flush()
    s.add(VaultFolderItem(account_id=ACCOUNT, folder_id=folder.id, media_id=mid))
    return folder.id


def assert_unsellable(classification):
    assert classification["sale_approved"] is False


def test_high_to_low_confidence_becomes_uncertain_miscellaneous(store):
    with Session(store.engine) as s, s.begin():
        assert analyse(s)["status"] == "categorised"
        first = cls(s)
        assert first["status"] == "proposed" and "casual" in first["categories"]
        assert MISC not in [c for c, _ in members(s)]
        result = analyse(s, confidence=20)
        last = cls(s)
        assert result["status"] == "categorised" and result["categories"] == ["miscellaneous"]
        assert last["status"] == "uncertain" and last["reason"] == "low_confidence"
        assert last["categories"] == ["miscellaneous"] and last["content_stamp"] == first["content_stamp"]
        assert_unsellable(last)
        assert MISC in [c for c, _ in members(s)]
        # boundary: 70 is proposed, 69 is not
        analyse(s, confidence=70); assert cls(s)["status"] == "proposed"
        analyse(s, confidence=69); assert cls(s)["status"] == "uncertain"


def test_low_to_high_confidence_becomes_proposed_and_drops_only_internal_miscellaneous(store):
    with Session(store.engine) as s, s.begin():
        analyse(s, confidence=20)
        assert cls(s)["status"] == "uncertain" and auto_keys(s) == ["miscellaneous"]
        op = operator_folder(s, 11)
        analyse(s, confidence=95)
        last = cls(s)
        assert last["status"] == "proposed" and last["reason"] == "metadata_evidence"
        assert {"casual", "outdoors"} <= set(last["categories"]) and last["categories"] == auto_keys(s)
        assert MISC not in [c for c, _ in members(s)]
        assert (ACTOR, "Operator set") in members(s)
        assert_unsellable(last)


def test_changed_or_unbound_content_stamp_invalidates_and_rebinding_restores(store):
    with Session(store.engine) as s, s.begin():
        analyse(s)
        assert cls(s)["status"] == "proposed"
        s.get(VaultItem, (ACCOUNT, 11)).content_hash = "f" * 64   # content changed after analysis
        assert assign_sync(s, ACCOUNT, 11)["categories"] == ["miscellaneous"]
        last = cls(s)
        assert last["status"] == "uncertain" and last["reason"] == "analysis_version_unbound"
        assert last["categories"] == ["miscellaneous"] and MISC in [c for c, _ in members(s)]
        assert_unsellable(last)
        analyse(s)  # re-analysed against the new content
        assert cls(s)["status"] == "proposed" and cls(s)["content_stamp"] == stamp_of(s.get(VaultItem, (ACCOUNT, 11)))
        assert MISC not in [c for c, _ in members(s)]
        analyse(s, stamped=False)
        assert cls(s)["reason"] == "analysis_version_unbound"


def test_removed_metadata_becomes_uncertain_and_returns_when_restored(store):
    with Session(store.engine) as s, s.begin():
        analyse(s)
        analyse(s, text=None)
        last = cls(s)
        assert last["status"] == "uncertain" and last["reason"] == "analysis_pending"
        assert last["categories"] == ["miscellaneous"] and MISC in [c for c, _ in members(s)]
        assert_unsellable(last)
        analyse(s)
        assert cls(s)["status"] == "proposed" and MISC not in [c for c, _ in members(s)]


def test_operator_memberships_survive_both_directions_and_removals_are_not_readded(store):
    with Session(store.engine) as s, s.begin():
        analyse(s)
        op = operator_folder(s, 11, "Manual picks")
        # operator removes the automatic Casual filing
        casual = s.scalar(select(VaultFolder.id).where(VaultFolder.created_by == OWNER_PREFIX + "casual"))
        s.delete(s.get(VaultFolderItem, (ACCOUNT, casual, 11))); s.flush()
        analyse(s, confidence=10)
        assert (ACTOR, "Manual picks") in members(s) and "casual" not in auto_keys(s)
        analyse(s, confidence=90)
        assert (ACTOR, "Manual picks") in members(s)
        assert "casual" not in auto_keys(s), "operator removal must not be undone by a later refresh"
        assert "outdoors" in auto_keys(s) and "miscellaneous" not in auto_keys(s)
        # operator-created folder named Miscellaneous is never touched
        mine = operator_folder(s, 11, "Miscellaneous")
        analyse(s, confidence=10); analyse(s, confidence=90)
        assert s.get(VaultFolderItem, (ACCOUNT, mine, 11)) is not None


def test_new_evidence_adds_only_newly_derived_categories(store):
    with Session(store.engine) as s, s.begin():
        analyse(s)
        analyse(s, text="A fitness photo at the gym.")
        assert cls(s)["status"] == "proposed" and "fitness" in cls(s)["categories"]
        assert {"casual", "outdoors", "fitness"} <= set(auto_keys(s))


def test_archived_miscellaneous_and_category_folders_are_not_recreated(store):
    with Session(store.engine) as s, s.begin():
        misc_before = s.scalar(select(VaultFolder).where(VaultFolder.created_by == MISC))
        assert misc_before is not None          # created by the first pending sync
        misc_before.deleted_at = utcnow()
        analyse(s)
        casual = s.scalar(select(VaultFolder).where(VaultFolder.created_by == OWNER_PREFIX + "casual"))
        casual.deleted_at = utcnow(); s.flush()
        before = s.scalar(select(func.count()).select_from(VaultFolder))
        analyse(s, confidence=5)
        assert cls(s)["status"] == "uncertain"
        analyse(s, confidence=95)
        assert s.scalar(select(func.count()).select_from(VaultFolder)) == before
        assert s.scalar(select(func.count()).select_from(VaultFolder).where(VaultFolder.created_by == MISC)) == 1
        assert s.scalar(select(func.count()).select_from(VaultFolder).where(
            VaultFolder.created_by == OWNER_PREFIX + "casual")) == 1
        assert MISC not in [c for c, _ in members(s)]
        assert casual.deleted_at is not None
        assert OWNER_PREFIX + "outdoors" in [c for c, _ in members(s)]


def test_operator_lock_override_rename_and_price_are_respected(store):
    with Session(store.engine) as s, s.begin():
        analyse(s)
        item = s.get(VaultItem, (ACCOUNT, 11))
        item.operator_overrides_json = json.dumps({"description": "Operator corrected: gym workout.", "tags": ["fitness"]})
        item.locked_fields_json = json.dumps(["description", "tags"])
        s.flush()
        before = (item.operator_overrides_json, item.locked_fields_json)
        casual = s.scalar(select(VaultFolder).where(VaultFolder.created_by == OWNER_PREFIX + "casual"))
        casual.name = "Renamed by operator"; casual.single_price_cents = 1500; casual.price_revision = 3
        casual.pricing_binding_id = "binding-x"; casual.pricing_actor = ACTOR; casual.pricing_updated_at = utcnow()
        s.flush()
        analyse(s, text="A casual outdoor photograph.", confidence=30)
        assert cls(s)["status"] == "uncertain"
        analyse(s, confidence=90)
        last = cls(s)
        assert last["status"] == "proposed" and "fitness" in last["categories"]      # locked override is the evidence
        item = s.get(VaultItem, (ACCOUNT, 11))
        assert (item.operator_overrides_json, item.locked_fields_json) == before
        casual = s.get(VaultFolder, casual.id)
        assert (casual.name, casual.single_price_cents, casual.price_revision) == ("Renamed by operator", 1500, 3)
        assert_unsellable(last)


def test_repeated_refresh_with_unchanged_evidence_is_idempotent(store):
    with Session(store.engine) as s, s.begin():
        for confidence in (95, 20):
            analyse(s, confidence=confidence)
            state = snapshot(s)
            for _ in range(3):
                result = assign_sync(s, ACCOUNT, 11)
                assert result == {"status": "already_categorised"}
                assert snapshot(s) == state
        # unchanged pending state too
        s.get(VaultItem, (ACCOUNT, 12))
        first = assign_sync(s, ACCOUNT, 12); state = snapshot(s, 12)
        assert assign_sync(s, ACCOUNT, 12)["status"] in {"awaiting_metadata", "already_categorised"}
        assert snapshot(s, 12) == state


def reviewed_category(store, *, confidence=99):
    with Session(store.engine) as s, s.begin():
        analyse(s, confidence=confidence)
        fid = operator_folder(s, 11, "Scenic photos")
    current = get_category_price(store, TENANT, ACCOUNT, fid)
    set_category_price(store, TENANT, ACCOUNT, fid, price_cents=1200, expected_fingerprint=current["fingerprint"],
        command_key="0" * 64, actor=ACTOR)
    review = get_category_review(store, TENANT, ACCOUNT, fid, ["11"])
    category = {"folderId": fid, "priceCents": 1200, "categoryPriceRevision": 1, "classificationConfirmed": True,
        "rightsConfirmed": True, "reviewedState": review, "products": [
            {"label": "Scenic one", "description": "A reviewed photograph.", "mediaIds": ["11"], "previewMediaIds": []}]}
    return fid, category


def test_stale_reviewed_category_is_refused_after_classification_changes(store):
    fid, category = reviewed_category(store)
    assert get_category_review(store, TENANT, ACCOUNT, fid, ["11"], include_assets=True)["assets"][0]["classificationUncertain"] is False
    with Session(store.engine) as s, s.begin():
        analyse(s, confidence=20)
    assert get_category_review(store, TENANT, ACCOUNT, fid, ["11"], include_assets=True)["assets"][0]["classificationUncertain"] is True
    with pytest.raises(Refused, match="reviewed category or asset changed"):
        create_category_drafts(store, TENANT, ACCOUNT, category=category, command_key="b" * 64, actor=ACTOR)
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(CatalogItem)) == 0


def test_saved_reviewed_draft_is_refused_when_classification_later_changes(store):
    fid, category = reviewed_category(store)
    created = create_category_drafts(store, TENANT, ACCOUNT, category=category, command_key="b" * 64, actor=ACTOR)
    item_id = int(created["items"][0]["itemId"])
    from db.onlyfans_models import OfRuntime
    def check():
        with Session(store.engine) as s, s.begin():
            assert_category_review(s, s.get(OfRuntime, ACCOUNT), s.get(CatalogItem, item_id))
    check()
    with Session(store.engine) as s, s.begin():
        analyse(s, confidence=20)
    with pytest.raises(Refused, match="reviewed category or asset changed"):
        check()
    with Session(store.engine) as s:
        item = s.get(CatalogItem, item_id)
        assert item.enabled is False and item.price_cents == 1200


def test_redescribe_path_ends_with_refreshed_classification(database, monkeypatch):
    import test_automatic_vault_descriptions_pg as auto
    from types import SimpleNamespace
    args, calls, engine, _ = auto.setup(database, monkeypatch)
    import llm_client
    answers = {"confidence": 20}
    async def chat(**kwargs):
        return SimpleNamespace(content=json.dumps({"description": "Synthetic outdoors portrait", "tags": ["outdoors"],
            "explicitness": "sfw", "confidence": answers["confidence"]}), call_id=None, cost_cents=1)
    monkeypatch.setattr(llm_client, "chat", chat)
    mid = auto.MID
    with Session(database[1]) as s, s.begin():
        item = s.get(VaultItem, (auto.ACCOUNT, mid))
        item.content_hash = "a" * 64; item.content_hash_kind = "thumb/test"
        item.ai_fields_json = json.dumps({"_commercial_classification_v1": {"status": "proposed", "reason": "metadata_evidence",
            "content_stamp": "thumb/test:" + "a" * 64, "categories": ["outdoors"], "sale_approved": False},
            "_auto_folders_v1": True, "_auto_folders_v2": True})
        folder = VaultFolder(account_id=auto.ACCOUNT, name="Outdoors", created_by=OWNER_PREFIX + "outdoors")
        s.add(folder); s.flush()
        s.add(VaultFolderItem(account_id=auto.ACCOUNT, folder_id=folder.id, media_id=mid))
    def state():
        with Session(database[1]) as s:
            item = s.get(VaultItem, (auto.ACCOUNT, mid))
            fields = json.loads(item.ai_fields_json)
            names = sorted(f.created_by for f in s.scalars(select(VaultFolder).join(VaultFolderItem,
                (VaultFolderItem.account_id == VaultFolder.account_id) & (VaultFolderItem.folder_id == VaultFolder.id))
                .where(VaultFolderItem.account_id == auto.ACCOUNT, VaultFolderItem.media_id == mid)))
            return fields["_commercial_classification_v1"], names
    async def run():
        try:
            assert (await auto.child.run(args))["described"] == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
    classification, names = state()
    assert classification["status"] == "uncertain" and classification["reason"] == "low_confidence"
    assert classification["sale_approved"] is False and MISC in names
    # a later, better re-description ends proposed and withdraws only the automatic Miscellaneous filing
    answers["confidence"] = 95
    with Session(database[1]) as s, s.begin():
        item = s.get(VaultItem, (auto.ACCOUNT, mid))
        item.description = None; item.describe_status = None; item.describe_generated_at = None
        item.content_hash = "b" * 64    # content changed, so a fresh description is owed
    asyncio.run(run())
    classification, names = state()
    assert classification["status"] == "proposed" and classification["reason"] == "metadata_evidence"
    assert classification["sale_approved"] is False and MISC not in names
    assert OWNER_PREFIX + "outdoors" in names


def test_legacy_v1_and_precommercial_items_do_not_get_removed_filings_back(store):
    with Session(store.engine) as s, s.begin():
        # genuine V1 item: casual/fitness were filed by V1 and an operator may have removed them
        item = s.get(VaultItem, (ACCOUNT, 11))
        fields = json.loads(item.ai_fields_json)
        fields.pop("_auto_folder_pending_v1", None)
        fields.update({"_auto_folders_v1": True, "description": "A casual outdoor photograph.", "confidence": 20,
                       "_content_stamp": stamp_of(item)})
        item.ai_fields_json = json.dumps(fields)
        assign_sync(s, ACCOUNT, 11)
        assert cls(s)["status"] == "uncertain"
        analyse(s, confidence=95)
        assert cls(s)["status"] == "proposed" and "casual" in cls(s)["categories"]
        assert auto_keys(s) == ["outdoors"]
        # categorised before commercial mode (v2 marker, no classification): classify, add nothing
        item = s.get(VaultItem, (ACCOUNT, 12))
        item.ai_fields_json = json.dumps({"_auto_folders_v1": True, "_auto_folders_v2": True, "description": TEXT,
                                          "confidence": 99, "_content_stamp": stamp_of(item)})
        s.flush()
        before = members(s, 12)
        assign_sync(s, ACCOUNT, 12, commercial=True)
        assert cls(s, 12)["status"] == "proposed"
        assert [m for m in members(s, 12) if m[0] != MISC] == [m for m in before if m[0] != MISC]
        assert auto_keys(s, 12) == [k for k in auto_keys(s, 12) if k != "miscellaneous"]
