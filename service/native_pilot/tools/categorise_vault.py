"""Backfill internal folders from saved metadata, with no provider/model calls.

python -m native_pilot.tools.categorise_vault --account of-123 [--apply]
Dry-run is the default. Safe to repeat; existing manual folder edits are kept.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def run(args):
    from sqlalchemy import select
    from db.engine import get_session
    from db.models import VaultItem
    from native_pilot.scope import AccountScope
    from vault_auto_folders import assign, categories, MARKER
    scope = AccountScope.parse(args.account)
    if not scope.twin_form or os.environ.get("ONLYFANS_PROCESS_ROLE") not in {None, "worker"}:
        raise ValueError("Use an explicit Twin account in a worker process")
    async with get_session() as s:
        rows = list(await s.scalars(select(VaultItem).where(
            VaultItem.account_id == scope.db_id, VaultItem.removed_at.is_(None),
            VaultItem.kind.in_(["photo", "video", "gif"])).order_by(VaultItem.media_id).limit(args.limit)))
    counts = {}
    for item in rows:
        if args.apply:
            async with get_session() as s:
                result = await assign(s, scope.db_id, item.media_id)
                await s.commit()
        else:
            try:
                fields = json.loads(item.ai_fields_json or "{}")
                if not isinstance(fields, dict):
                    raise ValueError()
                result = {"status": "already_categorised" if fields.get(MARKER) is True else "would_categorise",
                    "categories": categories(item)}
            except (ValueError, TypeError):
                result = {"status": "invalid_metadata"}
        status = result["status"]
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps({"account": scope.db_id, "apply": args.apply, "scanned": len(rows), "counts": counts}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 5000:
        parser.error("--limit must be 1..5000")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
