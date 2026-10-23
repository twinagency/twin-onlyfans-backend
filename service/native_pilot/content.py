"""Explicit single-card pilot configuration; no import starts or enables anything.

Exactly ONE reviewed paid item may be sent: a fixed US$3 card that carries its
full media set and its free preview subset, in the exact shape the native
catalogue path produces (``media_files`` = every media id of the item,
``previews`` = the item's preview ids, ``price`` 3, ``locked_text`` False).
Anything else - a different item, a missing/extra media or preview id, another
price, a locked text, an unknown field - is refused before any provider call.

The private manifest names the item; this module does not embed live ids:

    {"account_id": "of-<owner>", "fan_id": <fan>, "items": [
        {"catalog_id": N, "kind": "video", "price_cents": 300,
         "media": [{"id": <media id>, "type": "photo|video"}, ...],
         "preview_ids": [<subset of media ids>]}]}

This tests the existing native catalogue-offer path. It does not certify the
separate vault-wide ask resolver, which needs its own described/curated shelf.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from .ledger import Refused
from .scope import AccountScope

PRICE_CENTS = 300
MAX_MEDIA = 10
MEDIA_TYPES = ("photo", "video")


def _id(value):
    return type(value) is int and value > 0


class ContentPlan:
    def __init__(self, path, account, fan):
        self.raw = json.loads(Path(path).read_text(encoding="utf-8"))
        r = self.raw
        if not isinstance(r, dict) or not AccountScope.valid(r.get("account_id")) \
                or AccountScope.parse(r["account_id"]) != AccountScope.parse(account) or r.get("fan_id") != fan:
            raise Refused("content-plan-scope")
        items = r.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise Refused("content-plan-shape")
        x = items[0]
        if (not isinstance(x, dict) or set(x) != {"catalog_id", "kind", "price_cents", "media", "preview_ids"}
                or not _id(x["catalog_id"]) or x["kind"] not in MEDIA_TYPES
                or type(x["price_cents"]) is not int):
            raise Refused("content-plan-item")
        if x["price_cents"] != PRICE_CENTS:
            raise Refused("content-plan-terms")
        media, previews = x["media"], x["preview_ids"]
        if (not isinstance(media, list) or not 1 <= len(media) <= MAX_MEDIA
                or any(not isinstance(m, dict) or set(m) != {"id", "type"} or not _id(m["id"])
                       or m["type"] not in MEDIA_TYPES for m in media)):
            raise Refused("content-plan-media")
        ids = [m["id"] for m in media]
        if (len(set(ids)) != len(ids) or not isinstance(previews, list) or len(previews) > len(ids)
                or any(not _id(p) for p in previews) or len(set(previews)) != len(previews)
                or not set(previews) <= set(ids)):
            raise Refused("content-plan-previews")
        self.item = {"catalog_id": x["catalog_id"], "kind": x["kind"], "price_cents": PRICE_CENTS,
                     "media_ids": ids, "media_types": {str(m["id"]): m["type"] for m in media},
                     "preview_ids": list(previews)}
        # The runner's "card sent" stop condition keys off the paid price.
        self.paid = self.item

    @property
    def catalog_ids(self):
        return {self.item["catalog_id"]}

    def terms(self, kwargs):
        if not kwargs:
            return None
        if not isinstance(kwargs, dict) or set(kwargs) - {"media_files", "price", "locked_text", "previews"}:
            raise Refused("content-send-fields")
        x = self.item
        media = kwargs.get("media_files")
        if (not isinstance(media, list) or len(media) != len(x["media_ids"])
                or any(type(m) is not int for m in media) or media != x["media_ids"]):
            raise Refused("content-send-media")
        price = kwargs.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise Refused("content-send-price")
        if Decimal(str(price)) * 100 != x["price_cents"]:
            raise Refused("content-send-price")
        if kwargs.get("locked_text", False) is not False:
            raise Refused("content-send-lock")
        previews = kwargs.get("previews")
        if (not isinstance(previews, list) or any(type(p) is not int for p in previews)
                or previews != x["preview_ids"]):
            raise Refused("content-send-previews")
        return {**x, "media_ids": list(x["media_ids"]), "preview_ids": list(x["preview_ids"]),
                "media_types": dict(x["media_types"])}

    def check_catalog(self, con, account):
        """The reviewed row must match the manifest exactly; nothing else may be sellable."""
        rows = con.execute("SELECT id,kind,media_ids,preview_media_ids,price_cents,is_free_teaser,enabled,pricing_mode "
                           "FROM catalog_items WHERE account_id=?", (account,)).fetchall()
        x = self.item
        mine = [r for r in rows if r["id"] == x["catalog_id"]]
        if len(mine) != 1 or any(r["id"] != x["catalog_id"] and r["enabled"] for r in rows):
            raise Refused("content-catalog-scope")
        r = mine[0]
        try:
            stored_media, stored_previews = json.loads(r["media_ids"]), json.loads(r["preview_media_ids"])
        except (TypeError, ValueError):
            raise Refused("content-catalog-drift") from None
        if (r["kind"] != x["kind"] or stored_media != x["media_ids"] or stored_previews != x["preview_ids"]
                or r["price_cents"] != x["price_cents"] or bool(r["is_free_teaser"])
                or r["pricing_mode"] != "fixed"):
            raise Refused("content-catalog-drift")

    def evidence(self, raw, expected):
        if Decimal(str(raw.get("price", 0))) * 100 != expected["price_cents"]:
            raise Refused("content-receipt-price")
        media = raw.get("media")
        want = {(str(i), expected["media_types"][str(i)]) for i in expected["media_ids"]}
        if (not isinstance(media, list) or len(media) != len(expected["media_ids"])
                or any(not isinstance(m, dict) for m in media)
                or {(str(m.get("id")), m.get("type")) for m in media} != want
                or raw.get("mediaCount", len(media)) != len(media)
                or raw.get("isTip") or raw.get("isUnsent") or raw.get("lockedText")):
            raise Refused("content-receipt-media")
        # The provider may omit `previews`; a present value must be exactly ours.
        previews_confirmed = raw.get("previews") is not None
        if previews_confirmed:
            seen = raw["previews"]
            if (not isinstance(seen, list) or any(isinstance(p, bool) or not isinstance(p, (int, str)) for p in seen)
                    or sorted(str(p) for p in seen) != sorted(str(p) for p in expected["preview_ids"])):
                raise Refused("content-receipt-previews")
        return {"catalog_id": expected["catalog_id"], "media_ids": list(expected["media_ids"]),
                "preview_ids": list(expected["preview_ids"]), "kind": expected["kind"],
                "price_cents": expected["price_cents"], "previews_confirmed": previews_confirmed}
