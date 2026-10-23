"""Account-locked canonical reads. No provider polling or ownership writes."""
from datetime import timedelta
from types import SimpleNamespace
import re

from sqlalchemy import func, select
from db.models import (AccountAiConfig, CatalogItem, ContentOffer, Fan,
                       FanSafetyFlag, LadderState, Message, OfConversationState, OfCreatorBinding,
                       Transaction, VaultSend)
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from ..media_delivery import assert_unowned, catalog_terms
from ..store import Refused
from .contracts import digest, load_object


def latest(s, account, fan, direction=None):
    q = select(Message).where(Message.account_id == account, Message.fan_id == fan,
                              Message.is_unsent.is_(False))
    if direction:
        q = q.where(Message.direction == direction)
    return s.scalar(q.order_by(Message.created_at.desc(), Message.message_id.desc()).limit(1).with_for_update(read=True))


def context(s, runtime, fan_id, now, authority, *, own_intent=None):
    account = runtime.account_id
    from onlyfans_read.agency_scope import creator_allowed
    binding = s.scalar(select(OfCreatorBinding).where(OfCreatorBinding.id == runtime.binding_id,
        OfCreatorBinding.account_id == account, OfCreatorBinding.channel == "onlyfans",
        OfCreatorBinding.revoked_at.is_(None)).with_for_update(read=True))
    if binding is None or not creator_allowed(binding.creator_id):
        raise Refused("scope_unavailable")
    fan = s.scalar(select(Fan).where(Fan.account_id == account, Fan.fan_id == fan_id).with_for_update(read=True))
    state = s.scalar(select(OfConversationState).where(OfConversationState.account_id == account,
        OfConversationState.fan_id == fan_id).with_for_update(read=True))
    policy = s.get(OfDeliveryPolicy, account)
    reason = authority._eligibility(s, runtime, policy, state, fan, "campaign", now,
                                  own_attempt_id=own_intent.attempt_id if own_intent else None)
    if reason:
        raise Refused(reason)
    if fan.automation_paused_until is not None and fan.automation_paused_until > now:
        raise Refused("fan_automation_paused")
    if s.scalar(select(FanSafetyFlag.id).where(FanSafetyFlag.account_id == account,
            FanSafetyFlag.fan_id == fan_id, FanSafetyFlag.severity == "critical").limit(1)):
        raise Refused("fan_safety_hold")
    pending = select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account,
        OfTextIntent.fan_id == fan_id, OfTextIntent.status.in_(("queued", "dispatching", "unknown")))
    if own_intent:
        pending = pending.where(OfTextIntent.intent_id != own_intent.intent_id)
    if s.scalar(pending.limit(1)):
        raise Refused("conversation_delivery_pending")
    newest, inbound = latest(s, account, fan_id), latest(s, account, fan_id, "in")
    if newest is None or inbound is None or newest.direction != "out":
        raise Refused("reactive_reply_has_priority")
    if newest.created_at > now or inbound.created_at > now:
        raise Refused("history_timestamp_invalid")
    # A bounded fingerprint detects new purchases, reversals, edits and ledger
    # links even if a writer does not advance input_revision for commerce.
    purchases = s.execute(select(Transaction.id, Transaction.kind, Transaction.message_id,
        Transaction.amount_cents, Transaction.currency, Transaction.status, Transaction.occurred_at)
        .where(Transaction.account_id == account, Transaction.fan_id == fan_id)
        .order_by(Transaction.id).limit(2001).with_for_update(read=True)).all()
    if len(purchases) > 2000:
        raise Refused("purchase_snapshot_too_large")
    cfg = s.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id == account).with_for_update(read=True))
    lad = s.scalar(select(LadderState).where(LadderState.account_id == account,
        LadderState.fan_id == fan_id).with_for_update(read=True))
    snapshot = {"binding": runtime.binding_id, "control": state.revision,
        "input": state.input_revision, "delivery": policy.revision,
        "latest": [newest.message_id, newest.created_at, newest.body, newest.price_cents, newest.is_paid],
        "inbound": [inbound.message_id, inbound.created_at, inbound.body],
        "preferences": [fan.fetishes, fan.likes_boobs, fan.likes_ass, fan.language],
        "purchases": [list(x) for x in purchases],
        "sales_config": cfg.ai_chatter_config_json if cfg else None,
        "ladder": None if lad is None else [lad.status, lad.last_ask_at, lad.last_paid_at,
            lad.offers_paused_until, lad.cooldown_until, lad.companion_until]}
    return fan, newest, inbound, digest(snapshot)


def frequency(s, account, fan_id, policy, now, *, own_intent=None):
    """All outgoing messages and outstanding reservations count, across lanes.

    Accepted intents not yet projected count too, closing the receipt/project
    crash gap. Future scheduled actions are not messages and consume no quota.
    """
    messages = select(func.count()).select_from(Message).where(
        Message.account_id == account, Message.direction == "out")
    intents = select(func.count()).select_from(OfTextIntent).where(OfTextIntent.account_id == account,
        ((OfTextIntent.status.in_(("queued", "dispatching", "unknown"))) |
         ((OfTextIntent.status == "accepted") & OfTextIntent.projected_at.is_(None))))
    if own_intent:
        intents = intents.where(OfTextIntent.intent_id != own_intent.intent_id)
    # Aggregate in SQL; do not load a busy account's entire outgoing history.
    for hours, per_fan, limit in ((24, True, policy.fan_daily_limit), (168, True, policy.fan_weekly_limit),
                                  (1, False, policy.account_hourly_limit), (24, False, policy.account_daily_limit)):
        mq, iq = messages.where(Message.created_at > now - timedelta(hours=hours)), intents
        if per_fan:
            mq, iq = mq.where(Message.fan_id == fan_id), iq.where(OfTextIntent.fan_id == fan_id)
        if s.scalar(mq) + s.scalar(iq) >= limit:
            raise Refused("frequency_limit")


def selling(s, account, fan, inbound, policy, now, *, own_intent=None):
    from automations import ai_chatter as native, sell_lane, upsell, _language
    from ..native_sales import _config
    cfg_row = s.get(AccountAiConfig, account)
    cfg = _config(cfg_row)
    delivery = s.get(OfDeliveryPolicy, account)
    if not delivery.media_enabled:
        raise Refused("media_disabled")
    lad = s.get(LadderState, (account, fan.fan_id))
    # Reuse the existing tapped TTL without mutating shared ladder state. No
    # explicit stop, objection, companion or unexpired pause is cleared here.
    if (lad and lad.status == upsell.STATUS_TAPPED and lad.last_ask_at is not None
            and lad.last_ask_at <= now - timedelta(hours=max(upsell.TAPPED_TTL_H, policy.unpaid_cooldown_hours))):
        lad = SimpleNamespace(**{c.name: getattr(lad, c.name) for c in LadderState.__table__.columns})
        lad.status = upsell.STATUS_IDLE
    lang = _language.resolve_language(cfg_row.language, fan.language)
    turn = sell_lane.Turn(inbound.body, at=inbound.created_at, fan_spoke_last=False, lang=lang)
    brake = sell_lane.fan_brake(fan, turn, lad, now)
    if brake is not None:
        raise Refused(brake.reason)
    if _language.detect_bot_accusation(inbound.body or "", lang):
        raise Refused("bot_accusation")
    # Proactive work must never use an old fan-pull quote to lift a live pause.
    if lad and lad.offers_paused_until and lad.offers_paused_until > now:
        raise Refused("offers_paused")
    open_count = s.scalar(select(func.count()).select_from(ContentOffer).where(
        ContentOffer.account_id == account, ContentOffer.fan_id == fan.fan_id,
        ContentOffer.status == "open", ContentOffer.offered_at > now - timedelta(hours=policy.unpaid_cooldown_hours)))
    pending_query = select(func.count()).select_from(OfTextIntent).where(
        OfTextIntent.account_id == account, OfTextIntent.fan_id == fan.fan_id,
        OfTextIntent.catalog_item_id.is_not(None), OfTextIntent.status.in_(("queued", "dispatching", "unknown")))
    if own_intent:
        pending_query = pending_query.where(OfTextIntent.intent_id != own_intent.intent_id)
    pending = s.scalar(pending_query)
    verdict = sell_lane.money_brake(cfg, native._paid_cents_7d_in_session(s, account, fan.fan_id, now),
        open_count, False, native._offer_caps_ok_in_session(s, account, fan.fan_id, cfg, now=now))
    if not verdict:
        raise Refused(verdict.reason)
    # Any recent unpaid offer cools this lane, even below shared open-count cap.
    if open_count:
        raise Refused("unpaid_offer_cooldown")
    by_fan, hour, day = native._ask_counters_in_session(s, account, now)
    if (by_fan.get(fan.fan_id, 0) + pending >= upsell.MAX_ASKS_PER_FAN_PER_DAY
            or hour >= upsell.LADDER_OFFERS_PER_HOUR_MAX or day >= upsell.LADDER_OFFERS_PER_DAY_MAX):
        raise Refused("shared_sales_cap")


def candidates(s, account, fan, policy, now):
    """Fixed prices only. Preference/history affect rank, never the terms."""
    words = set(re.findall(r"\w+", (fan.fetishes or "").lower()))
    if fan.likes_boobs:
        words.add("boobs")
    if fan.likes_ass:
        words.add("ass")
    from automations.ai_chatter import _paid_ppv_facts_in_session
    _, recent_price = _paid_ppv_facts_in_session(s, account, fan.fan_id)
    rows = s.scalars(select(CatalogItem).where(CatalogItem.account_id == account,
        CatalogItem.enabled.is_(True), CatalogItem.script_id.is_(None),
        CatalogItem.pricing_mode == "fixed", CatalogItem.is_free_teaser.is_(False))
        .order_by(CatalogItem.id).limit(48)).all()
    ranked = []
    for item in rows:
        try:
            terms = catalog_terms(s, account, item.id)
            assert_unowned(s, account, fan.fan_id, terms)
            # Avoid repeating payoff media recently sent under any item alias.
            hero = set(terms["media_ids"]) - set(terms["preview_ids"])
            if s.scalar(select(VaultSend.id).where(VaultSend.account_id == account,
                    VaultSend.fan_id == fan.fan_id, VaultSend.media_id.in_(hero),
                    VaultSend.sent_at > now - timedelta(days=policy.repeat_item_days)).limit(1)):
                continue
            tags = load_object(item.tags)
            if type(tags) is not list or any(type(x) is not str for x in tags):
                continue
            overlap = len(words & set(re.findall(r"\w+", " ".join(tags).lower())))
            ranked.append(((-overlap, abs(terms["price_cents"] - recent_price) if recent_price else 0,
                            item.id), terms))
        except Refused:
            continue
    return [terms for _, terms in sorted(ranked, key=lambda x: x[0])]


def follow_source(s, account, fan_id, newest):
    offer = s.scalar(select(ContentOffer).where(ContentOffer.account_id == account,
        ContentOffer.fan_id == fan_id, ContentOffer.offer_message_id == newest.message_id,
        ContentOffer.status == "open").order_by(ContentOffer.id).limit(1).with_for_update(read=True))
    if offer is None or newest.is_paid or newest.price_cents <= 0:
        raise Refused("no_unpaid_follow_up_source")
    terms = catalog_terms(s, account, offer.item_id)
    assert_unowned(s, account, fan_id, terms)
    return {"id": offer.id, "item": offer.item_id, "price": offer.price_cents,
            "message": offer.offer_message_id, "offered_at": offer.offered_at,
            "catalogue_hash": digest(terms)}
