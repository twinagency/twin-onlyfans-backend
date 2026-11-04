"""Synthetic, local-only selection regression. No provider or model calls."""
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.models import VaultItem
from native_pilot.tools.describe_imported_vault import select_batch
from onlyfans_runtime.store import utcnow


def test_mixed_batch_includes_retry_video_and_respects_cooldown_and_scope():
    engine = create_engine("sqlite:///:memory:")
    VaultItem.__table__.create(engine)
    now = utcnow()
    with Session(engine) as session, session.begin():
        for mid in range(100, 104):
            session.add(VaultItem(account_id="of-900001", media_id=mid, kind="photo", created_at=now))
        session.add(VaultItem(account_id="of-900001", media_id=10, kind="video", created_at=now,
            describe_status="fetch_failed", describe_generated_at=now-timedelta(minutes=31)))
        session.add(VaultItem(account_id="of-900001", media_id=20, kind="video", created_at=now))
        session.add(VaultItem(account_id="of-900001", media_id=5, kind="video", created_at=now,
            describe_status="fetch_failed", describe_generated_at=now))
        session.add(VaultItem(account_id="of-900002", media_id=1, kind="video", created_at=now))
    with Session(engine) as session, session.begin():
        assert select_batch(session, "of-900001", 4) == [100, 101, 102, 10]
        assert select_batch(session, "of-900001", 1) == [10]
        item = session.get(VaultItem, ("of-900001", 10))
        item.describe_status = "described"
        item.video_description = "Synthetic complete description"
        item.ai_fields_json = '{"_folder_evidence_v1": {}}'
        session.flush()
        assert select_batch(session, "of-900001", 4) == [100, 101, 102, 20]
        session.get(VaultItem, ("of-900001", 20)).describe_status = "refused"
        session.flush()
        assert select_batch(session, "of-900001", 4) == [100, 101, 102, 103]
    engine.dispose()


if __name__ == "__main__":
    test_mixed_batch_includes_retry_video_and_respects_cooldown_and_scope()
    print("PASS mixed batch, eligible retries, cooldown, account scope, and full photo batch")
