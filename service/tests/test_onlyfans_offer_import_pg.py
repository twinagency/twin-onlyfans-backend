"""Owned PostgreSQL offer import: inventory, refresh, exact drafts and evidence.

Synthetic accounts, vault pages and history rows only. Fake history readers
are injected locally; no credential, provider request, send or model call.
"""
import json
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import ACTOR, TENANT
from test_onlyfans_vault_pg import media, sync
from db.models import CatalogItem, Message
from db.onlyfans_models import OfIngress, OfRuntime
from db.onlyfans_offer_import_models import (OfOfferImportCandidate, OfOfferImportCursor,
    OfOfferImportDecision, OfOfferImportJob, OfOfferObservation)
from onlyfans_runtime import offer_import
from onlyfans_runtime.native_protocol import message
from onlyfans_runtime.offer_import import (InvalidImport, create_drafts, create_draft, get_candidate,
    get_command, list_candidates, process_inventory_jobs, request_job, cancel_job, withdraw_draft, provenance)
from onlyfans_runtime.offer_import_worker import process_refresh_jobs
from onlyfans_runtime.offer_terms import from_history, media_key
from onlyfans_runtime.store import Refused
from onlyfans_runtime.worker import MessagePage
from test_onlyfans_provider_adapters import OWNER

VAULT = [media(11), media(12), media(13, "video"), media(14, "video"), media(15), media(16, "audio")]


def key(n):
    return f"{n:064x}"


def history(store, account, rows):
    with Session(store.engine) as s, s.begin():
        for fan, mid, ids, price, direction in rows:
            s.add(Message(account_id=account, fan_id=fan, message_id=mid, direction=direction, body="synthetic",
                media_ids=json.dumps(ids), media_count=len(ids), price_cents=price,
                created_at=datetime(2026, 9, 1, 12, 0, 0)))


@pytest.fixture
def ready(database, monkeypatch):
    store, account, job, _ = sync(database, monkeypatch, rows=VAULT)
    history(store, account, [(501, 1001, [11, 12], 1999, "out"), (501, 1002, [12, 11], 1999, "out"),
                      (502, 1003, [11, 12], 2500, "out"), (502, 1004, [13, 14], 1999, "out"),
                      (503, 1005, [11], 900, "in"), (503, 1006, [15], 0, "out")])
    yield store, account
    store.close()


def raw(mid, ids, price, *, previews=..., locked=..., queue=False, owner=OWNER, fan=501):
    row = {"id": mid, "fromUser": {"id": owner}, "createdAt": "2026-09-01T12:00:00+00:00", "text": "<p>synthetic</p>",
           "media": [{"id": i} for i in ids], "mediaCount": len(ids), "price": f"{price // 100}.{price % 100:02d}",
           "responseType": "message", "isFromQueue": queue}
    if previews is not ...:
        row["previews"] = previews
    if locked is not ...:
        row["lockedText"] = locked
    return row


class FakeHistory:
    """Account-bound reader built on the real strict parser and offer capture."""
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def messages(self, *, fan_id, before_id, limit):
        self.calls.append((fan_id, before_id))
        rows, more = self.pages[(fan_id, before_id)]
        events = [message(r, owner_id=OWNER, fan_id=fan_id, history=True) for r in rows]
        observations = [o for o in (from_history(r, e) for r, e in zip(rows, events)) if o is not None]
        return MessagePage(events, more, observations)


def factory(reader):
    return lambda connection, tenant, account: reader


def inventory(store, account, n=1):
    job = request_job(store, TENANT, account, mode="inventory", command_key=key(n), actor=ACTOR)
    while process_inventory_jobs(store, TENANT):
        pass
    return job


def refresh(store, account, sources, pages, n=2):
    job = request_job(store, TENANT, account, mode="refresh", command_key=key(n), actor=ACTOR,
                      sources=[{"fanId": str(f), "messageId": str(m)} for f, m in sources])
    reader = FakeHistory(pages)
    while process_refresh_jobs(store, TENANT, transport_factory=factory(reader)):
        with Session(store.engine) as s, s.begin():
            s.execute(text("UPDATE of_offer_import_job SET next_attempt_at=NULL"))
    return job, reader


def observation(store, account, source_id):
    with Session(store.engine) as s:
        return s.scalar(select(OfOfferObservation).where(OfOfferObservation.account_id == account,
                                                          OfOfferObservation.source_id == source_id))


def draft(obs, n, **patch):
    return {"commandKey": key(n), "observationId": obs.observation_id, "expectedFingerprint": obs.fingerprint,
            "label": "Two photos", "description": "Two approved photographs from the shoot.",
            "reuseConfirmed": True, "descriptionApproved": True, **patch}


def test_inventory_groups_unordered_sets_and_keeps_every_variant(ready):
    store, account = ready
    job = inventory(store, account)
    page = list_candidates(store, TENANT, account, job["jobId"])
    assert page["job"]["status"] == "complete" and page["job"]["coverage"] == "imported_history"
    assert page["job"]["rowsScanned"] == 4, "incoming and unpriced rows are not creator offers"
    by_key = {c["candidateId"]: c for c in page["items"]}
    pair = by_key[media_key([11, 12])]
    assert pair["sourceCount"] == 3 and pair["observedPrices"] == [1999, 2500] and pair["variantCount"] == 3
    assert pair["status"] == "source_details_needed", "stored history alone never certifies previews"
    detail = get_candidate(store, TENANT, account, job["jobId"], pair["candidateId"])
    orders = sorted(v["mediaIds"] for v in detail["variants"])
    assert orders == [["11", "12"], ["11", "12"], ["12", "11"]], "attachment order differences stay visible"
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(OfIngress)) == 0
        assert s.scalar(select(func.count()).select_from(CatalogItem)) == 0
        assert s.get(OfRuntime, account).enabled is False


def test_inventory_batches_commit_progress_without_double_counting(ready, monkeypatch):
    store, account = ready
    monkeypatch.setattr(offer_import, "BATCH_ROWS", 1)
    job = inventory(store, account)
    with Session(store.engine) as s:
        row = s.get(OfOfferImportJob, (account, job["jobId"]))
        assert row.status == "complete" and row.rows_scanned == 4 and row.pages_read == 5
        counts = sum(c.source_count for c in s.scalars(select(OfOfferImportCandidate)))
        assert counts == 4
    assert request_job(store, TENANT, account, mode="inventory", command_key=key(1), actor=ACTOR)["jobId"] == job["jobId"]
    with pytest.raises(Refused, match="idempotency"):
        request_job(store, TENANT, account, mode="refresh", command_key=key(1), actor=ACTOR,
                    sources=[{"fanId": "501", "messageId": "1001"}])


def test_refresh_captures_exact_terms_without_reply_work_or_new_messages(ready):
    store, account = ready
    pages = {(501, None): ([raw(1002, [12, 11], 1999, previews=[11], locked=False),
                            raw(1001, [11, 12], 1999, previews=[11], locked=False)], False)}
    job, reader = refresh(store, account, [(501, 1001)], pages)
    assert reader.calls == [(501, None)]
    with Session(store.engine) as s:
        row = s.get(OfOfferImportJob, (account, job["jobId"]))
        assert row.status == "complete" and row.targets_done == 1
        cursor = s.get(OfOfferImportCursor, (account, job["jobId"], 501))
        assert cursor.status == "complete" and json.loads(cursor.found_json) == [1001]
        assert s.scalar(select(func.count()).select_from(OfIngress)) == 0, "refresh records evidence, not inbox work"
    obs = observation(store, account, 1001)
    assert json.loads(obs.media_json) == [11, 12] and json.loads(obs.preview_json) == [11]
    assert obs.locked_text is False and obs.price_cents == 1999 and obs.source_context == "direct"
    other = observation(store, account, 1002)
    assert json.loads(other.media_json) == [12, 11], "order is source evidence"


def test_refresh_unresolved_when_target_is_passed_or_budget_spent(ready, monkeypatch):
    store, account = ready
    pages = {(502, None): ([raw(1004, [13, 14], 1999, previews=[], locked=False, fan=502)], True),
             (502, 1004): ([raw(900, [11], 500, previews=[], locked=False, fan=502)], True)}
    job, _ = refresh(store, account, [(502, 1003)], pages)
    with Session(store.engine) as s:
        cursor = s.get(OfOfferImportCursor, (account, job["jobId"], 502))
        assert cursor.status == "unresolved" and cursor.outcome == "source_not_found"
        assert s.get(OfOfferImportJob, (account, job["jobId"])).status == "partial"
    assert observation(store, account, 1003) is None, "never inferred from a sibling source"


def test_unknown_previews_and_lock_stay_blocked(ready):
    store, account = ready
    pages = {(501, None): ([raw(1001, [11, 12], 1999)], False)}
    refresh(store, account, [(501, 1001)], pages)
    obs = observation(store, account, 1001)
    assert obs.preview_json is None and obs.locked_text is None
    assert json.loads(obs.reasons_json) == ["locked_text_unknown", "previews_unknown"]
    result = create_drafts(store, TENANT, account, drafts=[draft(obs, 10)], actor=ACTOR)
    assert result[0]["ok"] is False and "cannot become a draft" in result[0]["error"]


def test_exact_bundle_becomes_one_disabled_fixed_draft(ready):
    store, account = ready
    pages = {(502, None): ([raw(1004, [13, 14], 1999, previews=[], locked=False, fan=502)], False)}
    refresh(store, account, [(502, 1004)], pages)
    obs = observation(store, account, 1004)
    first = create_draft(store, TENANT, account, draft=draft(obs, 20), actor=ACTOR)
    assert first["priceCents"] == 1999
    with Session(store.engine) as s:
        item = s.get(CatalogItem, int(first["itemId"]))
        assert (item.enabled, item.pricing_mode, item.price_cents, item.kind, item.duration_sec) == (False, "fixed", 1999, "video", None)
        assert json.loads(item.media_ids) == [13, 14] and json.loads(item.preview_media_ids) == []
        assert s.scalar(select(func.count()).select_from(CatalogItem)) == 1
    assert create_draft(store, TENANT, account, draft=draft(obs, 20), actor=ACTOR) == first
    with pytest.raises(Refused, match="idempotency"):
        create_draft(store, TENANT, account, draft=draft(obs, 20, label="Changed"), actor=ACTOR)
    with pytest.raises(Refused, match="already present"):
        create_draft(store, TENANT, account, draft=draft(obs, 21), actor=ACTOR)
    assert get_command(store, TENANT, account, key(20))["itemId"] == first["itemId"]
    assert get_command(store, TENANT, account, key(99)) == {"commandKey": key(99), "recorded": False}
    with Session(store.engine) as s:
        source = provenance(s, account, int(first["itemId"]))
        assert source["sourcePriceCents"] == 1999 and source["previewIds"] == [] and not source["withdrawn"]


def test_mixed_bundle_keeps_order_and_photo_preview(ready):
    store, account = ready
    history(store, account, [(504, 1010, [15, 13, 11], 2499, "out")])
    pages = {(504, None): ([raw(1010, [15, 13, 11], 2499, previews=[15], locked=False, fan=504)], False)}
    refresh(store, account, [(504, 1010)], pages)
    result = create_draft(store, TENANT, account, draft=draft(observation(store, account, 1010), 30), actor=ACTOR)
    with Session(store.engine) as s:
        item = s.get(CatalogItem, int(result["itemId"]))
        assert json.loads(item.media_ids) == [15, 13, 11] and json.loads(item.preview_media_ids) == [15]
        assert item.kind == "video" and item.price_cents == 2499


@pytest.mark.parametrize("ids,previews,locked,price,reason", [
    ([13, 11], [13], False, 1999, "Only included photos"),
    ([11, 12], [11], True, 1999, "cannot become a draft"),
    ([11, 12], [11], False, 100, "cannot become a draft"),
    ([11, 16], [], False, 1999, "not available"),
    ([11, 12], [11, 12], False, 1999, "cannot become a draft"),
])
def test_unsupported_shapes_create_nothing(ready, ids, previews, locked, price, reason):
    store, account = ready
    history(store, account, [(505, 1020, ids, price, "out")])
    pages = {(505, None): ([raw(1020, ids, price, previews=previews, locked=locked, fan=505)], False)}
    refresh(store, account, [(505, 1020)], pages)
    obs = observation(store, account, 1020)
    result = create_drafts(store, TENANT, account, drafts=[draft(obs, 40)], actor=ACTOR)
    assert result[0]["ok"] is False and reason in result[0]["error"]
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(CatalogItem)) == 0
        assert s.scalar(select(func.count()).select_from(OfOfferObservation)) == 1, "evidence is retained"


def test_stale_review_and_forged_fields_refused(ready):
    store, account = ready
    pages = {(501, None): ([raw(1001, [11, 12], 1999, previews=[11], locked=False)], False)}
    refresh(store, account, [(501, 1001)], pages)
    obs = observation(store, account, 1001)
    result = create_drafts(store, TENANT, account, drafts=[draft(obs, 50, expectedFingerprint="0" * 64)], actor=ACTOR)
    assert result[0]["ok"] is False and "changed after review" in result[0]["error"]
    for patch in [{"priceCents": 1}, {"reuseConfirmed": False}, {"descriptionApproved": False}, {"description": ""}]:
        with pytest.raises(InvalidImport):
            create_drafts(store, TENANT, account, drafts=[draft(obs, 51, **patch)], actor=ACTOR)
    with pytest.raises(Refused):
        create_draft(store, "other-tenant", account, draft=draft(obs, 52), actor=ACTOR)
    with pytest.raises(InvalidImport):
        create_drafts(store, TENANT, account, drafts=[draft(obs, 60 + i) for i in range(21)], actor=ACTOR)


def test_concurrent_commands_for_one_bundle_create_exactly_one_item(ready):
    store, account = ready
    pages = {(501, None): ([raw(1001, [11, 12], 1999, previews=[11], locked=False)], False)}
    refresh(store, account, [(501, 1001)], pages)
    obs = observation(store, account, 1001)
    barrier = threading.Barrier(4)
    def attempt(n):
        barrier.wait()
        return create_drafts(store, TENANT, account, drafts=[draft(obs, 70 + n)], actor=ACTOR)[0]
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(r["ok"] for r in results) == 1
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(CatalogItem)) == 1
        assert s.scalar(select(func.count()).select_from(OfOfferImportDecision)) == 1


def test_evidence_is_append_only_and_withdraw_is_audited(ready, database):
    store, account = ready
    pages = {(501, None): ([raw(1001, [11, 12], 1999, previews=[11], locked=False)], False)}
    refresh(store, account, [(501, 1001)], pages)
    obs = observation(store, account, 1001)
    item = create_draft(store, TENANT, account, draft=draft(obs, 80), actor=ACTOR)["itemId"]
    for statement in ["UPDATE of_offer_observation SET price_cents=1", "DELETE FROM of_offer_observation",
                      "UPDATE of_offer_import_decision SET actor='x'", "DELETE FROM of_offer_import_decision",
                      "UPDATE of_offer_import_job SET request_hash='x'", "DELETE FROM of_offer_import_job"]:
        with pytest.raises(DBAPIError), database[1].begin() as c:
            c.exec_driver_sql(statement)
    assert withdraw_draft(store, TENANT, account, int(item), command_key=key(81), actor=ACTOR)["withdrawn"] is True
    assert withdraw_draft(store, TENANT, account, int(item), command_key=key(81), actor=ACTOR)["withdrawn"] is True
    with pytest.raises(Refused):
        withdraw_draft(store, TENANT, account, int(item), command_key=key(82), actor=ACTOR)
    with Session(store.engine) as s:
        assert s.get(CatalogItem, int(item)) is not None, "withdrawal never deletes"
        assert provenance(s, account, int(item))["withdrawn"] is True


def test_cancel_is_durable_and_existing_items_stay_dynamic(ready):
    store, account = ready
    job = request_job(store, TENANT, account, mode="inventory", command_key=key(90), actor=ACTOR)
    cancelled = cancel_job(store, TENANT, account, job["jobId"], expected_revision=1, actor=ACTOR)
    assert cancelled["status"] == "cancelled"
    assert process_inventory_jobs(store, TENANT) == 0
    with Session(store.engine) as s, s.begin():
        s.add(CatalogItem(account_id=account, kind="image", media_ids="[11]", price_cents=500))
    with Session(store.engine) as s:
        assert s.scalar(select(CatalogItem.pricing_mode)) == "dynamic"


def test_inventory_scales_past_the_audited_history_volume(ready):
    """T13: 330,000 synthetic priced rows (audit saw 325,072), bounded batches."""
    store, account = ready
    with store.engine.begin() as c:
        c.execute(text("""INSERT INTO messages(account_id, fan_id, message_id, direction, sender_name, body, media_ids,
              media_count, price_cents, is_tip, is_unsent, is_promise, created_at)
            SELECT :account, 10000 + (n / 100), 100000 + n, 'out', '', '',
                   '[' || (n % 160 + 1) || ',' || (n % 160 + 1000) || ']', 2, 300 + (n % 7) * 100,
                   false, false, false, now()
            FROM generate_series(1, 330000) AS n"""), {"account": account})
    import time
    started = time.monotonic()
    job = inventory(store, account, 777)
    elapsed = time.monotonic() - started
    with Session(store.engine) as s:
        row = s.get(OfOfferImportJob, (account, job["jobId"]))
        assert row.status == "complete" and row.rows_scanned == 330004
        assert row.pages_read == 331, "1,000-row committed batches"
        total = s.scalar(select(func.sum(OfOfferImportCandidate.source_count)).where(OfOfferImportCandidate.job_id == job["jobId"]))
        assert total == 330004, "no source counted twice"
        groups = s.scalar(select(func.count()).select_from(OfOfferImportCandidate).where(OfOfferImportCandidate.job_id == job["jobId"]))
        assert groups == 162
    print(f"inventory scanned 330,004 rows in {elapsed:.1f}s")
