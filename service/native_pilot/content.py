"""Explicit two-card pilot configuration; no import starts or enables anything.

This tests the existing native catalogue-offer path. It does not certify the
separate vault-wide ask resolver, which needs its own described/curated shelf.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from .ledger import Refused


class ContentPlan:
    def __init__(self, path, account, fan):
        self.raw = json.loads(Path(path).read_text(encoding="utf-8"))
        r = self.raw
        if r.get("account_id") != account or r.get("fan_id") != fan:
            raise Refused("content-plan-scope")
        items = r.get("items")
        if not isinstance(items, list) or len(items) != 2:
            raise Refused("content-plan-shape")
        self.items = {}
        for x in items:
            if (not isinstance(x, dict) or type(x.get("catalog_id")) is not int
                    or type(x.get("media_id")) is not int or x["media_id"] <= 0
                    or x.get("kind") not in ("photo", "video")
                    or type(x.get("price_cents")) is not int
                    or x["price_cents"] not in (0, 300)):
                raise Refused("content-plan-item")
            self.items[x["media_id"]] = x
        if len(self.items) != 2 or sorted(x["price_cents"] for x in items) != [0, 300]:
            raise Refused("content-plan-terms")
        self.free = next(x for x in items if x["price_cents"] == 0)
        self.paid = next(x for x in items if x["price_cents"] == 300)

    def terms(self, kwargs):
        if not kwargs:
            return None
        if not isinstance(kwargs, dict) or set(kwargs) - {"media_files", "price", "locked_text", "previews"}:
            raise Refused("content-send-fields")
        media = kwargs.get("media_files")
        if not isinstance(media, list) or len(media) != 1 or type(media[0]) is not int or media[0] not in self.items:
            raise Refused("content-send-media")
        x = self.items[media[0]]
        price = kwargs.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise Refused("content-send-price")
        if Decimal(str(price)) * 100 != x["price_cents"]:
            raise Refused("content-send-price")
        if kwargs.get("locked_text", False) is not False or kwargs.get("previews", []) != []:
            raise Refused("content-send-lock")
        return dict(x)

    def check_catalog(self, con, account):
        rows = con.execute("SELECT id,kind,media_ids,price_cents,is_free_teaser,enabled FROM catalog_items WHERE account_id=?", (account,)).fetchall()
        if {r["id"] for r in rows} != {x["catalog_id"] for x in self.items.values()}:
            raise Refused("content-catalog-scope")
        for r in rows:
            x = next(x for x in self.items.values() if x["catalog_id"] == r["id"])
            if (r["kind"] != x["kind"] or json.loads(r["media_ids"]) != [x["media_id"]]
                    or r["price_cents"] != x["price_cents"]
                    or bool(r["is_free_teaser"]) != (x["price_cents"] == 0)):
                raise Refused("content-catalog-drift")

    def evidence(self, raw, expected):
        if Decimal(str(raw.get("price", 0))) * 100 != expected["price_cents"]:
            raise Refused("content-receipt-price")
        media = raw.get("media")
        if (not isinstance(media, list) or len(media) != 1
                or str(media[0].get("id")) != str(expected["media_id"])
                or media[0].get("type") != expected["kind"]
                or raw.get("isTip") or raw.get("isUnsent") or raw.get("lockedText")):
            raise Refused("content-receipt-media")
        return {"media_id": expected["media_id"], "kind": expected["kind"], "price_cents": expected["price_cents"]}
