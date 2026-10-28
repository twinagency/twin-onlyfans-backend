"""Focused integration regressions with synthetic SQLite rows and no provider I/O.

These isolate projection and route scoping. PostgreSQL acceptance still proves
the migrated schema, triggers and real delivery lifecycle.
"""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_offer_import_models import OfContentSyncDraft as Draft
from onlyfans_commands import launch_api
from onlyfans_runtime import content_sync_media
from onlyfans_runtime.launch_engagement.models import Action
from onlyfans_runtime.launch_engagement.service import EngagementService
from onlyfans_runtime.store import Refused

NOW = datetime(2026, 10, 23, 12)


@pytest.fixture
def engine():
    value = create_engine("sqlite://", poolclass=StaticPool,
                          connect_args={"check_same_thread": False})
    Action.__table__.create(value)
    yield value
    value.dispose()


def action(*, binding="current", fan=101, linked=True):
    return Action(account_id="acc-a", action_id=str(uuid4()), binding_id=binding,
        fan_id=fan, kind="follow_up", anchor_message_id=1, command_key=uuid4().hex,
        request_hash="a" * 64, snapshot_json="{}", snapshot_hash="b" * 64,
        status="queued" if linked else "scheduled", due_at=NOW,
        expires_at=NOW + timedelta(hours=1), created_at=NOW, updated_at=NOW,
        intent_id=str(uuid4()) if linked else None)


@pytest.mark.parametrize("operation", ["refresh", "cancel"])
def test_refused_intent_becomes_terminal_action_without_constraint_error(engine, operation):
    row = action()
    identity = row.action_id
    with Session(engine) as s, s.begin():
        s.add(row)
    intent = SimpleNamespace(status="refused", reason="cap_exhausted")
    lookup = SimpleNamespace(get=lambda model, key: intent if model is OfTextIntent else None)
    with Session(engine) as s, s.begin():
        row = s.get(Action, ("acc-a", identity))
        if operation == "refresh":
            EngagementService._refresh(object(), lookup, None, row, NOW)
        else:
            EngagementService._cancel(lookup, row, NOW, "operator", "operator_cancelled")
        assert row.status == "cancelled"
        s.flush()
    with Session(engine) as s:
        saved = s.get(Action, ("acc-a", identity))
        assert saved.status == "cancelled"
        assert saved.intent_id is not None
        if operation == "refresh":
            assert saved.reason == "cap_exhausted"
        else:
            assert saved.cancel_requested_at == NOW


def store_for(engine):
    return SimpleNamespace(engine=engine, _runtime=lambda *args: SimpleNamespace(
        account_id="acc-a", binding_id="current"))


def test_actions_list_excludes_retained_previous_binding_rows(engine, monkeypatch):
    current, old = action(), action(binding="previous-creator", fan=202)
    current_id = current.action_id
    with Session(engine) as s, s.begin():
        s.add_all([current, old])
    monkeypatch.setattr(launch_api, "authenticate", lambda *args: (store_for(engine), "tenant-a"))
    result = asyncio.run(launch_api.engagement_actions(object(), "acc-a"))
    payload = json.loads(result.body)
    assert [x["actionId"] for x in payload["actions"]] == [current_id]


def test_post_action_projection_rechecks_binding_after_prior_transaction(engine):
    old = action(binding="previous-creator")
    identity = old.action_id
    with Session(engine) as s, s.begin():
        s.add(old)
    with pytest.raises(Refused, match="engagement action unavailable"):
        launch_api.engagement_action_sync(store_for(engine), "tenant-a", "acc-a", identity)


def test_draft_pages_preserve_rows_with_the_same_creation_time(engine, monkeypatch):
    Draft.__table__.create(engine)
    identities = sorted([str(uuid4()) for _ in range(3)], reverse=True)
    with Session(engine) as s, s.begin():
        for identity in identities:
            s.add(Draft(account_id="acc-a", draft_id=identity, binding_id="current",
                command_key=uuid4().hex, request_hash="a" * 64, actor="operator",
                revision=1, label="Synthetic draft", description="Synthetic fixture",
                price_cents=1000, currency="USD", assets_json="[]", preview_json="[]",
                state="draft", created_at=NOW, updated_at=NOW))
    monkeypatch.setattr(content_sync_media, "draft_view", lambda s, row: {"draftId": row.draft_id})
    first = content_sync_media.list_drafts(store_for(engine), "tenant-a", "acc-a", after=None, limit=2)
    second = content_sync_media.list_drafts(store_for(engine), "tenant-a", "acc-a",
        after=first["nextCursor"], limit=2)
    assert first["nextCursor"] == identities[1]
    assert [x["draftId"] for x in first["items"] + second["items"]] == identities
    assert second["nextCursor"] is None
