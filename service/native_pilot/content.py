"""Explicit single-card pilot configuration; no import starts or enables anything.

Only reviewed paid items listed in the manifest may be sent (one item at the
fixed US$3 by default, or several within a manifest price range of 300..10000
cents): each a fixed-price card that carries its full media set and its free preview subset, in the exact shape the native
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
PRICE_FLOOR_CENTS, PRICE_CEIL_CENTS = 300, 10000   # hard bounds a manifest range may never exceed
MAX_ITEMS = 50
MAX_MEDIA = 10
MEDIA_TYPES = ("photo", "video")
ITEM_KINDS = MEDIA_TYPES + ("image", "image_set")   # catalogue kinds create_catalog_single writes
ITEM_KEYS = {"catalog_id", "kind", "price_cents", "media", "preview_ids"}


def _id(value):
    return type(value) is int and value > 0


def _item(x, lo, hi):
    if (not isinstance(x, dict) or set(x) != ITEM_KEYS
            or not _id(x["catalog_id"]) or x["kind"] not in ITEM_KINDS
            or type(x["price_cents"]) is not int):
        raise Refused("content-plan-item")
    if not lo <= x["price_cents"] <= hi:
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
    return {"catalog_id": x["catalog_id"], "kind": x["kind"], "price_cents": x["price_cents"],
            "media_ids": ids, "media_types": {str(m["id"]): m["type"] for m in media},
            "preview_ids": list(previews)}


class ContentPlan:
    """An account catalogue plan: one or more reviewed fixed-price items.

    Manifest (``fan_id`` is the fan, or ``"*"`` for the whole account)::

        {"account_id": "of-<owner>", "fan_id": <fan>|"*",
         "price_range_cents": [300, 10000],      # optional; default [300, 300]
         "items": [{"catalog_id": N, "kind": "video", "price_cents": 300,
                    "media": [{"id": <id>, "type": "photo|video"}, ...],
                    "preview_ids": [<subset of media ids>]}, ...]}

    Without ``price_range_cents`` the legacy rule holds: every price is exactly
    US$3. The range is bounded to 300..10000 and each item price must fall in it.
    """

    vault_mode = False
    enables_catalog = True

    def __init__(self, path, account, fan):
        self.raw = json.loads(Path(path).read_text(encoding="utf-8"))
        r = self.raw
        if not isinstance(r, dict) or not AccountScope.valid(r.get("account_id")) \
                or AccountScope.parse(r["account_id"]) != AccountScope.parse(account) \
                or (fan is not None and not _id(fan)) \
                or (not _id(r.get("fan_id")) and r.get("fan_id") != "*") \
                or (r.get("fan_id") != fan and r.get("fan_id") != "*") \
                or set(r) - {"account_id", "fan_id", "items", "price_range_cents"}:
            raise Refused("content-plan-scope")
        lo = hi = PRICE_CENTS
        if "price_range_cents" in r:
            rng = r["price_range_cents"]
            if (not isinstance(rng, list) or len(rng) != 2 or any(type(v) is not int for v in rng)
                    or not PRICE_FLOOR_CENTS <= rng[0] <= rng[1] <= PRICE_CEIL_CENTS):
                raise Refused("content-plan-terms")
            lo, hi = rng
        items = r.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            raise Refused("content-plan-shape")
        self.items = [_item(x, lo, hi) for x in items]
        ids = [i["catalog_id"] for i in self.items]
        shapes = [tuple(i["media_ids"]) for i in self.items]
        if len(set(ids)) != len(ids) or len(set(shapes)) != len(shapes):
            raise Refused("content-plan-shape")   # terms() must identify exactly one item
        self.price_range = (lo, hi)
        self.item = self.items[0]       # first listed item (single-item callers)
        # The runner's "card sent" stop condition keys off the paid price.
        self.paid = self.item

    @property
    def catalog_ids(self):
        return {i["catalog_id"] for i in self.items}

    def item_for(self, catalog_id):
        return next((i for i in self.items if i["catalog_id"] == catalog_id), None)

    def terms(self, kwargs):
        if not kwargs:
            return None
        if not isinstance(kwargs, dict) or set(kwargs) - {"media_files", "price", "locked_text", "previews"}:
            raise Refused("content-send-fields")
        media = kwargs.get("media_files")
        if not isinstance(media, list) or any(type(m) is not int for m in media):
            raise Refused("content-send-media")
        x = next((i for i in self.items if i["media_ids"] == media), None)
        if x is None:
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

    def check_catalog(self, con, account, require_enabled=False):
        """Every reviewed row must match the manifest exactly; nothing else may be sellable."""
        rows = con.execute("SELECT id,kind,media_ids,preview_media_ids,price_cents,is_free_teaser,enabled,pricing_mode "
                           "FROM catalog_items WHERE account_id=?", (account,)).fetchall()
        listed = self.catalog_ids
        if any(r["id"] not in listed and r["enabled"] for r in rows):
            raise Refused("content-catalog-scope")
        for x in self.items:
            mine = [r for r in rows if r["id"] == x["catalog_id"]]
            if len(mine) != 1:
                raise Refused("content-catalog-scope")
            r = mine[0]
            try:
                stored_media, stored_previews = json.loads(r["media_ids"]), json.loads(r["preview_media_ids"])
            except (TypeError, ValueError):
                raise Refused("content-catalog-drift") from None
            if (r["kind"] != x["kind"] or stored_media != x["media_ids"] or stored_previews != x["preview_ids"]
                    or r["price_cents"] != x["price_cents"] or bool(r["is_free_teaser"])
                    or r["pricing_mode"] != "fixed" or (require_enabled and not r["enabled"])):
                raise Refused("content-catalog-drift")

    def check_send(self, con, account, terms):
        """Per-send store check at the wire; the catalogue row must still be enabled."""
        row = con.execute("SELECT enabled FROM catalog_items WHERE id=? AND account_id=?",
                          (terms["catalog_id"], account)).fetchone()
        if not row or not row["enabled"]:
            raise Refused("content-item-disabled")
        return terms

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


class VaultPlan:
    """FASTT's stock vault mode (``--vault-mode``; default off, catalogue mode untouched).

    FASTT's own ask lane (``sell_lane`` -> ``pack_sender``) picks media from the account's
    ``vault_items`` mirror, prices it with its own ladder and sends one card through
    ``client.send_message(fan, caption, price=, locked_text=False, media_files=, previews=)``.
    No manifest names the item, so this boundary re-checks what FASTT sent instead: every media
    id is a live (``removed_at IS NULL``) ``vault_items`` row of THIS account, of a kind the
    provider receipt can be compared with, previews are a subset of the media, the price is a
    whole number of cents inside FASTT's own floor and ceiling (``automations._price_limits``,
    the constants ``upsell`` uses), the text is not locked, and the provider receipt must
    match exactly what was sent. One attempt, send cap, unknown-send hold and human controls
    are the runner's and are unchanged.
    """

    vault_mode = True
    enables_catalog = False      # FASTT's own lane rows (enabled=0) are never enabled or listed here
    # FASTT's lane rows: the ask lane's and the hook lane's (pack_sender.ASK_ITEM_TAG / HOOK_CATEGORY).
    LANE_TAGS = ("rung:vault-ask", "hook_upsell")

    def __init__(self, account, fan, path=None):
        if not AccountScope.valid(account) or (fan is not None and fan != "*" and not _id(fan)):
            raise Refused("content-plan-scope")
        self.account = AccountScope.parse(account).db_id
        self.fan = fan
        self.path = path

    @staticmethod
    def bounds():
        from automations._price_limits import OF_PRICE_FLOOR_CENTS, OF_PRICE_MAX_CENTS
        return OF_PRICE_FLOOR_CENTS, OF_PRICE_MAX_CENTS

    @property
    def catalog_ids(self):
        """Open PPVs of FASTT's own lane rows are settled by OnlyFans (the fan unlocks), like an
        approved manifest item in catalogue mode; any other open offer still blocks."""
        if not self.path:
            return frozenset()
        from twin_bridge.store import readonly
        clause = " OR ".join("tags LIKE ?" for _ in self.LANE_TAGS)
        with readonly(self.path) as con:
            rows = con.execute(f"SELECT id FROM catalog_items WHERE account_id=? AND ({clause})",
                               (self.account, *(f'%"{t}"%' for t in self.LANE_TAGS))).fetchall()
        return frozenset(int(r[0]) for r in rows)

    def terms(self, kwargs):
        if not kwargs:
            return None
        if not isinstance(kwargs, dict) or set(kwargs) - {"media_files", "price", "locked_text", "previews"}:
            raise Refused("content-send-fields")
        media = kwargs.get("media_files")
        if (not isinstance(media, list) or not media or any(not _id(m) for m in media)
                or len(set(media)) != len(media)):
            raise Refused("content-send-media")
        price = kwargs.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise Refused("content-send-price")
        cents = Decimal(str(price)) * 100
        lo, hi = self.bounds()
        if cents != cents.to_integral_value() or not lo <= cents <= hi:
            raise Refused("content-send-price")
        if kwargs.get("locked_text", False) is not False:
            raise Refused("content-send-lock")
        previews = kwargs.get("previews", [])
        if (not isinstance(previews, list) or any(not _id(p) for p in previews)
                or len(set(previews)) != len(previews) or not set(previews) <= set(media)):
            raise Refused("content-send-previews")
        return {"catalog_id": None, "kind": None, "price_cents": int(cents), "media_ids": list(media),
                "media_types": {}, "preview_ids": list(previews)}

    def check_catalog(self, con, account, require_enabled=False):
        """Nothing is listed, so nothing can drift; the per-send vault check is ``check_send``."""

    def check_send(self, con, account, terms):
        ids = terms["media_ids"]
        rows = con.execute("SELECT media_id,kind FROM vault_items WHERE account_id=? AND removed_at IS NULL AND media_id IN (%s)"
                           % ",".join("?" * len(ids)), (account, *ids)).fetchall()
        kinds = {int(r[0]): r[1] for r in rows}
        if set(kinds) != set(ids) or any(k not in MEDIA_TYPES for k in kinds.values()):
            raise Refused("content-send-media")
        return {**terms, "media_types": {str(i): kinds[i] for i in ids}}

    def evidence(self, raw, expected):
        return ContentPlan.evidence(self, raw, expected)
