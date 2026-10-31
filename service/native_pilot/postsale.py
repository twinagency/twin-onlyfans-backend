"""The runner's post-sale scan: when FASTT's discounted resend and post-purchase upsell have something to do.

FASTT runs both from inside its own watcher (``_resolve_open_offers_stock`` behind ``qualification_gate_enabled``,
which this runner keeps off because that switch also turns on the whole ladder). The runner therefore looks for the
two moments itself, in the application database, read only, and queues one offer-keyed follow-up per moment
(``followups.py``); the stock functions then decide and act when the job runs (``ai_chatter._run_discount`` /
``_run_post_buy``), through the same single-attempt boundary as every other send.

* post-purchase: a vault card the PAYMENT LEDGER resolved (``content_offers.status='delivered'``, ``resolved_by=
  'ppv_ledger'``, written by ``onlyfans_runtime.commerce.settle_media_purchase`` together with ``messages.is_paid``
  and the ``vault_sends.was_purchased`` ownership stamps). A fan saying "i bought it", an outgoing card or a local
  status nobody confirmed never match. FASTT's own 90-240 s window sets when it runs.
* discount: a vault card still unpaid after FASTT's own ``RESEND_AFTER_M``, whose fan voiced a price objection
  and then spoke again. Silence alone never matches. ``ai_chatter._maybe_discount_resend`` re-proves every rule.

Everything here is a read plus the queue write. Nothing here sends, unsends, prices or touches a fan.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone


SETTING_DISCOUNT = "fastt_discount_resend"
SETTING_POST_BUY = "fastt_post_buy_upsell"
POST_BUY_WINDOW_S = (90, 240)          # FASTT's own spread (ai_chatter: paid_at + uniform(90, 240))
POST_BUY_FRESH = timedelta(minutes=15)  # a purchase older than this at the time of the scan is history, not an event
LANE_TAGS = ("rung:vault-ask", "hook_upsell")   # FASTT's vault lane rows (pack_sender.ASK_ITEM_TAG / HOOK_CATEGORY)

def settings(path, account):
    """``{"discount": bool, "post_buy": bool}``; each is on only for a literal ``true`` in the account's saved FASTT
    config (``ai_chatter_config_json``). Default off, and anything unreadable is off."""
    from twin_bridge import store
    try:
        with store.readonly(path) as con:
            row = con.execute("SELECT ai_chatter_config_json FROM account_ai_config WHERE account_id=?", (account,)).fetchone()
        value = json.loads(row[0]) if row and row[0] else {}
    except Exception:
        value = {}
    value = value if isinstance(value, dict) else {}
    return {"discount": value.get(SETTING_DISCOUNT) is True, "post_buy": value.get(SETTING_POST_BUY) is True}


def _naive(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value is not None and value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _lane_clause():
    return " OR ".join("c.tags LIKE ?" for _ in LANE_TAGS), tuple(f'%"{t}"%' for t in LANE_TAGS)


def purchases(con, account, now):
    """Vault cards the payment ledger settled in the last ``POST_BUY_FRESH``: ``[(offer_id, fan_id)]`` oldest first."""
    clause, args = _lane_clause()
    rows = con.execute(
        "SELECT o.id,o.fan_id,o.resolved_at FROM content_offers o JOIN catalog_items c ON c.id=o.item_id AND c.account_id=o.account_id "
        "WHERE o.account_id=? AND o.status='delivered' AND o.resolved_by='ppv_ledger' AND o.mode='ppv' "
        f"AND o.offer_message_id IS NOT NULL AND ({clause}) ORDER BY o.id DESC LIMIT 200", (account, *args)).fetchall()
    out = []
    for oid, fan, resolved in rows:
        resolved = _naive(resolved)
        if resolved is not None and now - POST_BUY_FRESH <= resolved <= now + timedelta(minutes=1):
            out.append((int(oid), int(fan)))
    return sorted(out)


def unlinked_paid_fans(con, account):
    """Fans with a paid ``ppv_message`` ledger purchase since the current creator binding that no message explains.

    Such a fan's card can look unpaid (``messages.is_paid`` is only set by a linked purchase), so no discount or other
    post-sale action may be queued for them, whether or not an operator excluded the purchase from the account hold
    (``commerce.held``'s ``_unlinked_paid``). Read only; a database without these tables has none."""
    from transaction_contract import PAID
    from twin_bridge.store import has_table
    if not has_table(con, "transactions"):
        return set()
    since = None
    if has_table(con, "of_runtime") and has_table(con, "of_creator_bindings"):
        row = con.execute("SELECT b.created_at FROM of_runtime r JOIN of_creator_bindings b ON b.id=r.binding_id "
                          "WHERE r.account_id=?", (account,)).fetchone()
        since = _naive(row[0]) if row and row[0] is not None else None
    marks = ",".join("?" for _ in PAID)
    rows = con.execute("SELECT fan_id,occurred_at FROM transactions WHERE account_id=? AND source='ledger' "
                       f"AND kind='ppv_message' AND message_id IS NULL AND fan_id IS NOT NULL AND status IN ({marks})",
                       (account, *sorted(PAID))).fetchall()
    return {int(fan) for fan, occurred in rows if since is None or _naive(occurred) >= since}


def unpaid_cards(con, account, now, after_minutes):
    """Vault cards still unpaid ``after_minutes`` after they were sent: ``[(offer_id, fan_id, offered_at, price_cents)]``.

    Cards of a fan with an unlinked paid purchase are never returned (see ``unlinked_paid_fans``)."""
    clause, args = _lane_clause()
    rows = con.execute(
        "SELECT o.id,o.fan_id,o.offered_at,o.price_cents,o.offer_message_id FROM content_offers o "
        "JOIN catalog_items c ON c.id=o.item_id AND c.account_id=o.account_id "
        "LEFT JOIN messages m ON m.account_id=o.account_id AND m.fan_id=o.fan_id AND m.message_id=o.offer_message_id "
        f"WHERE o.account_id=? AND o.status='open' AND o.mode='ppv' AND o.offer_message_id IS NOT NULL AND ({clause}) "
        "AND NOT COALESCE(m.is_paid, FALSE) ORDER BY o.id DESC LIMIT 200", (account, *args)).fetchall()
    out = []
    blocked = unlinked_paid_fans(con, account) if rows else set()
    seen_fans = set()
    for oid, fan, offered, price, _msg in rows:
        # One request concerns the latest open card, not every older unpaid card.
        if int(fan) in seen_fans:
            continue
        seen_fans.add(int(fan))
        if int(fan) in blocked:
            continue
        from twin_bridge.store import has_table
        if has_table(con, "of_contextual_offer") and con.execute(
                "SELECT 1 FROM of_contextual_offer WHERE account_id=? AND fan_id=? AND message_id=?",
                (account, int(fan), _msg)).fetchone():
            # Silence never authorizes a discounted replacement for a contextual card.
            continue
        offered = _naive(offered)
        if offered is not None and offered <= now - timedelta(minutes=after_minutes):
            out.append((int(oid), int(fan), offered, int(price or 0)))
    return out


def inbound_since(con, account, fan, since):
    """The fan's messages after ``since``, oldest first: ``[(created_at, body)]``."""
    rows = con.execute("SELECT created_at,body FROM messages WHERE account_id=? AND fan_id=? AND direction='in' "
                       "ORDER BY created_at,message_id", (account, fan)).fetchall()
    return [(_naive(created), body or "") for created, body in rows if _naive(created) > since]


def post_buy_run_at(account, fan, offer_id, now):
    """FASTT's own delay: ``uniform(90, 240)`` seconds, seeded exactly as ``ai_chatter`` seeds it."""
    return now + timedelta(seconds=random.Random(f"postbuy:{account}:{fan}:{offer_id}").uniform(*POST_BUY_WINDOW_S))

