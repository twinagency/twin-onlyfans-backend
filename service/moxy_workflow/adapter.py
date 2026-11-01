"""Moxy workflow adapter: SHADOW/dry-run lane between the OnlyFans backend and the Moxy engine.

Per-account config lives in the existing account config JSON (account_ai_config.ai_chatter_config_json):

    "moxy_workflow": {"mode": "off" | "shadow" | "live", "persona_path": "...", "playbook_paths": ["..."]}

Default is off. Only SHADOW is implemented: the Engine Decision is recorded in the existing append-only
``of_sales_decision`` table (stage "moxy", mode "shadow", full decision in evidence_json) and NOTHING is sent.
"live" refuses with LiveNotEnabled. This module imports no sender, client, network or messaging code by design;
offers are mapped to vault/PPV ids and prices as data only.

DB inputs: fan purchases come from ``vault_sends`` (was_purchased) and replay as Engine.on_purchase so the vault
advances. Fan labels are not stored in a structured table, so callers pass them via ``labels``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .engine import Decision, Engine
from .persona import load_persona
from .playbook import load_playbook
from .state import JsonStateStore

log = logging.getLogger(__name__)

KEY = "moxy_workflow"
MODES = ("off", "shadow", "live")
STAGE = "moxy"


class LiveNotEnabled(RuntimeError):
    """mode=live was requested; this build only supports shadow."""


@dataclass
class ShadowResult:
    mode: str
    status: str                        # "off" | "shadow" | "refused"
    reason: str = ""
    decision: Decision | None = None
    offer: dict | None = None          # data-only mapping of the engine offer (ids/prices), never sent
    logged: bool = False
    purchases_replayed: int = 0


def read_account_settings(conn, account_id: str) -> dict:
    row = conn.execute(text("SELECT ai_chatter_config_json FROM account_ai_config WHERE account_id=:a"),
                       {"a": account_id}).fetchone()
    if not row or not row[0]:
        return {}
    try:
        v = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def read_config(settings: dict) -> dict:
    """Normalised moxy_workflow block. Anything invalid degrades to mode=off."""
    blk = settings.get(KEY) if isinstance(settings, dict) else None
    if not isinstance(blk, dict):
        return {"mode": "off", "persona_path": None, "playbook_paths": []}
    mode = blk.get("mode", "off")
    if not isinstance(mode, str) or mode not in MODES:
        log.warning("invalid moxy_workflow.mode: treating as off")
        mode = "off"
    paths = blk.get("playbook_paths") or []
    if not isinstance(paths, list):
        paths = []
    return {"mode": mode, "persona_path": blk.get("persona_path"), "playbook_paths": [str(p) for p in paths]}


def _purchases(conn, account_id: str, fan_id: int) -> list:
    rows = conn.execute(text(
        "SELECT media_id, price_cents FROM vault_sends WHERE account_id=:a AND fan_id=:f AND was_purchased "
        "ORDER BY sent_at, media_id"), {"a": account_id, "f": fan_id}).fetchall()
    return [(str(r[0]), int(r[1] or 0)) for r in rows]


def _log(conn, account_id, fan_id, source_message_id, mode, action, outcome, reason, settings, evidence) -> bool:
    exists = conn.execute(text(
        "SELECT 1 FROM of_sales_decision WHERE account_id=:a AND fan_id=:f AND source_message_id=:m AND stage=:s"),
        {"a": account_id, "f": fan_id, "m": source_message_id, "s": STAGE}).fetchone()
    if exists:
        return False
    conn.execute(text(
        "INSERT INTO of_sales_decision(account_id,fan_id,source_message_id,stage,mode,action,approach,outcome,"
        "reason,settings_json,evidence_json,created_at) VALUES(:a,:f,:m,:s,:mode,:act,'none',:o,:r,:sj,:ej,:t)"),
        {"a": account_id, "f": fan_id, "m": source_message_id, "s": STAGE, "mode": mode, "act": action[:32],
         "o": outcome, "r": reason[:64], "sj": json.dumps(settings), "ej": json.dumps(evidence, ensure_ascii=False),
         "t": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")})
    return True


def run_workflow(conn, account_id: str, fan_id: int, messages, *, source_message_id: int, labels=None,
                 state_store: JsonStateStore | None = None, clock=None) -> ShadowResult:
    """Evaluate one fan turn. off: no-op. shadow: decide + record, send nothing. live: raises LiveNotEnabled."""
    cfg = read_config(read_account_settings(conn, account_id))
    mode = cfg["mode"]
    if mode == "off":
        return ShadowResult("off", "off", "mode off")
    if mode == "live":
        raise LiveNotEnabled("moxy_workflow mode=live is not enabled in this build (shadow only)")

    def refuse(why: str) -> ShadowResult:
        ok = _log(conn, account_id, fan_id, source_message_id, "shadow", "refused", "blocked", why, cfg, {"error": why})
        return ShadowResult("shadow", "refused", why, logged=ok)

    if not cfg["persona_path"]:
        return refuse("persona_path not configured")
    if not Path(cfg["persona_path"]).is_file():
        return refuse("persona file missing")
    try:
        persona = load_persona(cfg["persona_path"])
        book = load_playbook(*cfg["playbook_paths"]) if cfg["playbook_paths"] else None
    except (OSError, ValueError, KeyError, TypeError) as e:
        return refuse(f"persona/playbook invalid: {type(e).__name__}")
    if book is None:
        return refuse("playbook_paths not configured")

    store = state_store or JsonStateStore(os.environ.get("MOXY_SHADOW_STATE_DIR", "data/moxy_shadow"))
    eng = Engine(persona, book, clock=clock)
    st = store.load(account_id, str(fan_id))
    if labels is not None:
        st.labels = list(labels)
    replayed = 0
    for pid, cents in _purchases(conn, account_id, fan_id):
        if pid not in st.purchased:
            eng.on_purchase(st, pid, cents)
            replayed += 1
    if isinstance(messages, str):
        messages = [messages]
    d = eng.on_message(st, "\n".join(m for m in messages if m))
    store.save(st)

    offer = None
    if d.offer:
        offer = {"vault_id": d.offer["id"], "kind": d.offer["kind"], "price_cents": d.offer["price_cents"],
                 "label": d.offer.get("label")}
    evidence = {"decision": asdict(d), "offer_data": offer, "purchases_replayed": replayed,
                "vault": {"level": st.vault_level, "step": st.vault_step}, "sent": False}
    ok = _log(conn, account_id, fan_id, source_message_id, "shadow", d.action, "evaluated",
              d.scenario or d.reason, cfg, evidence)
    return ShadowResult("shadow", "shadow", d.reason, decision=d, offer=offer, logged=ok, purchases_replayed=replayed)
