"""Build a runner content manifest from the account's approved catalogue; read-only.

The runner (``--content-manifest``) sells only the items a manifest lists, and
``ContentPlan.check_catalog`` refuses any enabled catalogue row it does not list.
Writing that file by hand per creator is the setup this replaces: the manifest is
derived from what the operator already approved in the dashboard (enabled,
fixed-price, non-teaser ``catalog_items`` rows, media types from ``vault_items``).

Nothing is clamped or invented. A row that cannot be sold exactly as stored (no
media, a media id missing from the vault, a price outside the supported range)
is reported as a blocker, because leaving it enabled but unlisted would stop the
runner anyway. Fix the row in the dashboard (or disable it) and rebuild.

    python -m native_pilot.manifest --db <sqlite path | postgresql url> \\
        --account of-<owner> (--fan <id> | --all-fans) --out manifest.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from twin_bridge.store import readonly

from .content import MAX_ITEMS, MAX_MEDIA, MEDIA_TYPES, PRICE_CEIL_CENTS, PRICE_CENTS, PRICE_FLOOR_CENTS, ContentPlan
from .ledger import Refused
from .scope import AccountScope


def _ids(raw):
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list) or any(type(v) is not int or v <= 0 for v in value):
        return None
    return value


def build(db, account, fan):
    """(manifest or None, blockers): the manifest lists every enabled sellable row."""
    if not AccountScope.valid(account) or not (fan == "*" or (type(fan) is int and fan > 0)):
        raise Refused("manifest-scope")
    items, blockers = [], []
    with readonly(db) as con:
        rows = con.execute("SELECT id,kind,media_ids,preview_media_ids,price_cents,is_free_teaser,pricing_mode "
                           "FROM catalog_items WHERE account_id=? AND enabled ORDER BY id", (account,)).fetchall()
        for r in rows:
            media, previews = _ids(r["media_ids"]), _ids(r["preview_media_ids"])
            reason = None
            if bool(r["is_free_teaser"]) or r["pricing_mode"] != "fixed":
                reason = "not-fixed-paid"
            elif r["kind"] not in MEDIA_TYPES or media is None or previews is None:
                reason = "invalid-row"
            elif not 1 <= len(media) <= MAX_MEDIA or not set(previews) <= set(media):
                reason = "invalid-media"
            elif not PRICE_FLOOR_CENTS <= r["price_cents"] <= PRICE_CEIL_CENTS:
                reason = "price-out-of-range"
            if reason is None:
                marks = ",".join("?" * len(media))
                kinds = {row["media_id"]: row["kind"] for row in con.execute(
                    f"SELECT media_id,kind FROM vault_items WHERE account_id=? AND media_id IN ({marks})",
                    (account, *media)).fetchall()}
                if set(kinds) != set(media) or any(kinds[m] not in MEDIA_TYPES for m in media):
                    reason = "media-not-in-vault"
            if reason is not None:
                blockers.append({"catalog_id": r["id"], "reason": reason})
                continue
            items.append({"catalog_id": r["id"], "kind": r["kind"], "price_cents": r["price_cents"],
                          "media": [{"id": m, "type": kinds[m]} for m in media], "preview_ids": previews})
    if not items:
        blockers.append({"catalog_id": None, "reason": "no-approved-items"})
    if len(items) > MAX_ITEMS:
        blockers.append({"catalog_id": None, "reason": "too-many-items"})
    if blockers:
        return None, blockers
    manifest = {"account_id": account, "fan_id": fan, "items": items}
    prices = [i["price_cents"] for i in items]
    if prices != [PRICE_CENTS] * len(prices):
        manifest["price_range_cents"] = [min(prices), max(prices)]
    return manifest, []


def verify(db, manifest, fan):
    """Load the manifest exactly as the runner will and recheck it against the DB."""
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, sort_keys=True)
        plan = ContentPlan(path, manifest["account_id"], None if fan == "*" else fan)
        with readonly(db) as con:
            plan.check_catalog(con, manifest["account_id"])
        return plan
    finally:
        os.unlink(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True)
    parser.add_argument("--account", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--fan", type=int)
    scope.add_argument("--all-fans", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    fan = "*" if args.all_fans else args.fan
    manifest, blockers = build(args.db, args.account, fan)
    if blockers:
        print(json.dumps({"ok": False, "blockers": blockers}, sort_keys=True))
        return 2
    verify(args.db, manifest, fan)
    Path(args.out).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    print(json.dumps({"ok": True, "items": len(manifest["items"]),
                      "price_range_cents": manifest.get("price_range_cents")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
