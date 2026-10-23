"""Canonical snapshot adapter for Fastt's existing sales policy and prices.

The same reply invocation may select one existing, described catalogue single.
There is no resolver model, scheduler, provider client or independent send path.
Unknown/unsupported configuration closes selling while text replies continue.
"""
from dataclasses import asdict
from datetime import timedelta
import json

from sqlalchemy import func, select

from db.models import AccountAiConfig, CatalogItem, Fan, LadderState, Message, CATALOG_IS_SINGLE
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from .media_delivery import assert_unowned, catalog_terms
from .native_context import canonical
from .store import Refused

MAX_CANDIDATES = 12


def _config(row):
    from automations.ai_chatter import config_from_row
    from .native_memory import _unique
    if row is None or not row.ai_chatter_config_json:
        raise Refused("saved native sales configuration required")
    try:
        raw = json.loads(row.ai_chatter_config_json, object_pairs_hook=_unique)
    except (ValueError, TypeError) as exc:
        raise Refused("invalid saved native sales configuration") from exc
    # Dedicated account and media policy permission is explicit. Saved legacy
    # defaults cannot silently arm this new delivery capability.
    if type(raw) is not dict or any(raw.get(k) is not True for k in ("pack_send_enabled", "pack_on_ask_enabled", "qualification_gate_enabled")):
        raise Refused("native on-request sales disabled")
    return config_from_row(row)


def eligible(s, account_id, fan_id, now, *, own_intent_id=None):
    """The existing SellLane brakes/qualification, read in the account lock."""
    from automations import ai_chatter as native, sell_lane, _language
    policy = s.get(OfDeliveryPolicy, account_id)
    if policy is None or not policy.media_enabled:
        return False, "media_disabled"
    from .commerce import held
    if held(s, account_id):
        return False, "commerce_evidence_conflict"
    cfg_row = s.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id == account_id).with_for_update(read=True))
    try:
        cfg = _config(cfg_row)
    except Refused:
        return False, "sales_configuration_refused"
    fan = s.scalar(select(Fan).where(Fan.account_id == account_id, Fan.fan_id == fan_id).with_for_update(read=True))
    lad = s.scalar(select(LadderState).where(LadderState.account_id == account_id, LadderState.fan_id == fan_id).with_for_update(read=True))
    latest = s.scalar(select(Message).where(Message.account_id == account_id, Message.fan_id == fan_id,
        Message.is_unsent.is_(False)).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(1).with_for_update(read=True))
    if latest is None or latest.direction != "in":
        return False, "no_current_ask"
    lang = _language.resolve_language(cfg_row.language, fan.language if fan else None)
    # Initial commerce scope is a direct request. The wider/proactive readers
    # and inferred assent do not acquire new send rights here.
    turn = sell_lane.Turn(latest.body, at=latest.created_at, fan_spoke_last=True, lang=lang)
    if (not sell_lane.SellLane(account_id, "onlyfans-native", cfg).is_ask(turn)
            or _language.detect_bot_accusation(latest.body, lang)):
        return False, "no_current_ask"
    brake = sell_lane.fan_brake(fan, turn, lad, now)
    if brake is not None:
        return False, brake.reason
    pending = select(OfTextIntent.fan_id, OfTextIntent.created_at).where(
        OfTextIntent.account_id == account_id, OfTextIntent.catalog_item_id.is_not(None),
        OfTextIntent.status.in_(("queued", "dispatching", "unknown")))
    if own_intent_id is not None:
        pending = pending.where(OfTextIntent.intent_id != own_intent_id)
    reservations = s.execute(pending).all()
    open_offers = native._open_offers_in_session(s, account_id, fan_id)
    unlocked = bool(open_offers) and native._unlocked_since_open_offers_in_session(s, account_id, fan_id, open_offers)
    verdict = sell_lane.money_brake(cfg, native._paid_cents_7d_in_session(s, account_id, fan_id, now),
        len(open_offers) + sum(fid == fan_id for fid, _ in reservations), unlocked,
        native._offer_caps_ok_in_session(s, account_id, fan_id, cfg, now=now))
    if not verdict:
        return False, verdict.reason
    by_fan, hour, day = native._ask_counters_in_session(s, account_id, now)
    cnt = sell_lane.AskCounters(asks_today=by_fan.get(fan_id, 0) + sum(fid == fan_id for fid, _ in reservations),
        account_hour=hour + len(reservations), account_day=day + len(reservations))
    human_ask = s.scalar(select(func.max(Message.created_at)).where(Message.account_id == account_id,
        Message.fan_id == fan_id, Message.direction == "out", Message.price_cents > 0,
        Message.automation_kind.is_(None)))
    # The canonical read supplies the same pure qualification decision. A
    # teammate's priced message still paces the native seller.
    return sell_lane.qualify_snapshot(cfg, fan_id, turn, lad, cnt, human_ask, now)


def choice(s, account_id, fan_id, item_id, now):
    """Existing native quote arithmetic, never a model-authored price."""
    from automations import ai_chatter as native, upsell
    cfg_row = s.get(AccountAiConfig, account_id)
    cfg = _config(cfg_row)
    terms = catalog_terms(s, account_id, item_id)
    assert_unowned(s, account_id, fan_id, terms)
    item = s.get(CatalogItem, item_id)
    if item.pricing_mode == "fixed":
        # The saved amount is the offer. Policies may refuse it; nothing here
        # may reprice it, and no dynamic quote arithmetic applies.
        return terms
    biggest, latest = native._paid_ppv_facts_in_session(s, account_id, fan_id)
    fan = upsell.FanState(fan_id=fan_id, max_single_paid_cents=biggest,
        last_paid_cents=latest or None, has_ever_paid=bool(biggest))
    lad = s.get(LadderState, (account_id, fan_id))
    rung = int(lad.rung_index or 0) if lad else 0
    asks, median, bounds = native._price_context_in_session(s, account_id, cfg_row)
    quote = native._quote_item(account_id, fan, item, rung_index=rung,
        media_asks=asks, median=median, bounds=bounds, escalation_mult=cfg["escalation_mult"],
        max_ask_vs_history_mult=cfg["max_ask_history_mult"],
        proven_floor_cents=native._proven_floor_cents(fan, cfg))
    if quote is None:
        raise Refused("native price policy refused this item")
    terms["price_cents"] = quote.price_cents
    terms["native_quote"] = {**asdict(quote), "rung_index": rung}
    return terms


def candidates(s, account_id, fan_id, now):
    """Bounded eligible singles; no catalogue writes or extra model calls."""
    try:
        if not eligible(s, account_id, fan_id, now)[0]:
            return []
        # Read at most 48 rows, retaining at most 12 bounded descriptions. A
        # refusal never paginates the entire vault inside a reply claim.
        ids = s.scalars(select(CatalogItem.id).where(CatalogItem.account_id == account_id,
            CATALOG_IS_SINGLE, CatalogItem.enabled.is_(True), CatalogItem.is_free_teaser.is_(False))
            .order_by(CatalogItem.id).limit(MAX_CANDIDATES * 4)).all()
        result = []
        for item_id in ids:
            try:
                candidate = choice(s, account_id, fan_id, item_id, now)
                if len(canonical(result + [candidate]).encode()) > 16384:
                    break
                result.append(candidate)
            except Refused:
                continue
            if len(result) == MAX_CANDIDATES:
                break
        return result
    except (Refused, ValueError, TypeError, KeyError, OverflowError):
        return []


def selected(proposal, context):
    """Only an item offered to this exact invocation and a current source quote."""
    if proposal is None:
        return None
    if type(proposal) is not dict or set(proposal) != {"item_id", "quote"}:
        raise Refused("invalid native sale proposal")
    quote = proposal["quote"]
    if (type(proposal["item_id"]) is not int or type(quote) is not str or not quote.strip()
            or len(quote) > 500 or quote not in context["messages"][-1]["body"]):
        raise Refused("native sale requires its current request")
    matches = [x for x in context.get("sales", []) if x["item"]["id"] == proposal["item_id"]]
    if len(matches) != 1:
        raise Refused("native sale is outside this invocation's shelf")
    return {**matches[0], "source_quote": quote,
            "source_message_id": context["messages"][-1]["message_id"]}


def current_sale(s, account_id, fan_id, saved, now, *, own_intent_id=None):
    try:
        if not eligible(s, account_id, fan_id, now, own_intent_id=own_intent_id)[0]:
            return False
        current = choice(s, account_id, fan_id, saved["item"]["id"], now)
        source = s.scalar(select(Message).where(Message.account_id == account_id, Message.fan_id == fan_id,
            Message.message_id == saved["source_message_id"]).with_for_update(read=True))
        if source is None or source.is_unsent or source.direction != "in" or saved["source_quote"] not in source.body:
            return False
        return saved == {**current, "source_message_id": saved["source_message_id"], "source_quote": saved["source_quote"]}
    except (Refused, ValueError, TypeError, KeyError, OverflowError):
        return False


def matches(s, intent, now):
    try:
        return current_sale(s, intent.account_id, intent.fan_id, json.loads(intent.catalog_snapshot_json),
            now, own_intent_id=intent.intent_id)
    except (ValueError, TypeError):
        return False


def caption(terms):
    from automations.pack_claim import _media_phrase
    kinds = {x["media_id"]: x["kind"] for x in terms["files"]}
    phrase, _ = _media_phrase([kinds[mid] for mid in terms["media_ids"]])
    cents = terms["price_cents"]
    return f'{phrase}: {terms["item"]["description_for_ai"]}\nUSD ${cents // 100}.{cents % 100:02d} to unlock.'


def record_quote(s, intent, message_id, now):
    """Same accepted-quote ledger; no exposure is counted before acceptance."""
    from db.models import LadderQuote
    from automations import upsell
    terms = json.loads(intent.catalog_snapshot_json)
    q = terms.get("native_quote")
    # Fixed offers are recorded where the seller would record a quote (its own
    # automated and engagement sends); manual and campaign sends keep their existing ledger.
    fixed = terms["item"].get("pricing_mode") == "fixed" and intent.origin in {"automated", "engagement", "fastt"}
    if q is None and not fixed:
        return
    lad = s.get(LadderState, (intent.account_id, intent.fan_id))
    if fixed:
        # Accepted fixed offers count as asks/offers and stay payable through
        # the same quote row, but carry no derived band and never move the
        # dynamic ladder or enter its price experiments.
        price = terms["price_cents"]
        q = {"rung_index": int(lad.rung_index or 0) if lad else 0, "price_cents": price, "base_cents": price,
             "arm_mult": 1.0, "pre_clamp_cents": price, "clamped_by": None, "band_lo": 0, "band_hi": 0}
    s.add(LadderQuote(account_id=intent.account_id, fan_id=intent.fan_id,
        item_id=intent.catalog_item_id, message_id=message_id, media_key=upsell.media_key(terms["media_ids"]),
        kind="fixed" if fixed else "rung", sent_at=now, paid=False, **q))
    if lad is None:
        lad = LadderState(account_id=intent.account_id, fan_id=intent.fan_id)
        s.add(lad)
    lad.last_ask_at, lad.updated_at = now, now
    if not fixed:
        lad.rung_index = max(int(lad.rung_index or 0), q["rung_index"] + 1)
    # Receipt projection preserves independent human stop/pause/companion state.
    if lad.status in {None, upsell.STATUS_IDLE, upsell.STATUS_HOT, upsell.STATUS_OPEN}:
        lad.status = upsell.STATUS_OPEN
