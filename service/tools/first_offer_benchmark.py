"""first_offer_benchmark -- READ-ONLY pacing reference per creator (R5, item 8).

For the operator to run later against a copy or read replica. It only SELECTs from ``messages`` (human and automated
creator sends alike are counted, split by ``automation_kind``) and prints aggregates. It never writes, never prints
fan ids, message text or account ids (accounts are numbered in the output), and opens the database read-only.

Per creator it reports, for the FIRST priced card and for REPEAT cards separately:
  * conversations that reached a card / conversations with at least one creator message (coverage),
  * creator turns (grouped runs of bubbles between fan messages) before the card: median and quartiles,
  * raw outgoing bubbles before the card, excluding the card: median and quartiles.
Grouped turns and raw bubbles are separate numbers and are never added or substituted for each other. This is a
descriptive reference only; it does not set any control and nothing reads it at run time.

  python tools/first_offer_benchmark.py --db sqlite:///copy.db            (or postgresql://...?options=-c%20default_transaction_read_only%3Don)
  python tools/first_offer_benchmark.py --db ... --human-only
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SQL = ("SELECT account_id, fan_id, direction, price_cents, body, automation_kind FROM messages "
       "WHERE is_unsent = FALSE ORDER BY account_id, fan_id, created_at, message_id")


def card_gaps(rows):
    """rows: chronological (direction, is_card, body) of ONE conversation.
    Returns [(is_first, grouped_turns, raw_bubbles), ...], one per card; the card itself is excluded from both."""
    from automations.sales_action import count_since_last_card
    out, window, seen = [], [], False
    for direction, card, body in rows:
        if direction == "out" and card:
            c = count_since_last_card(window)
            out.append((not seen, c.grouped_turns, c.raw_bubbles))
            seen = True
            window = []
        else:
            window.append((direction, False, body))
    return out


def _q(values):
    if not values:
        return None
    s = sorted(values)
    qs = statistics.quantiles(s, n=4, method="inclusive") if len(s) >= 2 else [s[0]] * 3
    return {"n": len(s), "median": statistics.median(s), "q1": qs[0], "q3": qs[2]}


def summarize(conversations):
    """conversations: {account: [rows_of_conversation, ...]} -> per-account aggregate (anonymised by the caller)."""
    result = {}
    for account, convs in conversations.items():
        first_g, first_r, rep_g, rep_r = [], [], [], []
        reached = engaged = 0
        for rows in convs:
            if not any(d == "out" for d, _, _ in rows):
                continue
            engaged += 1
            gaps = card_gaps(rows)
            reached += bool(gaps)
            for is_first, g, r in gaps:
                (first_g if is_first else rep_g).append(g)
                (first_r if is_first else rep_r).append(r)
        result[account] = {
            "conversations_with_creator_message": engaged, "conversations_reaching_a_card": reached,
            "coverage": round(reached / engaged, 4) if engaged else None,
            "first_offer": {"grouped_turns": _q(first_g), "raw_bubbles": _q(first_r)},
            "repeat_offer": {"grouped_turns": _q(rep_g), "raw_bubbles": _q(rep_r)}}
    return result


def load(url, human_only=False):
    from sqlalchemy import create_engine, text
    engine = create_engine(url)
    conv = defaultdict(lambda: defaultdict(list))
    with engine.connect() as con:
        if con.dialect.name == "postgresql":
            con.exec_driver_sql("SET TRANSACTION READ ONLY")
        for account, fan, direction, price, body, kind in con.execute(text(SQL)):
            if human_only and direction == "out" and kind is not None:
                continue
            conv[account][fan].append((direction, bool(direction == "out" and price and price > 0), body))
        con.rollback()
    return {a: list(f.values()) for a, f in conv.items()}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--human-only", action="store_true", help="count only creator sends with no automation_kind")
    args = ap.parse_args(argv)
    summary = summarize(load(args.db, args.human_only))
    numbered = {f"creator_{i + 1}": v for i, (_, v) in enumerate(sorted(summary.items()))}
    pooled = [g for v in summary.values() for g in ([] if v["first_offer"]["grouped_turns"] is None else [v["first_offer"]["grouped_turns"]["median"]])]
    print(json.dumps({"creators": numbered, "median_of_creator_medians_first_offer_grouped": statistics.median(pooled) if pooled else None,
                      "note": "descriptive reference only; grouped turns and raw bubbles are separate"}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
