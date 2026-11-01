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


def _reply_format_enabled(cfg):
    """The same literal opt-in the FASTT route reads (native_reply_format_enabled), from the saved account config."""
    from .reply_settings import enabled
    try:
        saved = json.loads(cfg.ai_chatter_config_json or "{}")
    except (TypeError, ValueError):
        return False
    return type(saved) is dict and enabled(saved, "native_reply_format_enabled")


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
        # R3: an inbound attachment note (received/processing/described/unsupported/failed) rides the history line.
        candidate.add_message(message["direction"], message.get("model_body", message["body"]), message["message_id"], stamp)
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
                                         reply_format=_reply_format_enabled(cfg),
                                         onlyfans_memory=True, profile=profile,
                                         dated_memory="learnedProfile" in context.get("historicalMemory", {}),
                                         onlyfans_sales=context.get("sales"),
                                         bot_accused=bot_accused, kind=kind,
                                         v=_voice.blocks(cfg.voice, sell_customs=False),
                                         lang=language)
    from automations import _of_integrity
    if messages and messages[0].get("role") == "system":
        messages[0] = {**messages[0], "content": messages[0]["content"] + _of_integrity.onlyfans_rules()}
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
    from native_pilot.media_claims import echoes_instruction
    if echoes_instruction(text):
        raise Refused("instruction text held")

    from automations import _of_integrity
    if _of_integrity.unsafe_identity_claim(text):
        raise Refused("identity claim held")
    intent = context.get("inboundIntent") or {}
    if _of_integrity.looks_like_rating(text) and not intent.get("ratingAllowed"):
        raise Refused("unsolicited rating held")
    # Fan-authored explicit corrections are evidence; promo/business traffic never writes personal memory.
    if intent.get("memoryBlocked"):
        memory = {**memory, "facts": [], "profile": None}
    else:
        memory = _of_integrity.merge_corrections(memory, context["messages"][-1]["body"])
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
        if echoes_instruction(text.replace(generated, "")):
            raise Refused("instruction text held")
    return text, memory, sale


def review_inbound(session, account_id, fan_id, rows, fan, now):
    """R3/R7 review of the unanswered inbound burst (oldest first). Adds ``model_body`` (attachment notes)
    to each row, classifies each unanswered message on its own and decides whether a customer reply is allowed.
    """
    from automations import _of_integrity
    from automations._inbound_intent import (IntentContext, classify_inbound_intent, signals_of,
                                             _CONTENT, PROMO, BUSINESS)
    from native_pilot.identity import sincere_identity_question
    burst = []
    for row in reversed(rows):
        if row["direction"] != "in":
            break
        burst.append(row)
    burst.reverse()
    base = IntentContext(sender_is_creator=(getattr(fan, "source", "") or "") == "creator_we_follow",
                         lifetime_spend_cents=int(getattr(fan, "lifetime_spend_cents", 0) or 0),
                         our_replies=int(getattr(fan, "turn_counter", 0) or 0))
    window = now - timedelta(hours=24)
    results, states, cases = [], [], []
    for row in rows:
        if row["direction"] == "in":
            kind = _of_integrity.attachment_kind(row.get("raw_json"))
            state = _of_integrity.attachment_state(row.get("media_count"), row.get("image_desc"), kind, row["created_at"], now)
            if state is not None:
                row["model_body"] = _of_integrity.attachment_note(state, kind, row.get("image_desc"), row.get("media_count") or 1)
                row["model_body"] = (row["body"] + " " + row["model_body"]).strip() if (row["body"] or "").strip() else row["model_body"]
            else:
                row["model_body"] = row["body"]
            row["attachment"] = {"state": state, "kind": kind} if state else None
        else:
            row["model_body"] = row["body"]
    for row in burst:
        text = row["body"] or ""
        ctx = base
        if signals_of(text, base) & _CONTENT and text.strip():
            # Cross-chat evidence is only fetched for messages that already carry a promotional signal.
            others = session.scalar(select(func.count(func.distinct(Message.fan_id))).where(
                Message.account_id == account_id, Message.direction == "in", Message.body == text,
                Message.fan_id != fan_id, Message.created_at >= window))
            echo = session.scalar(select(func.count()).select_from(Message).where(
                Message.account_id == account_id, Message.direction == "out", Message.body == text,
                Message.created_at >= now - timedelta(days=7))) > 0
            own = sum(1 for r in rows if r["direction"] == "in" and r["body"] == text)
            ctx = IntentContext(**{**base.__dict__, "same_text_other_fans": int(others or 0),
                                   "same_text_self_count": own, "equals_our_recent_outbound": echo})
        result = classify_inbound_intent(text, ctx)
        results.append(result)
        states.append(row.get("attachment") and row["attachment"]["state"])
    latest = results[-1] if results else None
    withheld = None
    if results and all(r.suppress_reply for r in results):
        withheld = "business_no_reply" if any(r.label == BUSINESS for r in results) else "promo_no_reply"
        if any(r.label == BUSINESS for r in results):
            cases.append({"kind": "business_enquiry", "reason": "business_enquiry_needs_person",
                          "dedupe_key": f"business:{account_id}:{fan_id}:{burst[-1]['message_id']}",
                          "evidence": latest.as_json(), "source_message_id": burst[-1]["message_id"]})
    for row in burst:
        if sincere_identity_question(row["body"]):
            cases.append({"kind": "identity_question", "reason": "fan_asked_if_automated_or_human",
                          "dedupe_key": f"identity:{account_id}:{fan_id}:{row['message_id']}",
                          "evidence": {}, "source_message_id": row["message_id"]})
    if any(st in (_of_integrity.FAILED,) for st in states):
        bad = [r for r, st in zip(burst, states) if st == _of_integrity.FAILED][-1]
        cases.append({"kind": "attachment_unreadable", "reason": "inbound_attachment_not_described",
                      "dedupe_key": f"attachment:{account_id}:{fan_id}:{bad['message_id']}",
                      "evidence": {"messageId": bad["message_id"]}, "source_message_id": bad["message_id"]})
    texts = [r["body"] for r in burst]
    intent = {"latest": latest.label if latest else None,
              "labels": [r.label for r in results],
              "memoryBlocked": bool(latest is not None and not latest.allows_memory_write),
              "ratingAllowed": _of_integrity.rating_allowed(texts, [st for st in states if st]),
              "withheld": withheld}
    return {"withheld": withheld, "inboundIntent": intent, "cases": cases}


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
        # R1: judge the fan's recent messages with the shared detectors BEFORE any plan or model call. A live hold
        # (or a stop that arrived since the last turn) makes the eligibility check below refuse; shadow only records.
        self.authority._safety_scan(tenant_id, claim.account_id, claim.fan_id, "automated")
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
            messages = s.execute(select(Message.message_id, Message.direction, Message.body, Message.created_at,
                Message.media_count, Message.image_desc, Message.raw_json).where(
                Message.account_id == claim.account_id, Message.fan_id == claim.fan_id,
                Message.is_unsent.is_(False)).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(100)).mappings().all()
            if (not messages or messages[0]["direction"] != "in"
                    or messages[0]["message_id"] != claim.target_message_id):
                raise Refused("native input has moved on")
            ordered = list(reversed([dict(row) for row in messages]))
            review = review_inbound(s, claim.account_id, claim.fan_id, ordered, s.get(Fan, (claim.account_id, claim.fan_id)), now)
            from .operator_case_port import open_operator_case, durable_sink
            for case in review["cases"]:
                open_operator_case(account_id=claim.account_id, fan_id=claim.fan_id, sink=durable_sink(s), **case)
            if review["withheld"] is not None:
                # Confirmed promotional/business traffic: no customer reply, no model call, no memory. Raw
                # messages are untouched; the decision is recorded on the plan row and recomputed from the
                # raw text on any later input, so it is reviewable and reversible.
                identity = str(uuid.uuid4())
                s.add(OfNativePlan(account_id=claim.account_id, plan_id=identity, fan_id=claim.fan_id,
                    binding_id=runtime.binding_id, work_generation=claim.generation, input_revision=claim.input_revision,
                    claim_json=canonical(asdict(claim)), context_json=canonical({"inboundIntent": review["inboundIntent"]}),
                    context_hash=fingerprint, planner_version=PLANNER_VERSION, model_id=model.model_id,
                    status="refused", reason=review["withheld"], created_at=now, expires_at=now+timedelta(seconds=60)))
                work.status, work.lease_token, work.lease_until = "blocked", None, None
                return identity
            for row in ordered:
                for key in ("media_count", "image_desc", "raw_json"):
                    row.pop(key, None)
            context = {**snapshot, "account_id": claim.account_id, "messages": ordered, "inboundIntent": review["inboundIntent"]}
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
            # Shared runtime lock: model preparation for different fans may
            # proceed together, while an operator control still waits for the
            # short reservation/settlement transaction to finish.
            runtime = s.scalar(select(OfRuntime).where(OfRuntime.account_id == claim.account_id).with_for_update(read=True))
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
