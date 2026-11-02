"""Commercial mapping recovery: dedupe across runs, variants kept, nothing invented.

Synthetic accounts and history only; no provider, credential, send or model call.
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_connections_pg import ACTOR, TENANT
from test_onlyfans_offer_import_pg import (ready, key, history, inventory, refresh, observation, draft, raw)  # noqa: F401
from db.models import CatalogItem
from db.onlyfans_offer_import_models import OfOfferImportCandidate, OfOfferImportDecision
from onlyfans_runtime import offer_import
from onlyfans_runtime.offer_import import (GRAPHIC_REASON, create_draft, distinct_combinations,
    documented_price_floor, recover_mappings, sequence_status)
from onlyfans_runtime.offer_terms import media_key


def by_key(report):
    return {i["candidateId"]: i for i in report["items"]}


def counts(store):
    with Session(store.engine) as s:
        return (s.scalar(select(func.count()).select_from(OfOfferImportDecision)),
                s.scalar(select(func.count()).select_from(CatalogItem)))


def test_two_import_runs_do_not_double_count(ready):
    store, account = ready
    inventory(store, account, 1)
    inventory(store, account, 2)
    with Session(store.engine) as s:
        per_run_rows = s.scalar(select(func.sum(OfOfferImportCandidate.source_count)))
    assert per_run_rows == 8, "the same 4 saved rows are folded once per run"
    report = recover_mappings(store, TENANT, account)
    assert report["distinctCombinations"] == 2
    pair = by_key(report)[media_key([11, 12])]
    assert len(pair["runs"]) == 2 and sum(v["count"] for v in pair["variants"]) == 3, "counts are not summed across runs"


def test_price_and_order_variants_are_retained_not_chosen(ready):
    store, account = ready
    inventory(store, account, 1)
    inventory(store, account, 2)
    pair = by_key(recover_mappings(store, TENANT, account))[media_key([11, 12])]
    assert sorted((v["priceCents"], v["mediaIds"]) for v in pair["variants"]) == [
        (1999, ["11", "12"]), (1999, ["12", "11"]), (2500, ["11", "12"])]
    assert pair["status"] == "unresolved" and pair["reasons"] == ["variant_conflict"]
    assert "proposedBinding" not in pair and "observationId" not in pair


def test_unambiguous_existing_item_maps_automatically_without_writes(ready):
    store, account = ready
    pages = {(502, None): ([raw(1004, [13, 14], 1999, previews=[], locked=False, fan=502)], False)}
    refresh(store, account, [(502, 1004)], pages)
    item = create_draft(store, TENANT, account, draft=draft(observation(store, account, 1004), 20), actor=ACTOR)
    inventory(store, account, 1)
    before = counts(store)
    report = recover_mappings(store, TENANT, account)
    video = by_key(report)[media_key([13, 14])]
    assert video["status"] == "mapped_existing"
    assert video["proposedBinding"] == {"itemId": int(item["itemId"]), "priceCents": 1999, "enabled": False}
    assert counts(store) == before, "recovery is read-only"
    assert "catalogue:" + media_key([13, 14])[:16] not in {u["key"] for u in report["unresolved"]}


def test_existing_item_with_other_terms_is_unresolved_not_overwritten(ready):
    store, account = ready
    pages = {(502, None): ([raw(1004, [13, 14], 1999, previews=[], locked=False, fan=502)], False)}
    refresh(store, account, [(502, 1004)], pages)
    item = create_draft(store, TENANT, account, draft=draft(observation(store, account, 1004), 20), actor=ACTOR)
    with Session(store.engine) as s, s.begin():
        s.get(CatalogItem, int(item["itemId"])).price_cents = 2999
    inventory(store, account, 1)
    video = by_key(recover_mappings(store, TENANT, account))[media_key([13, 14])]
    assert video["status"] == "unresolved" and video["reasons"] == ["existing_item_terms_differ"]
    with Session(store.engine) as s:
        assert s.get(CatalogItem, int(item["itemId"])).price_cents == 2999


def test_single_variant_needs_verified_source_then_awaits_human_approval(ready):
    store, account = ready
    inventory(store, account, 1)
    video = by_key(recover_mappings(store, TENANT, account))[media_key([13, 14])]
    assert video["status"] == "unresolved" and video["reasons"] == ["source_details_needed"], \
        "stored history alone never certifies previews"
    pages = {(502, None): ([raw(1004, [13, 14], 1999, previews=[], locked=False, fan=502)], False)}
    refresh(store, account, [(502, 1004)], pages)
    report = recover_mappings(store, TENANT, account)
    video = by_key(report)[media_key([13, 14])]
    assert video["status"] == "awaiting_approval" and video["reasons"] == ["approval_required"]
    assert video["observationId"] == observation(store, account, 1004).observation_id
    assert counts(store) == (0, 0), "no approval flag, draft or item is fabricated"
    assert report["approvalsCreated"] == 0 and report["pricesInvented"] == 0


def test_unavailable_asset_blocks_the_offer(ready):
    store, account = ready
    history(store, account, [(504, 1010, [11, 9999], 1999, "out")])
    inventory(store, account, 1)
    blocked = by_key(recover_mappings(store, TENANT, account))[media_key([11, 9999])]
    assert blocked["status"] == "blocked" and "asset_missing" in blocked["reasons"]
    assert "proposedBinding" not in blocked and "observationId" not in blocked


def test_price_below_documented_creator_floor_is_not_accepted(ready):
    store, account = ready
    history(store, account, [(505, 1011, [15], 1500, "out")])
    inventory(store, account, 1)
    floor = documented_price_floor("amelia")
    assert floor and floor > 1500
    report = recover_mappings(store, TENANT, account, creator_key="amelia")
    assert report["priceFloorCents"] == floor
    low = by_key(report)[media_key([15])]
    assert low["status"] == "unresolved" and low["reasons"] == ["historical_price_below_creator_floor"]
    assert recover_mappings(store, TENANT, account)["priceFloorCents"] is None


def test_category_floors_are_reported_unresolved_not_guessed(ready):
    store, account = ready
    inventory(store, account, 1)
    unresolved = {u["key"]: u["reason"] for u in recover_mappings(store, TENANT, account)["unresolved"]}
    assert "category" in unresolved["catalogue:category"]


def test_vip_graphics_are_not_a_sequence(ready):
    store, account = ready
    graphic = {"kind": "vip_graphic", "name": "Invite Only Stripping", "steps": [
        {"step": 1, "mediaIds": [11], "priceCents": 1999}]}
    status = sequence_status([graphic])
    assert status["sequences"] == {} and status["rejected"] == [{"kind": "vip_graphic", "reason": GRAPHIC_REASON}]
    inventory(store, account, 1)
    report = recover_mappings(store, TENANT, account, sequence_evidence=[graphic])
    assert report["sequences"] == {}
    assert any(u["key"] == "sequence:vault" and "graphics" in u["reason"] for u in report["unresolved"])
    # Ordinary combinations are still recovered independently of the VIP mapping.
    assert report["distinctCombinations"] == 2


def test_only_a_complete_authoritative_export_forms_a_sequence():
    good = {"kind": "moxy_script_export", "name": "vip", "steps": [
        {"step": 2, "mediaIds": [12], "priceCents": 2500}, {"step": 1, "mediaIds": [11], "priceCents": 1999}]}
    gap = {"kind": "vault_sequence_export", "name": "gap", "steps": [{"step": 2, "mediaIds": [11], "priceCents": 1999}]}
    unpriced = {"kind": "moxy_script_export", "name": "free", "steps": [{"step": 1, "mediaIds": [11], "priceCents": 0}]}
    status = sequence_status([good, gap, unpriced, {"kind": "screenshot"}])
    assert list(status["sequences"]) == ["vip"] and [s["step"] for s in status["sequences"]["vip"]] == [1, 2]
    assert [r["reason"] for r in status["rejected"]] == ["incomplete_sequence_steps"] * 2 + ["not_an_authoritative_sequence_source"]
    assert status["unresolved"] == []
