"""Native text preparation over canonical PostgreSQL snapshots.

Uses Fastt's existing prompt composer and deterministic outbound guard. It
does not run the legacy scheduler/send loop, create catalogue/commerce rows,
or construct a provider. Explicit synthetic and account-bound native adapters
are registered; constructing/resolving live credentials belongs to deployment.
One durable invocation per canonical input survives crashes without a replay.
"""
from dataclasses import asdict
from datetime import datetime, timedelta
import asyncio
import json
import os
import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import AccountAiConfig, Fan, FanProfile, Message, OfConversationState, OfCreatorBinding
from db.onlyfans_models import OfWork, OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfNativePlan
from .native_context import canonical, versions
from .store import Claim, Refused, _id
from .text_delivery import TextAuthority

PLANNER_VERSION = "native-text.v4"
MAX_CONTEXT_BYTES = 65536
MAX_SYNTHETIC_INVOCATIONS = 1000


class SyntheticNativeModel:
    """Trusted offline seam; never resolves a key, URL or paid model fallback."""
    model_id = "synthetic/native-text"

    def __init__(self, body="Thanks for telling me. How has your day been?", *, delay_seconds=0, on_generate=None, fail=False, reported_cost_millicents=1, fact_updates=None, profile_update=None, raw_output=None, sale=None):
        if type(body) is not str or len(body.encode()) > 20000 or type(delay_seconds) not in {int, float} or not 0 <= delay_seconds <= 60:
            raise Refused("invalid synthetic model response")
        self.body, self.delay, self.on_generate, self.fail = body, delay_seconds, on_generate, fail
        if reported_cost_millicents is not None and (type(reported_cost_millicents) is not int or not 0 <= reported_cost_millicents <= 100000000):
            raise Refused("invalid synthetic model usage")
        self.reported_cost_millicents = reported_cost_millicents
        self.completed = False
        self.fact_updates, self.profile_update, self.raw_output = [] if fact_updates is None else fact_updates, profile_update, raw_output
        self.calls = []
        self.sale, self.sales_enabled = sale, False

    async def generate(self, messages):
        self.completed = False
        self.calls.append(messages)
        if self.on_generate is not None:
            self.on_generate()
        await asyncio.sleep(self.delay)
        if self.fail:
            raise TimeoutError("synthetic model uncertainty")
        self.completed = True
        return self.raw_output if self.raw_output is not None else canonical(
            {"reply": self.body, "facts": self.fact_updates, "profile": self.profile_update,
             **({"sale": self.sale} if self.sales_enabled else {})})


def _native_messages(context):
    # The worker environment is selected before these native library imports.
    # The scheduler, provider client and legacy model entrypoint stay disabled.
    from automations import ai_chatter as native
    from automations.welcome_chatter_for_info import compose_persona
    from automations import _voice
    from automations._language import resolve_language, detect_bot_accusation
    cfg = AccountAiConfig(**context["config"])
    from .native_context import approved_persona
    if not approved_persona(cfg.persona):
        raise Refused("approved creator configuration is required")
    fan = Fan(**context["fan"])
    profile = FanProfile(**context["profile"]) if context.get("profile") is not None else None
    language = resolve_language(cfg.language, fan.language)
    # Generated earlier creator claims cannot outrank the approved creator facts.
    for name in ("persona_claims_json", "persona_age_claimed", "persona_location_claimed", "persona_job_claimed"):
        setattr(fan, name, None)
    candidate = native._Cand(fan.fan_id)
    for message in context["messages"]:
        stamp = datetime.fromisoformat(message["created_at"])
        candidate.add_message(message["direction"], message["body"], message["message_id"], stamp)
        if message["direction"] == "in":
            candidate.last_in_text = message["body"]
            candidate.fan_msg_n += 1
    candidate.last_dir = context["messages"][-1]["direction"]
    # Shared typed lists must not silently lose creator limits after 240 chars.
    # Use the same composer; all legacy callers retain its existing default.
    persona = compose_persona(cfg, fallback="", fact_value_clip=16000)
    bot_accused = detect_bot_accusation(candidate.last_in_text, language)
    kind = native._turn_kind(bot_accused=bot_accused, pic_desc="", content_ask=False,
                            escalation=False, image_dare=False, dare_callback=False,
                            hot_thread=False, can_sell=False, pic_offer=False)
    messages, _ = native._build_messages(persona, fan, candidate, set(), history_tail=100,
                                         onlyfans_text_only=True, painful_on=False,
                                         onlyfans_memory=True, profile=profile,
                                         dated_memory="learnedProfile" in context.get("historicalMemory", {}),
                                         onlyfans_sales=context.get("sales"),
                                         bot_accused=bot_accused, kind=kind,
                                         v=_voice.blocks(cfg.voice, sell_customs=False),
                                         lang=language)
    memory = context.get("historicalMemory")
    # Older source excerpts and operator overrides only; canonical facts already reach the composer above.
    if memory and (memory.get("learnedProfile") or memory["interactions"] or memory["overrides"]
                   or any(fact["source"] != "canonical" for fact in memory["facts"].values())):
        from .launch_history_memory.memory import augment_prompt
        messages = augment_prompt(messages, memory)
    return messages


async def _draft(context, messages, model, timeout):
    if len(canonical(messages).encode()) > MAX_CONTEXT_BYTES + 16384:
        raise Refused("native prompt exceeds its byte budget")
    async with asyncio.timeout(timeout):
        model.sales_enabled = "sales" in context
        raw = await model.generate(messages)
    from .native_memory import parse_output
    raw, memory = parse_output(raw, context)
    if not raw.strip(): raise Refused("invalid native draft")
    from automations._outbound import finalize_draft
    text, leak = await finalize_draft(raw, account_id=context["account_id"], fan_id=context["fan"]["fan_id"], purpose="onlyfans_native_text")
    from automations.ai_chatter import _unbacked_talk, _DELIVERY_TALK_RE
    if leak or not text.strip() or _unbacked_talk(text) or _DELIVERY_TALK_RE.search(text):
        raise Refused("unbacked native draft")
    from native_pilot.identity import discloses_ai
    if discloses_ai(text):
        raise Refused("identity disclosure held")
    sale = memory.pop("sale", None)
    if sale is not None:
        from .native_sales import caption
        # The conversational answer leads. The saved description and exact
        # price are part of the outbound draft and must pass the same floor.
        text = text + "\n\n" + caption(sale)
        text, leak = await finalize_draft(text, account_id=context["account_id"],
            fan_id=context["fan"]["fan_id"], purpose="onlyfans_native_media", block_links=True)
        # Check all composed content, exempting only the exact structured media
        # count and price we just derived from the selected, validated sale.
        # Saved descriptions and all conversational text remain subject to the floor.
        generated = caption(sale)
        phrase = generated.split(": ", 1)[0]
        price_line = generated.rsplit("\n", 1)[-1]
        boundary = "\n\n" + phrase + ": "
        if not text.endswith("\n" + price_line) or boundary not in text:
            raise Refused("unbacked native media caption")
        floor_text = text[:-len("\n" + price_line)].replace(boundary, "\n\n", 1)
        if leak or not text.strip() or _unbacked_talk(floor_text) or _DELIVERY_TALK_RE.search(text):
            raise Refused("unbacked native media caption")
        if len(text.encode()) > 20000:
            raise Refused("native media caption exceeds its byte budget")
    return text, memory, sale


class NativeTextPlanner:
    def __init__(self, store):
        if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
            raise Refused("native preparation requires the explicit worker role")
        self.store, self.engine = store, store.engine
        self.authority = TextAuthority(store)

    def prepare(self, tenant_id, claim, model, *, timeout_seconds=10):
        from .provider_model import NativeProviderModel
        if type(claim) is not Claim or type(model) not in {SyntheticNativeModel, NativeProviderModel}:
            raise Refused("only an explicit scoped native model invocation is registered")
        live = type(model) is NativeProviderModel
        if live and model.account_id != claim.account_id:
            raise Refused("native model account scope mismatch")
        _id(claim.fan_id)
        _id(claim.target_message_id)
        if any(type(value) is not int or value < 0 for value in (claim.generation, claim.control_revision, claim.input_revision)):
            raise Refused("invalid native claim revision")
        if type(timeout_seconds) not in {int, float} or not .01 <= timeout_seconds <= 15:
            raise Refused("invalid native model deadline")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise Refused("native preparation requires a synchronous worker")
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant_id, claim.account_id)
            prior = s.scalar(select(OfNativePlan).where(OfNativePlan.account_id == claim.account_id,
                OfNativePlan.fan_id == claim.fan_id, OfNativePlan.input_revision == claim.input_revision))
            if prior is not None:
                # A copied/reclaimed claim never causes a second model call.
                if prior.status == "generating":
                    original = json.loads(prior.claim_json)
                    current = s.get(OfWork, (claim.account_id, claim.fan_id))
                    now = self.authority._db_now(s)
                    if current is None or current.lease_token != original["token"] or current.lease_until is None or current.lease_until <= now:
                        prior.status, prior.reason = "unknown", "interrupted_generation"
                        if current is not None and current.generation == claim.generation:
                            current.status, current.lease_token, current.lease_until = "blocked", None, None
                return prior.plan_id
            now = self.authority._db_now(s)
            state = s.get(OfConversationState, (claim.account_id, claim.fan_id))
            work = s.get(OfWork, (claim.account_id, claim.fan_id))
            policy = s.get(OfDeliveryPolicy, claim.account_id)
            if policy is None or policy.transport_kind != ("native" if live else "synthetic"):
                raise Refused("native model mode does not match its delivery policy")
            if not self.authority._valid_work(claim, work, state, now):
                raise Refused("stale native planning claim")
            if self.authority._eligibility(s, runtime, policy, state, s.get(Fan, (claim.account_id, claim.fan_id)), "automated", now):
                raise Refused("native planning is currently held")
            count = s.scalar(select(func.count()).select_from(OfNativePlan).where(OfNativePlan.account_id == claim.account_id))
            if not live and count >= MAX_SYNTHETIC_INVOCATIONS:
                raise Refused("synthetic planning allowance exhausted")
            fingerprint, snapshot = versions(s, claim.account_id, claim.fan_id)
            if snapshot.get("creatorFactsSource") is not None and snapshot["creatorFactsSource"]["state"] != "current":
                raise Refused("shared creator facts are not current")
            if snapshot["config"] is None:
                raise Refused("approved creator configuration is required")
            messages = s.execute(select(Message.message_id, Message.direction, Message.body, Message.created_at).where(
                Message.account_id == claim.account_id, Message.fan_id == claim.fan_id,
                Message.is_unsent.is_(False)).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(100)).mappings().all()
            if (not messages or messages[0]["direction"] != "in"
                    or messages[0]["message_id"] != claim.target_message_id):
                raise Refused("native input has moved on")
            context = {**snapshot, "account_id": claim.account_id, "messages": list(reversed([dict(row) for row in messages]))}
            if policy.media_enabled:
                from .native_sales import candidates
                context["sales"] = candidates(s, claim.account_id, claim.fan_id, now)
            encoded = canonical(context)
            if len(encoded.encode()) > MAX_CONTEXT_BYTES:
                raise Refused("native context exceeds its byte budget")
            identity = str(uuid.uuid4())
            binding_id = runtime.binding_id
            s.add(OfNativePlan(account_id=claim.account_id, plan_id=identity, fan_id=claim.fan_id,
                binding_id=runtime.binding_id, work_generation=claim.generation, input_revision=claim.input_revision, claim_json=canonical(asdict(claim)),
                context_json=encoded, context_hash=fingerprint, planner_version=PLANNER_VERSION,
                model_id=model.model_id, status="generating", created_at=now, expires_at=now+timedelta(seconds=60)))
        # Every transaction and row lock is released before model work.
        result, reason = None, None
        from automations._common import resolve_model_from_config
        from llm_client import (LLMCapExceeded, LLMConfigError,
            reserve_scoped_synthetic_call, settle_scoped_synthetic_call)
        try:
            context = json.loads(encoded)
            messages = _native_messages(context)
            chosen = resolve_model_from_config(AccountAiConfig(**context["config"]),
                "ai_chatter", account_id=claim.account_id, require_explicit=True)
            if live:
                model.configure_call(account_id=claim.account_id, chosen_model=chosen,
                                     config=context["config"], messages=messages)
            with Session(self.engine) as s, s.begin():
                runtime = self.store._runtime(s, tenant_id, claim.account_id)
                plan = s.get(OfNativePlan, (claim.account_id, identity))
                state = s.get(OfConversationState, (claim.account_id, claim.fan_id))
                work = s.get(OfWork, (claim.account_id, claim.fan_id))
                policy = s.get(OfDeliveryPolicy, claim.account_id)
                now = self.authority._db_now(s)
                if (plan.status != "generating" or plan.expires_at <= now
                        or not self.authority._valid_work(claim, work, state, now)
                        or self.authority._eligibility(s, runtime, policy, state, s.get(Fan, (claim.account_id, claim.fan_id)), "automated", now)
                        or versions(s, claim.account_id, claim.fan_id)[0] != fingerprint):
                    plan.status, plan.reason = "stale", "snapshot_changed"
                    return identity
                reserve = model.reserve if live else reserve_scoped_synthetic_call
                call, reserved, day = reserve(s,
                    account_id=claim.account_id, fan_id=claim.fan_id, model=chosen, messages=messages, now=now)
                plan.resolved_model_id, plan.model_call_id = chosen, call.id
                plan.reserved_millicents, plan.budget_day, plan.billing_provider = reserved, day, call.provider
            result, memory, sale = asyncio.run(_draft(context, messages, model, timeout_seconds))
        except Exception as exc:
            reason = ("model_unknown" if isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                else "model_budget_refused" if isinstance(exc, LLMCapExceeded)
                else "model_configuration_refused" if isinstance(exc, LLMConfigError)
                else "draft_refused")
        with Session(self.engine) as s, s.begin():
            # Finish only this invocation's historical evidence if its binding
            # was revoked while the model was running. Never authorize new work
            # through a revoked binding or leave a completed draft generating.
            runtime = s.scalar(select(OfRuntime).where(OfRuntime.account_id == claim.account_id).with_for_update())
            binding = s.scalar(select(OfCreatorBinding).where(
                OfCreatorBinding.id == binding_id, OfCreatorBinding.account_id == claim.account_id,
                OfCreatorBinding.tenant_id == tenant_id, OfCreatorBinding.channel == "onlyfans",
            ).with_for_update(read=True))
            plan = s.get(OfNativePlan, (claim.account_id, identity))
            now = self.authority._db_now(s)
            if plan.model_call_id is not None:
                settle = model.settle if live else settle_scoped_synthetic_call
                charged = settle(s, account_id=claim.account_id,
                    call_id=plan.model_call_id, reserved_millicents=plan.reserved_millicents,
                    actual_millicents=model.reported_cost_millicents if model.completed else None, now=now)
                if charged is not None:
                    plan.charged_millicents, plan.cost_settled_at = charged, now
                elif reason is None:
                    reason = "model_usage_unknown"
            if plan.status != "generating":
                return identity
            state = s.get(OfConversationState, (claim.account_id, claim.fan_id))
            work = s.get(OfWork, (claim.account_id, claim.fan_id))
            now = self.authority._db_now(s)
            policy = s.get(OfDeliveryPolicy, claim.account_id)
            if (runtime is None or runtime.binding_id != binding_id or binding is None or binding.revoked_at is not None
                    or plan.expires_at <= now or not self.authority._valid_work(claim, work, state, now)
                    or self.authority._eligibility(s, runtime, policy, state, s.get(Fan, (claim.account_id, claim.fan_id)), "automated", now)
                    or versions(s, claim.account_id, claim.fan_id)[0] != fingerprint):
                plan.status, plan.reason = "stale", "snapshot_changed"
            elif reason is not None:
                plan.status, plan.reason = ("unknown" if reason in {"model_unknown", "model_usage_unknown"} else "refused"), reason
                work.status = "blocked"
                work.lease_token = work.lease_until = None
            else:
                from .native_memory import apply_memory
                from .native_sales import current_sale
                if sale is not None and not current_sale(s, claim.account_id, claim.fan_id, sale, now):
                    plan.status, plan.reason = "stale", "sales_snapshot_changed"
                    work.status, work.lease_token, work.lease_until = "blocked", None, None
                    return identity
                try:
                    apply_memory(s, plan, claim, memory, now)
                except Refused:
                    plan.status, plan.reason = "refused", "memory_source_changed"
                    work.status, work.lease_token, work.lease_until = "blocked", None, None
                else:
                    plan.context_hash = versions(s, claim.account_id, claim.fan_id)[0]
                    plan.status, plan.body = "ready", result
                    plan.sale_json = canonical(sale) if sale is not None else None
        return identity

    def queue(self, tenant_id, account_id, plan_id):
        with Session(self.engine) as s:
            self.store._runtime(s, tenant_id, account_id)
            plan = s.get(OfNativePlan, (account_id, plan_id))
            if plan is None or plan.status not in {"ready", "queued"}:
                raise Refused("native plan is not ready")
            claim = Claim(**json.loads(plan.claim_json))
            body = plan.body
            sale = json.loads(plan.sale_json) if plan.sale_json else None
        return self.authority.prepare(tenant_id, account_id, claim.fan_id,
            command_key="native:"+plan_id, expected_revision=claim.control_revision,
            body=body, origin="automated", actor="native-text-worker", work=claim,
            native_plan_id=plan_id, catalog_item_id=sale["item"]["id"] if sale else None,
            quoted_price_cents=sale["price_cents"] if sale else None)

    def recover(self, tenant_id, account_id, *, limit=4):
        """Resume recorded ready work with its original lease, never another model.

        An expired/replaced lease cannot be renewed through this method. Original
        invocation/claim/cost evidence stays unchanged. A fresh input is required
        after an expired or uncertain plan; account activation is never inferred.
        """
        if type(limit) is not int or not 1<=limit<=20:raise Refused("invalid native recovery limit")
        with Session(self.engine) as s,s.begin():
            self.store._runtime(s,tenant_id,account_id)
            ids=s.scalars(select(OfNativePlan.plan_id).where(OfNativePlan.account_id==account_id,
                OfNativePlan.status.in_(("generating","ready"))).order_by(OfNativePlan.created_at,OfNativePlan.plan_id).limit(limit)).all()
        queued=retired=0
        for identity in ids:
            ready=False
            with Session(self.engine) as s,s.begin():
                runtime=self.store._runtime(s,tenant_id,account_id)
                plan=s.get(OfNativePlan,(account_id,identity))
                if plan.status not in {"generating","ready"}:continue
                claim=Claim(**json.loads(plan.claim_json))
                now=self.authority._db_now(s)
                work=s.get(OfWork,(account_id,plan.fan_id))
                state=s.get(OfConversationState,(account_id,plan.fan_id))
                valid=self.authority._valid_work(claim,work,state,now)
                if plan.status=="generating":
                    if valid and plan.expires_at>now:continue
                    plan.status,plan.reason="unknown","interrupted_generation"
                elif not valid or plan.expires_at<=now or plan.binding_id!=runtime.binding_id:
                    plan.status,plan.reason="stale","expired_planning_authority"
                else:
                    ready=True
                if not ready:
                    if (work is not None and work.status=="claimed" and work.generation==claim.generation
                            and work.lease_token==claim.token):
                        work.status="blocked";work.lease_token=work.lease_until=None
                    retired+=1
            if ready:
                try:
                    self.queue(tenant_id,account_id,identity);queued+=1
                except Refused as exc:
                    # Keep a valid ready plan briefly through a temporary account
                    # hold; it retains its original deadline and lease throughout.
                    if str(exc) in {"account delivery unresolved","delivery queue full","history_not_current"}:continue
                    with Session(self.engine) as s,s.begin():
                        self.store._runtime(s,tenant_id,account_id)
                        plan=s.get(OfNativePlan,(account_id,identity))
                        if plan.status=="ready":
                            plan.status,plan.reason="stale","recovery_snapshot_changed"
                            work=s.get(OfWork,(account_id,plan.fan_id))
                            if work is not None and work.status=="claimed" and work.lease_token==claim.token:
                                work.status="blocked";work.lease_token=work.lease_until=None
                            retired+=1
        return queued,retired
