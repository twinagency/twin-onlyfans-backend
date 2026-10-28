"""Canonical snapshot adapter for Fastt's existing sales policy and prices.

The same reply invocation may select one existing, described catalogue single.
There is no resolver model, scheduler, provider client or independent send path.
Unknown/unsupported configuration closes selling while text replies continue.

Every sale carries a versioned, source-backed BASIS (contract v5): a direct
request, the acceptance of one specific earlier offer, or a policy decision of
Fastt's own seller. The model only proposes; `standing` decides which bases are
permitted and `current_sale` re-derives them from live state before dispatch.
A saved sale without a basis is a v4 direct-request sale and is read ONLY by
`legacy_current_sale`, under exactly the rules it was created under.
"""
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import func, select

from db.models import (AccountAiConfig, CatalogItem, Fan, LadderState, Message, Transaction,
    CATALOG_IS_SINGLE)
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfNativePlan, OfTextIntent
from .media_delivery import assert_unowned, catalog_terms
from .native_context import canonical
from .store import Refused

MAX_CANDIDATES = 12
BASIS_VERSION = 1
DIRECT, ACCEPTED, POLICY = "direct_request", "accepted_offer", "policy_offer"
TEXT_OFFER = "text_offer"            # proposal only: offer one item in words, no card yet
KINDS = (DIRECT, ACCEPTED, POLICY)
ACCEPT_WINDOW = timedelta(hours=24)  # how long a verbal offer can still be accepted
# Keys that describe WHY a sale was made, not WHAT is sold. They never join the
# catalogue-terms comparison (media_delivery.terms_match strips the same set).
BASIS_KEYS = frozenset({"native_quote", "source_quote", "source_message_id", "basis"})


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


def _recent(s, account_id, fan_id):
    rows = s.execute(select(Message.message_id, Message.direction, Message.body, Message.created_at).where(
        Message.account_id == account_id, Message.fan_id == fan_id, Message.is_unsent.is_(False))
        .order_by(Message.created_at.desc(), Message.message_id.desc()).limit(20)
        .with_for_update(read=True)).all()
    return list(reversed(rows))


def _unresolved_prebinding(s, account_id, fan_id):
    """Ledger purchases by this fan with no message id: which media he owns is UNKNOWN."""
    from .ledger import PAID
    return s.scalar(select(Transaction.id).where(Transaction.account_id == account_id,
        Transaction.fan_id == fan_id, Transaction.source == "ledger", Transaction.kind == "ppv_message",
        Transaction.message_id.is_(None), Transaction.status.in_(tuple(PAID))).limit(1)) is not None


def fingerprint(terms):
    """Stable identity of the sold terms, independent of why they were chosen."""
    body = {k: v for k, v in terms.items() if k not in BASIS_KEYS}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def _prior_offer(s, account_id, fan_id, msgs, now, lang):
    """The ONE earlier offer of ours his latest message accepts, or None.

    Identifiable means our newest message is an accepted native text whose plan
    recorded the single item it offered in words; everything after it is his, and
    all of it is a bare yes. Anything else (a refusal, other chat, a newer message
    of ours, an expired or unrecorded offer) is not an acceptance."""
    from automations import _sell_signal
    last_out = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].direction == "out"), None)
    if last_out is None or last_out == len(msgs) - 1:
        return None
    after = msgs[last_out + 1:]
    if (not all(m.direction == "in" and _sell_signal.is_affirmation(m.body, lang) for m in after)
            or now - msgs[last_out].created_at > ACCEPT_WINDOW or msgs[last_out].created_at > now):
        return None
    intent = s.scalar(select(OfTextIntent).where(OfTextIntent.account_id == account_id,
        OfTextIntent.fan_id == fan_id, OfTextIntent.provider_message_id == msgs[last_out].message_id,
        OfTextIntent.status == "accepted", OfTextIntent.native_plan_id.is_not(None)).with_for_update(read=True))
    plan = s.get(OfNativePlan, (account_id, intent.native_plan_id)) if intent is not None else None
    try:
        verbal = json.loads(plan.sale_json)["verbal_offer"] if plan is not None and plan.sale_json else None
        if verbal is None or verbal["kind"] != TEXT_OFFER or verbal["v"] != BASIS_VERSION:
            return None
        return {"message_id": msgs[last_out].message_id, "intent_id": intent.intent_id,
                "plan_id": plan.plan_id, "item_id": int(verbal["item_id"]),
                "terms_fingerprint": verbal["terms_fingerprint"]}
    except (ValueError, TypeError, KeyError):
        return None


def standing(s, account_id, fan_id, now, *, own_intent_id=None):
    """Which sales bases Fastt's own policy permits for this fan right now.

    Reads under the account lock. Returns {"kinds": {kind: facts}, "reason",
    "prior", "source_message_id", "turn_kind"}; no permitted kind means no sale.
    Reuses the seller's brakes, caps and qualification gate unchanged; only the
    source of the "ask" differs per kind."""
    from automations import ai_chatter as native, sell_lane, _language
    from automations import _sell_signal
    from automations._common import thread_heat
    out = {"kinds": {}, "reason": "", "prior": None, "source_message_id": None, "turn_kind": ""}
    def refuse(reason):
        out["reason"] = reason
        return out
    policy = s.get(OfDeliveryPolicy, account_id)
    if policy is None or not policy.media_enabled:
        return refuse("media_disabled")
    from .commerce import held
    if held(s, account_id):
        return refuse("commerce_evidence_conflict")
    cfg_row = s.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id == account_id).with_for_update(read=True))
    try:
        cfg = _config(cfg_row)
    except Refused:
        return refuse("sales_configuration_refused")
    fan = s.scalar(select(Fan).where(Fan.account_id == account_id, Fan.fan_id == fan_id).with_for_update(read=True))
    lad = s.scalar(select(LadderState).where(LadderState.account_id == account_id, LadderState.fan_id == fan_id).with_for_update(read=True))
    msgs = _recent(s, account_id, fan_id)
    latest = msgs[-1] if msgs else None
    if latest is None or latest.direction != "in":
        return refuse("no_current_ask")
    out["source_message_id"] = latest.message_id
    lang = _language.resolve_language(cfg_row.language, fan.language if fan else None)
    if _language.detect_bot_accusation(latest.body, lang):
        return refuse("identity_turn")
    lane = sell_lane.SellLane(account_id, "onlyfans-native", cfg)
    turn = sell_lane.Turn(latest.body, at=latest.created_at, fan_spoke_last=True, lang=lang)
    wanted = {}
    if lane.is_ask(turn):
        wanted[DIRECT] = turn
    prior = _prior_offer(s, account_id, fan_id, msgs, now, lang)
    if prior is not None:
        out["prior"] = prior
        wanted[ACCEPTED] = sell_lane.Turn(latest.body, at=latest.created_at, fan_spoke_last=True,
            lang=lang, wide_ask=_sell_signal.WIDE_AFFIRMATION)
    # Fastt's own proactive decision, from the shared turn evaluator. The picture
    # beats need image analysis, which is unknown here and therefore off.
    inbound = sum(m.direction == "in" for m in msgs)
    _, _, pitch = native.decide_pitch_turn(can_sell=True, last_body=latest.body, fan_lang=lang,
        pivot_on_escalation=bool(cfg.get("pivot_on_escalation")), fan_msg_n=inbound,
        esc_min_msgs=int(cfg.get("min_fan_msgs_before_escalation_pitch") or 0), bot_accused=False,
        pic_desc="", image_dare=False, dare_callback=False, pic_offer=False,
        hot_thread=thread_heat([(m.direction, m.body) for m in msgs]))
    out["turn_kind"] = pitch
    if pitch == native._TURN_ESCALATION or (pitch == native._TURN_HOT and bool(cfg.get("force_ask"))):
        wanted[POLICY] = turn
    if not wanted:
        return refuse("no_current_ask")
    brake = sell_lane.fan_brake(fan, turn, lad, now)
    if brake is not None:
        return refuse(brake.reason)
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
        return refuse(verdict.reason)
    by_fan, hour, day = native._ask_counters_in_session(s, account_id, now)
    cnt = sell_lane.AskCounters(asks_today=by_fan.get(fan_id, 0) + sum(fid == fan_id for fid, _ in reservations),
        account_hour=hour + len(reservations), account_day=day + len(reservations))
    human_ask = s.scalar(select(func.max(Message.created_at)).where(Message.account_id == account_id,
        Message.fan_id == fan_id, Message.direction == "out", Message.price_cents > 0,
        Message.automation_kind.is_(None)))
    unresolved = _unresolved_prebinding(s, account_id, fan_id)
    facts = {"asks_today": cnt.asks_today, "account_hour": cnt.account_hour, "account_day": cnt.account_day,
             "open_offers": len(open_offers), "reserved": sum(fid == fan_id for fid, _ in reservations),
             "ownership": "unresolved_prebinding" if unresolved else "clear"}
    why_not = ""
    for kind, t in wanted.items():
        if kind == POLICY and unresolved:
            # Unknown ownership never reads as "never bought": no proactive offer.
            why_not = "ownership_unresolved"
            continue
        ok, why = sell_lane.qualify_snapshot(cfg, fan_id, t, lad, cnt, human_ask, now)
        if ok:
            out["kinds"][kind] = dict(facts)
        else:
            why_not = why
    if not out["kinds"]:
        return refuse(why_not or "not_qualified")
    return out


def eligible(s, account_id, fan_id, now, *, own_intent_id=None):
    """(True, why) when ANY sales basis is currently permitted."""
    st = standing(s, account_id, fan_id, now, own_intent_id=own_intent_id)
    return (True, "ok") if st["kinds"] else (False, st["reason"] or "not_eligible")


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


def shelf(s, account_id, fan_id, now, *, input_revision=0, control_revision=0, expires_at=None):
    """(sales, policy): bounded eligible singles plus the bases permitted for them.

    No catalogue writes and no extra model calls. `policy` is part of the prompt
    context and the only authority `selected` consults for the proposal's basis."""
    policy = {"v": BASIS_VERSION, "kinds": [], "prior_offer": None, "source_message_id": None,
              "turn_kind": "", "facts": {}, "policy_revision": 0, "input_revision": input_revision,
              "control_revision": control_revision,
              "expires_at": expires_at.isoformat() if expires_at is not None else None}
    try:
        st = standing(s, account_id, fan_id, now)
        delivery = s.get(OfDeliveryPolicy, account_id)
        policy.update(kinds=list(st["kinds"]), prior_offer=st["prior"], facts=st["kinds"],
            source_message_id=st["source_message_id"], turn_kind=st["turn_kind"],
            policy_revision=delivery.revision if delivery is not None else 0)
        if not st["kinds"]:
            return [], policy
        prior = st["prior"] if ACCEPTED in st["kinds"] else None
        if st["kinds"].keys() == {ACCEPTED}:
            ids = [prior["item_id"]]
        else:
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
                if prior is not None and item_id == prior["item_id"] and fingerprint(candidate) != prior["terms_fingerprint"]:
                    # The offered item changed since we offered it: not the same offer.
                    policy["kinds"].remove(ACCEPTED)
                    policy["prior_offer"] = None
                    if not policy["kinds"]:
                        return [], policy
                    continue
                result.append(candidate)
            except Refused:
                continue
            if len(result) == MAX_CANDIDATES:
                break
        if ACCEPTED in policy["kinds"] and not any(x["item"]["id"] == prior["item_id"] for x in result):
            policy["kinds"].remove(ACCEPTED)
            policy["prior_offer"] = None
        return (result if policy["kinds"] else []), policy
    except (Refused, ValueError, TypeError, KeyError, OverflowError):
        return [], {**policy, "kinds": [], "prior_offer": None}


def candidates(s, account_id, fan_id, now):
    return shelf(s, account_id, fan_id, now)[0]


def selected(proposal, context):
    """Only an item offered to this exact invocation, on a basis its policy permits."""
    if proposal is None:
        return None
    policy = context.get("sales_policy")
    if (type(proposal) is not dict or set(proposal) != {"item_id", "basis_kind", "quote"}
            or type(policy) is not dict or policy.get("v") != BASIS_VERSION):
        raise Refused("invalid native sale proposal")
    kind, quote, item_id = proposal["basis_kind"], proposal["quote"], proposal["item_id"]
    allowed = set(policy["kinds"]) | ({TEXT_OFFER} if POLICY in policy["kinds"] else set())
    latest = context["messages"][-1]
    if type(item_id) is not int or type(kind) is not str or kind not in allowed:
        raise Refused("native sale basis is not permitted")
    if policy["source_message_id"] != latest["message_id"]:
        raise Refused("native sale basis is not current")
    if kind == DIRECT:
        if (type(quote) is not str or not quote.strip() or len(quote) > 500 or quote not in latest["body"]):
            raise Refused("native sale requires its current request")
    elif quote is not None:
        raise Refused("native sale basis takes no quote")
    prior = policy["prior_offer"] if kind == ACCEPTED else None
    if kind == ACCEPTED and (prior is None or prior["item_id"] != item_id):
        raise Refused("native acceptance does not match its offer")
    matches = [x for x in context.get("sales", []) if x["item"]["id"] == item_id]
    if len(matches) != 1:
        raise Refused("native sale is outside this invocation's shelf")
    terms = matches[0]
    facts = policy["facts"].get(POLICY if kind == TEXT_OFFER else kind)
    basis = {"v": BASIS_VERSION, "kind": kind, "source_message_id": latest["message_id"],
        "source_quote": quote, "prior_offer": prior, "item_id": item_id,
        "terms_fingerprint": fingerprint(terms), "media_ids": terms["media_ids"],
        "preview_ids": terms["preview_ids"], "currency": "USD", "price_cents": terms["price_cents"],
        "policy_revision": policy["policy_revision"], "input_revision": policy["input_revision"],
        "control_revision": policy["control_revision"], "expires_at": policy["expires_at"],
        "turn_kind": policy["turn_kind"], "facts": facts}
    return {**terms, "basis": basis}


def _legacy_source_ok(s, account_id, fan_id, saved):
    source = s.scalar(select(Message).where(Message.account_id == account_id, Message.fan_id == fan_id,
        Message.message_id == saved["source_message_id"]).with_for_update(read=True))
    return not (source is None or source.is_unsent or source.direction != "in"
                or saved["source_quote"] not in source.body)


def legacy_current_sale(s, account_id, fan_id, saved, now, *, own_intent_id=None):
    """Contract v4: a sale with no basis was a direct request. Read ONLY under the
    rules it was created under; it never acquires a v5 basis."""
    try:
        if DIRECT not in standing(s, account_id, fan_id, now, own_intent_id=own_intent_id)["kinds"]:
            return False
        current = choice(s, account_id, fan_id, saved["item"]["id"], now)
        if not _legacy_source_ok(s, account_id, fan_id, saved):
            return False
        return saved == {**current, "source_message_id": saved["source_message_id"], "source_quote": saved["source_quote"]}
    except (Refused, ValueError, TypeError, KeyError, OverflowError):
        return False


def current_sale(s, account_id, fan_id, saved, now, *, own_intent_id=None):
    """Re-derive the sale's basis and terms from live state; any drift is False."""
    if type(saved) is not dict:
        return False
    basis = saved.get("basis")
    if basis is None:
        return legacy_current_sale(s, account_id, fan_id, saved, now, own_intent_id=own_intent_id)
    try:
        if (type(basis) is not dict or basis["v"] != BASIS_VERSION or basis["kind"] not in KINDS
                or basis["currency"] != "USD"):
            return False
        if basis["expires_at"] is not None and now > datetime.fromisoformat(basis["expires_at"]):
            return False
        st = standing(s, account_id, fan_id, now, own_intent_id=own_intent_id)
        facts = st["kinds"].get(basis["kind"])
        if (facts is None or st["source_message_id"] != basis["source_message_id"]
                or facts["ownership"] != basis["facts"]["ownership"]):
            return False
        if basis["kind"] == DIRECT:
            if not _legacy_source_ok(s, account_id, fan_id,
                    {"source_message_id": basis["source_message_id"], "source_quote": basis["source_quote"] or ""}) or not basis["source_quote"]:
                return False
        elif basis["kind"] == ACCEPTED:
            if basis["prior_offer"] is None or st["prior"] != basis["prior_offer"]:
                return False
        elif st["turn_kind"] != basis["turn_kind"]:
            return False
        current = choice(s, account_id, fan_id, saved["item"]["id"], now)
        if (fingerprint(current) != basis["terms_fingerprint"] or current["price_cents"] != basis["price_cents"]
                or basis["item_id"] != saved["item"]["id"]):
            return False
        return {k: v for k, v in saved.items() if k != "basis"} == current
    except (Refused, ValueError, TypeError, KeyError, OverflowError):
        return False


def matches(s, intent, now):
    try:
        saved = json.loads(intent.catalog_snapshot_json)
        basis = saved.get("basis")
        if basis is not None and (basis.get("input_revision") != intent.input_revision
                or basis.get("control_revision") != intent.control_revision
                or basis.get("policy_revision") != intent.policy_revision):
            return False
        return current_sale(s, intent.account_id, intent.fan_id, saved, now, own_intent_id=intent.intent_id)
    except (ValueError, TypeError, AttributeError):
        return False


def requote(s, account_id, fan_id, item_id, saved, now):
    """The exact terms to queue for a saved sale, or Refused when it no longer holds."""
    if not current_sale(s, account_id, fan_id, saved, now):
        raise Refused("native sales currently held")
    terms = choice(s, account_id, fan_id, item_id, now)
    if "basis" in saved:
        return {**terms, "basis": saved["basis"]}
    return {**terms, "source_quote": saved["source_quote"], "source_message_id": saved["source_message_id"]}


def offer_holds(s, account_id, fan_id, basis, now):
    """A verbal-only offer is recorded only while the policy still permits it."""
    try:
        st = standing(s, account_id, fan_id, now)
        return (POLICY in st["kinds"] and st["source_message_id"] == basis["source_message_id"]
                and basis["kind"] == TEXT_OFFER and st["turn_kind"] == basis["turn_kind"])
    except (Refused, ValueError, TypeError, KeyError):
        return False


def offered_in_text(sale):
    """The sale dict a plan stores for a verbal-only offer (no card), or None."""
    return None if sale is None else {"verbal_offer": sale["basis"]}


def card_sale_json(sale_json):
    """The stored card sale text itself (not parsed), or None."""
    return sale_json if card_sale(sale_json) is not None else None


def card_sale(sale_json):
    """The card sale a plan carries, or None for no sale / a verbal-only offer."""
    if not sale_json:
        return None
    sale = json.loads(sale_json)
    return None if "verbal_offer" in sale else sale


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
    fixed = terms["item"].get("pricing_mode") == "fixed" and intent.origin in {"automated", "engagement"}
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
