"""Bounded evidence from the same native reply, applied to existing domain rows.

No model, provider, scheduler or alternate prompt owner. Only the claimed latest
inbound can support a correction. The caller validates snapshot/lease/controls
and known model cost before applying these changes in the ready-plan transaction.
"""
import json
import os
import uuid
from sqlalchemy import select

from db.models import Fan, FanProfile, Message
from db.onlyfans_delivery_models import OfNativeMemoryChange
from .native_context import canonical, cost_matches, versions
from .store import Refused

FACT_FIELDS = frozenset({"real_name", "his_age", "home_city", "home_country", "hobbies", "occupation", "fetishes"})


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise Refused("duplicate native output key")
        result[key] = value
    return result


def parse_output(raw, context):
    if type(raw) is not str or not 0 < len(raw.encode()) <= 20000:
        raise Refused("invalid native output size")
    try:
        data = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, TypeError) as exc:
        raise Refused("invalid native output") from exc
    keys = {"reply", "facts", "profile"} | ({"sale"} if "sales" in context else set())
    if type(data) is not dict or set(data) != keys or type(data["reply"]) is not str:
        raise Refused("invalid native output contract")
    latest = context["messages"][-1]
    if latest["direction"] != "in": raise Refused("native memory requires current inbound")
    def quote(value):
        if type(value) is not str or not value.strip() or len(value) > 500 or value not in latest["body"]:
            raise Refused("native memory has no current source quote")
        return value
    facts = data["facts"]
    if type(facts) is not list or len(facts) > len(FACT_FIELDS):
        raise Refused("invalid native fact updates")
    seen = set()
    for fact in facts:
        if type(fact) is not dict or set(fact) != {"field", "value", "quote"}:
            raise Refused("invalid native fact shape")
        field, value = fact["field"], fact["value"]
        if type(field) is not str or field not in FACT_FIELDS or field in seen:
            raise Refused("unsupported or repeated native fact")
        seen.add(field)
        if type(value) is not str or not value.strip() or value != value.strip() or len(value) > (40 if field == "real_name" else 80):
            raise Refused("invalid native fact value")
        if field == "his_age" and (not value.isascii() or not value.isdigit() or not 18 <= int(value) <= 120):
            raise Refused("unsupported native age evidence")
        quote(fact["quote"])
    profile = data["profile"]
    if profile is not None:
        if type(profile) is not dict or set(profile) != {"short_bio", "bullet_points", "quote"}:
            raise Refused("invalid native profile shape")
        for field,limit in [("short_bio",400),("bullet_points",600)]:
            value = profile[field]
            if type(value) is not str or not value.strip() or value != value.strip() or len(value) > limit:
                raise Refused("invalid native profile value")
        quote(profile["quote"])
    memory = {"facts": facts, "profile": profile}
    if "sales" in context:
        from .native_sales import selected
        memory["sale"] = selected(data["sale"], context)
    return data["reply"], memory


def apply_memory(session, plan, claim, proposal, now):
    """Existing Fan/FanProfile values and their append-only evidence commit together."""
    if not proposal["facts"] and proposal["profile"] is None: return
    if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker" or plan.status != "generating"
            or plan.account_id != claim.account_id or plan.fan_id != claim.fan_id
            or plan.input_revision != claim.input_revision or not cost_matches(session, plan)
            or versions(session, plan.account_id, plan.fan_id)[0] != plan.context_hash):
        raise Refused("native memory lost its scoped invocation")
    source = session.scalar(select(Message).where(Message.account_id == plan.account_id,
        Message.fan_id == plan.fan_id,Message.message_id == claim.target_message_id).with_for_update(read=True))
    quotes = [f["quote"] for f in proposal["facts"]]
    if proposal["profile"] is not None: quotes.append(proposal["profile"]["quote"])
    if source is None or source.is_unsent or source.direction != "in" or any(q not in source.body for q in quotes):
        raise Refused("native memory source changed")
    # Operator overrides (including cleared fields) suppress automatic writes to the same fields.
    from .launch_history_memory.memory import respect_overrides
    proposal = respect_overrides(session, plan.account_id, plan.fan_id, proposal)
    fan = session.get(Fan,(plan.account_id,plan.fan_id))
    profile = session.get(FanProfile,(plan.account_id,plan.fan_id))
    def write(row, target, field, value, quote):
        before = getattr(row,field)
        if before == value: return False
        session.add(OfNativeMemoryChange(account_id=plan.account_id,change_id=str(uuid.uuid4()),
            fan_id=plan.fan_id,plan_id=plan.plan_id,source_message_id=claim.target_message_id,
            input_revision=claim.input_revision,target=target,field=field,before_json=canonical(before),
            after_json=canonical(value),source_quote=quote,context_hash=plan.context_hash,created_at=now))
        setattr(row,field,value)
        return True
    facts_changed = False
    for fact in proposal["facts"]:
        facts_changed |= write(fan,"fan",fact["field"],fact["value"],fact["quote"])
    if facts_changed: fan.grok_facts_updated_at = now
    update = proposal["profile"]
    if update is not None:
        if profile is None:
            profile = FanProfile(account_id=plan.account_id,fan_id=plan.fan_id)
            session.add(profile)
        changed = False
        for field in ["short_bio","bullet_points"]:
            changed |= write(profile,"profile",field,update[field],update["quote"])
        if changed:
            profile.last_generated_at,profile.generated_by_grok_call_id = now,plan.model_call_id
    session.flush()
