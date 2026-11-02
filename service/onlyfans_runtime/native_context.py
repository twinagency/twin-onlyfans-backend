"""Native configuration/fact snapshots, shared by preparation and final egress.

Reads use row locks in the common account transaction so ordinary native
configuration/fact updates cannot win between the last check and consumption.
No prompt, model client or native automation is imported at this boundary.
"""
from datetime import datetime
import hashlib
import json
import re

from sqlalchemy import select
from db.models import AccountAiConfig, Fan, FanProfile


def approved_persona(value):
    return (type(value) is str and bool(value.strip()) and not
        re.search(r"100%\s*human|never\s+(?:admit|reveal).*\bai\b|never a bot",value,re.I))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=lambda v: v.isoformat() if isinstance(v, datetime) else (_ for _ in ()).throw(TypeError()))


def versions(session, account_id, fan_id):
    def read(model, *clauses):
        result = session.execute(select(*model.__table__.columns).where(*clauses).with_for_update(read=True)).mappings().first()
        return dict(result) if result is not None else None
    config = read(AccountAiConfig, AccountAiConfig.account_id == account_id)
    fan = read(Fan, Fan.account_id == account_id, Fan.fan_id == fan_id)
    profile = read(FanProfile, FanProfile.account_id == account_id, FanProfile.fan_id == fan_id)
    source = None
    if config is not None:
        from .creator_facts import effective
        row = session.get(AccountAiConfig, account_id)
        facts, source = effective(session, row)
        config["persona_facts_json"] = canonical(facts) if facts else None
    snapshot = {"config": config, "fan": fan, "profile": profile, "creatorFactsSource": source}
    # Source-backed older history and operator corrections join the fenced snapshot (generation, queue and egress).
    from .launch_history_memory.memory import enrich_snapshot
    snapshot = enrich_snapshot(session, account_id, fan_id, snapshot)
    from .history_policy import memory_notice
    from db.onlyfans_models import OfRuntime
    runtime = session.get(OfRuntime, account_id)
    notice = memory_notice(session, account_id, runtime.binding_id, fan_id) if runtime else None
    if notice is not None:
        snapshot['historyAvailability'] = notice
        from .history_policy import approved_onboarding
        onboarding = approved_onboarding(session, account_id, runtime.binding_id)
        if onboarding:
            snapshot['approvedOnboarding'] = {k:v for k,v in onboarding.items() if k != 'privateInformation'}
    return hashlib.sha256(canonical(snapshot).encode()).hexdigest(), snapshot


def cost_matches(session, plan):
    from db.models import GrokCall
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    if (plan.model_call_id is None or plan.cost_settled_at is None or plan.charged_millicents is None
            or plan.reserved_millicents is None or plan.reserved_millicents <= 0 or plan.charged_millicents < 0):
        return False
    call = session.scalar(select(GrokCall).where(GrokCall.id == plan.model_call_id).with_for_update(read=True))
    policy = session.get(OfDeliveryPolicy, plan.account_id)
    if call is None or policy is None:
        return False
    if call.was_dry_run:
        mode_matches = (policy.transport_kind == "synthetic" and plan.model_id == "synthetic/native-text"
                        and call.endpoint == "synthetic://onlyfans-native-text")
    else:
        # Pure registries only: no credential resolution or model client is
        # constructed at final egress. Synthetic plans cannot cross into a
        # native account after an explicit mode change.
        from llm_models import MODELS
        from llm_providers import PROVIDERS
        model = MODELS.get(plan.resolved_model_id)
        provider = PROVIDERS.get(model.provider) if model is not None else None
        mode_matches = (policy.transport_kind == "native" and provider is not None
            and plan.model_id == plan.resolved_model_id and call.provider == provider.name
            and call.endpoint == provider.base_url.rstrip("/") + "/chat/completions")
    return (call is not None and call.account_id == plan.account_id and call.fan_id == plan.fan_id
            and call.model == plan.resolved_model_id and call.provider == plan.billing_provider
            and call.called_at.strftime("%Y-%m-%d") == plan.budget_day and call.purpose == "ai_chatter"
            and call.status == "done" and mode_matches and call.cost_cents == plan.charged_millicents)


def matches(session, intent):
    if intent.native_plan_id is None:
        return True
    from db.onlyfans_delivery_models import OfNativePlan
    plan = session.get(OfNativePlan, (intent.account_id, intent.native_plan_id))
    return (plan is not None and plan.status == "queued" and plan.intent_id == intent.intent_id
            and plan.fan_id == intent.fan_id and plan.body == intent.body
            and plan.sale_json == intent.catalog_snapshot_json
            and cost_matches(session, plan)
            and versions(session, intent.account_id, intent.fan_id)[0] == plan.context_hash)
