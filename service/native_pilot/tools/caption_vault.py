"""One-off: caption N vault items of ONE account with FASTT's own captioner.

No captioning logic lives here. Every item goes through ``vault_ai_api._describe_one``
(frames via ``vault_frames.collect``, vision prompt, legacy ``llm_client.chat``) and is
persisted by that function itself, into the same ``vault_items`` columns the dashboard's
"describe all" fills (description / video_description, tags, search_text,
ai_fields_json, describe_status, describe_model, ...). The candidate query below is the
non-force branch of ``vault_ai_api._run_describe_all`` plus a LIMIT.

Two adapters, because this runs in the Twin backend image, which has no FASTT session
store and runs under a dedicated process role:

1. OF client. ``vault_frames`` / ``vault_stills`` obtain their client from
   ``automation_executor._make_client(account_id)``. For this process only, that seam
   returns the client built from Twin's verified connection (the same two calls the
   native pilot runner makes: ``load_verified_connection`` + ``build_client``, which
   also opens the role guard for this ONE provider account).
2. Model call. ``llm_client.chat`` refuses while ``ONLYFANS_PROCESS_ROLE`` is set. After
   the connection is loaded and the client built (both want the role intact), this
   process drops the variable, so the legacy caller runs as the unset-role legacy
   application would. Only ``worker`` (or unset) may do this; ``api`` is refused. The
   provider key is the deployment's ``OPENROUTER_API_KEY`` (house key), never the
   account's pinned chatter-model credential.

Usage (inside the backend image, from /app):
  python -m native_pilot.tools.caption_vault --account of-<owner> --limit 50 [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_MODEL = "openrouter/qwen3-vl-30b"
ROLE_ENV = "ONLYFANS_PROCESS_ROLE"


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)


async def candidates(api, account: str, limit: int, *, fresh_only: bool = False) -> list[int]:
    """Un-described, not-removed photo/video media ids, newest first.

    Same WHERE / ORDER BY as the non-force branch of ``_run_describe_all``."""
    from sqlalchemy import select
    from db.engine import get_session
    from db.models import VaultItem

    stmt = select(VaultItem.media_id).where(
        VaultItem.account_id == account, VaultItem.removed_at.is_(None),
        VaultItem.kind.in_(api._DESCRIBABLE_KINDS),
    )
    if fresh_only:
        stmt = stmt.where(VaultItem.describe_status.is_(None))
    else:
        # IS DISTINCT FROM: portable (a bare IS NOT 'text' is SQLite-only and fails on PostgreSQL).
        stmt = stmt.where(VaultItem.describe_status.is_distinct_from("described"))
    stmt = stmt.order_by(VaultItem.created_at.desc(), VaultItem.media_id.desc()).limit(limit)
    async with get_session() as s:
        return [r[0] for r in (await s.execute(stmt)).all()]


def install_client_adapter(client: Any, db_account: str) -> None:
    """Make FASTT's client seam return Twin's verified client, for this account only."""
    import automation_executor as ax

    def _make_client(account_id: str):
        if account_id != db_account:
            raise RuntimeError(f"caption_vault is bound to {db_account}, not {account_id}")
        return client

    ax._make_client = _make_client


def install_llm_adapter() -> None:
    """Let the legacy ``llm_client.chat`` run in this process (see module docstring)."""
    import llm_client
    import llm_providers

    role = os.environ.get(ROLE_ENV)
    if role not in (None, "worker"):
        raise RuntimeError(f"caption_vault must run as the worker role or with no role, not {role!r}")
    os.environ.pop(ROLE_ENV, None)

    async def _house_key(account_id, prov, purpose=""):
        key = llm_providers.house_key(prov.name)
        if not key:
            raise llm_client.LLMConfigError(f"{prov.api_key_env} is missing/empty")
        return key

    llm_client._tenant_api_key = _house_key


async def caption(api, account: str, ids: list[int], *, model: str, with_flags: bool = False) -> dict[str, Any]:
    """Sequentially run FASTT's ``_describe_one`` over ``ids``; one JSON line each."""
    done = failed = 0
    cost = 0.0
    stopped = None
    for mid in ids:
        try:
            res = await api._describe_one(account, mid, model=model)
        except Exception as e:  # noqa: BLE001  (HTTPException 404 etc.; one item must not end the run)
            res = {"media_id": mid, "ok": False, "status": "exception",
                   "detail": f"{type(e).__name__}: {e}"[:300]}
        if res.get("ok") and with_flags:
            try:
                await api._flags_one(account, mid)
            except Exception as e:  # noqa: BLE001
                res["flags_error"] = f"{type(e).__name__}: {e}"[:200]
        ok = bool(res.get("ok"))
        c = res.get("cost_millicents")
        row = {"media_id": mid, "ok": ok, "status": res.get("status"),
               "chars": len(res.get("description") or ""), "cost_millicents": c,
               "model": res.get("describe_model")}
        if not ok:
            row["detail"] = res.get("detail") or res.get("raw")
        emit(row)
        if ok:
            done += 1
            cost += float(c or 0)
        else:
            failed += 1
        if res.get("status") == "capped":
            stopped = "capped"
            break
    return {"summary": True, "account": account, "attempted": done + failed, "described": done,
            "failed": failed, "cost_millicents": cost, "stopped": stopped}


async def run(args, api=None) -> dict[str, Any]:
    from native_pilot.scope import AccountScope

    scope = AccountScope.parse(args.account)
    if not scope.twin_form:
        raise SystemExit("--account must be the Twin form of-<owner>")
    if api is None:
        import vault_ai_api as api
    ids = await candidates(api, scope.db_id, args.limit, fresh_only=args.fresh_only)
    if args.dry_run:
        for mid in ids:
            emit({"media_id": mid, "would_caption": True})
        return {"summary": True, "account": scope.db_id, "dry_run": True, "would_caption": len(ids),
                "model": args.model}
    if not ids:
        return {"summary": True, "account": scope.db_id, "attempted": 0, "described": 0, "failed": 0,
                "cost_millicents": 0.0, "stopped": None}
    from native_pilot import connection
    conn = await connection.load_verified_connection(scope)
    client = await asyncio.to_thread(connection.build_client, conn)
    try:
        install_client_adapter(client, scope.db_id)
        install_llm_adapter()
        return await caption(api, scope.db_id, ids, model=args.model, with_flags=args.with_flags)
    finally:
        await asyncio.to_thread(client.http.close)


def parse(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--account", required=True, help="Twin account id, of-<owner>")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--dry-run", action="store_true", help="list candidates; no model or provider call")
    p.add_argument("--fresh-only", action="store_true",
                   help="only never-attempted items (describe_status NULL), so failed/refused ones do not eat the limit")
    p.add_argument("--with-flags", action="store_true",
                   help="also run FASTT's exposure-flags pass after each caption, as describe-all does")
    a = p.parse_args(argv)
    if not 1 <= a.limit <= 5000:
        p.error("--limit must be 1..5000")
    return a


def main(argv=None) -> int:
    args = parse(argv)
    out = asyncio.run(run(args))
    emit(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
