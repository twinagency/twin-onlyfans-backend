"""R0 read-only baseline snapshot (final implementation plan, 1 Nov 2026).

Run by the designated operator only, inside an existing backend container that
already has DATABASE_URL (for example `docker exec <worker> python
/path/r0_readonly_snapshot.py`). It opens ONE read-only transaction, sets a
statement timeout, reads aggregates, prints JSON and exits. It never writes,
starts, stops, repairs or enqueues anything.

Tolerant by design: a missing table or column is reported as "unavailable"
instead of failing, because the live schema/image provenance is exactly what
R0 has to establish.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

# Config keys the plan needs per account (account_ai_config.ai_chatter_config_json).
CONFIG_KEYS = (
    "enabled", "mode", "payers_only", "fastt_contextual_sales_mode", "pack_send_enabled",
    "pack_on_ask_enabled", "qualification_gate_enabled", "fastt_vault_mode", "fastt_text_only",
    "prompt_sell_catalog", "pivot_on_escalation", "force_ask", "max_open_offers",
    "max_offers_per_fan_per_day", "min_fan_msgs_between_offers", "haggle_discount_pct",
    "teaser_discount_pct", "spend_velocity_cap_7d_cents", "persona_facts_on_ask_only",
)
# Style switches (account_ai_config.style_config_json). painful_texting is the
# brevity governor ("write the LEAST", "a line that PUNCHES beats one that explains"); default ON.
STYLE_KEYS = ("painful_texting", "strip_emojis", "factground_welcome_chatter_for_info", "sell_customs")


def _url() -> str:
    raw = os.environ.get("DATABASE_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not raw:
        raise SystemExit("DATABASE_URL is not set")
    url = make_url(raw)
    if url.drivername.startswith("postgresql"):
        url = url.set(drivername="postgresql+psycopg2")
    elif url.drivername.startswith("sqlite"):
        url = url.set(drivername="sqlite")
    return url


def _rows(con, sql, **params):
    return [dict(r._mapping) for r in con.execute(text(sql), params)]


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "<bytes>"
    return v


def snapshot(engine) -> dict:
    out: dict = {"checked_at": datetime.now(timezone.utc).isoformat(), "unavailable": []}
    pg = engine.dialect.name == "postgresql"
    with engine.connect() as con:
        tx = con.begin()
        try:
            if pg:
                con.execute(text("SET TRANSACTION READ ONLY"))
                con.execute(text("SET LOCAL statement_timeout = '20s'"))
            insp = inspect(con)
            tables = set(insp.get_table_names())

            def cols(t):
                return {c["name"] for c in insp.get_columns(t)} if t in tables else set()

            def grouped(t, extra_where="", select_extra=""):
                if t not in tables or "account_id" not in cols(t):
                    out["unavailable"].append(t)
                    return {}
                sql = f"SELECT account_id, count(*) AS n{select_extra} FROM {t} {extra_where} GROUP BY account_id"
                return {r["account_id"]: {k: _jsonable(v) for k, v in r.items() if k != "account_id"}
                        for r in _rows(con, sql)}

            out["schema_version"] = (_rows(con, "SELECT version_num FROM alembic_version")
                                     if "alembic_version" in tables else "unavailable")

            # Accounts, provider connection and runtime state.
            acc = {}
            if "accounts" in tables:
                c = cols("accounts")
                pick = [k for k in ("id", "nickname", "is_active_default") if k in c]
                for r in _rows(con, f"SELECT {', '.join(pick)} FROM accounts"):
                    acc[r.get("id")] = {k: _jsonable(v) for k, v in r.items() if k != "id"}
            else:
                out["unavailable"].append("accounts")
            for t in ("of_provider_connection", "of_runtime", "of_delivery_policy"):
                if t not in tables:
                    out["unavailable"].append(t)
                    continue
                c = cols(t)
                want = [k for k in ("enabled", "status", "last_error", "verified_at", "revision",
                                    "automate_new_conversations", "binding_id", "configuration_revision",
                                    "recovery_needed", "pending_events", "last_sync_at", "media_enabled",
                                    "delivery_mode", "throttle_seconds", "daily_limit_units", "limit_units",
                                    "used_units", "queued_count", "transport_kind") if k in c]
                for r in _rows(con, f"SELECT account_id, {', '.join(want)} FROM {t}"):
                    acc.setdefault(r["account_id"], {})[t] = {k: _jsonable(v) for k, v in r.items()
                                                              if k != "account_id"}
            def _blob(v):
                if isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except ValueError:
                        v = {}
                return v if isinstance(v, dict) else {}

            if "account_ai_config" in tables and "ai_chatter_config_json" in cols("account_ai_config"):
                style = ", style_config_json AS style" if "style_config_json" in cols("account_ai_config") else ""
                for r in _rows(con, f"SELECT account_id, ai_chatter_config_json AS cfg{style} FROM account_ai_config"):
                    cfg = _blob(r["cfg"])
                    entry = acc.setdefault(r["account_id"], {})
                    entry["config"] = {k: cfg.get(k, "<missing>") for k in CONFIG_KEYS}
                    if style:
                        st = _blob(r["style"])
                        entry["style"] = {k: st.get(k, "<missing>") for k in STYLE_KEYS}
            else:
                out["unavailable"].append("account_ai_config.ai_chatter_config_json")

            # Inventory: total, live, with non-empty search_text.
            if "vault_items" in tables:
                c = cols("vault_items")
                live = "WHERE removed_at IS NULL" if "removed_at" in c else ""
                described = ", sum(CASE WHEN search_text IS NOT NULL AND search_text <> '' THEN 1 ELSE 0 END) AS described" \
                    if "search_text" in c else ""
                for a, v in grouped("vault_items", live, described).items():
                    acc.setdefault(a, {})["vault_live"] = v
            else:
                out["unavailable"].append("vault_items")

            # Import / description / ledger jobs: per-account status counts and newest error.
            for t in ("of_vault_sync_job", "of_vault_media_job", "of_ledger_scan", "of_offer_import_job"):
                if t not in tables or "account_id" not in cols(t):
                    out["unavailable"].append(t)
                    continue
                c = cols(t)
                keys = [k for k in ("kind", "status", "last_error", "page_offset", "pages_read", "items_seen",
                                    "last_complete_at", "window_end", "next_poll_at", "lease_until",
                                    "requested_at", "updated_at", "completed_at") if k in c]
                order = "ORDER BY updated_at DESC" if "updated_at" in c else ""
                rows = _rows(con, f"SELECT account_id, {', '.join(keys)} FROM {t} {order} LIMIT 500")
                for r in rows:
                    acc.setdefault(r["account_id"], {}).setdefault(t, []).append(
                        {k: _jsonable(v) for k, v in r.items() if k != "account_id"})
                for a in acc:
                    if t in acc[a]:
                        acc[a][t] = acc[a][t][:5]

            # Open commercial/delivery state.
            if "of_contextual_offer" in tables:
                for r in _rows(con, "SELECT account_id, state, count(*) AS n FROM of_contextual_offer "
                                    "GROUP BY account_id, state"):
                    acc.setdefault(r["account_id"], {}).setdefault("contextual_offers", {})[r["state"]] = r["n"]
            else:
                out["unavailable"].append("of_contextual_offer")
            if "of_conversation_state" in tables:
                c = cols("of_conversation_state")
                gb = [k for k in ("mode", "takeover_state") if k in c]
                if gb:
                    for r in _rows(con, f"SELECT account_id, {', '.join(gb)}, count(*) AS n "
                                        f"FROM of_conversation_state GROUP BY account_id, {', '.join(gb)}"):
                        key = "/".join(str(r[k]) for k in gb)
                        acc.setdefault(r["account_id"], {}).setdefault("conversations", {})[key] = r["n"]
            if "of_text_intent" in tables and "status" in cols("of_text_intent"):
                for r in _rows(con, "SELECT account_id, status, count(*) AS n FROM of_text_intent "
                                    "WHERE status IN ('queued','dispatching','unknown') GROUP BY account_id, status"):
                    acc.setdefault(r["account_id"], {}).setdefault("open_text_intents", {})[r["status"]] = r["n"]
            out["accounts"] = acc
        finally:
            tx.rollback()
    return out


def main() -> None:
    engine = create_engine(_url(), pool_pre_ping=True)
    try:
        print(json.dumps(snapshot(engine), indent=1, default=str, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
