"""Real model invocation for the engine: preflight, reserve, call OUTSIDE any lock, settle, validate, regenerate, hand over.

The engine gives `ModelRuntime.propose()` a snapshot and two callbacks. Everything here runs outside any engine
database transaction: the ledger opens short transactions around the call, never across it.

Order for every attempt (any failure before step 4 means no network traffic at all):
  1. preflight  config enabled, account enabled, models on the allow-list, creator profile present, provider matches
  2. recheck    owner, binding, control revision, input revision, memory/offer fingerprints still current (engine callback)
  3. reserve    estimate against the daily and per-creator caps + a lease, atomically (ledger)
  4. call       one request; a lost response is `unknown`, a refused/unsent request is released
  5. settle     actual usage (or conservative unknown), then recheck again before anything is accepted
  6. judge      strict schema + safety rules; a bad answer advances the bounded chain

Chain (ModelConfig.chain): primary -> same model with an in-character prefix -> alternate model (if configured) ->
hand the request to a human. No canned line is ever produced. A stored response is reused after a crash only when it
was produced from exactly the same snapshot, so a restart never pays twice or sends a stale answer.
"""
from dataclasses import dataclass
from datetime import timedelta
import os
import uuid

from onlyfans_runtime.store import Refused

from .model_client import (ChatRequest, ModelBusy, ModelCallError, ModelHTTPStatus, ModelNotSent, ModelOutcomeUnknown,
                           ModelUnavailable)
from .model_ledger import CONTENT_FAILURES, ModelLedger
from .model_output import JudgeContext, judge
from .prompt_registry import get_prompt_provider
from .writer_context import PromptBudget, build_context, pick_offer
from .writer_examples import NullExamples


@dataclass(frozen=True)
class ModelTurn:
    account: str
    fan_id: int
    request_id: str
    generation: int
    binding_id: str
    owner_epoch: int
    snapshot: dict
    snapshot_sha256: str
    recheck: object   # callable() -> None; raises Refused when the conversation moved on
    handover: object  # callable(reason: str) -> None; moves the request to a human


class ModelRuntime:
    kind = 'runtime'

    def __init__(self, engine, client, config, profiles, *, prompts=None, examples=None, worker_id=None, clock=None,
                 budget=PromptBudget()):
        self.config, self.client, self.profiles, self.budget = config, client, profiles, budget
        self.prompts = prompts if prompts is not None else get_prompt_provider(config.prompt_provider)
        self.examples = examples if examples is not None else NullExamples()
        self.worker_id = worker_id or f'w-{os.getpid()}-{uuid.uuid4().hex[:12]}'
        kwargs = {'clock': clock} if clock is not None else {}
        self.ledger = ModelLedger(engine, config, **kwargs)
        self._clock = self.ledger.clock

    # ---- 1. preflight: nothing here touches the network or the ledger ---------------
    def _preflight(self, turn):
        cfg = self.config
        if not cfg.enabled:
            raise Refused('model invocation is not enabled')
        if turn.account not in cfg.enabled_accounts:
            raise Refused('model invocation is not enabled for this account')
        models = [m for _, m, _ in cfg.chain()]
        if not cfg.primary_model or any(m not in cfg.allowed_models for m in models):
            raise Refused('model is not on the approved list')
        if getattr(self.client, 'provider', None) != cfg.provider:
            raise Refused('model provider does not match the configuration')
        if cfg.lease_seconds <= cfg.timeout_seconds + 5:
            raise Refused('model lease must outlast the call timeout')
        profile = self.profiles.get(turn.account)
        if profile is None:
            raise Refused('creator facts are unavailable for this account')
        return profile

    def _judge_context(self, turn, profile):
        snap = turn.snapshot
        scope_ids = {str(i['itemId']) for i in (snap.get('catalogue_search') or {}).get('items', [])}
        scope_ids |= {str(g['itemId']) for g in snap.get('offer_grants', [])}
        scope_ids |= {str(x) for x in snap.get('sales_scope', [])}
        offer = pick_offer(snap, snap['current_message']['body'])
        return JudgeContext(snapshot=snap, profile=profile,
                            allowed_grant_ids=frozenset({offer['grantId']} if offer else ()),
                            allowed_scope_ids=frozenset(scope_ids))

    # ---- the chain -------------------------------------------------------------------
    def propose(self, turn):
        cfg, ledger = self.config, self.ledger
        profile = self._preflight(turn)
        ledger.sweep(turn.account, turn.request_id, turn.generation)
        rows = ledger.rows(turn.account, turn.request_id, turn.generation)
        if len(rows) >= cfg.max_calls_per_generation:
            turn.handover('model_call_limit')
            raise Refused('model call limit reached; request handed to a human')
        jctx = self._judge_context(turn, profile)
        chain = [(i, step, model, prefixed) for i, (step, model, prefixed) in enumerate(cfg.chain())]
        pos, dead, content_failed, fix = 0, set(), False, []
        now = self._clock()
        for row in rows:
            same = row.snapshot_sha256 == turn.snapshot_sha256
            if row.status in ('reserved', 'calling'):
                raise ModelBusy('a model call is already in flight for this request')
            if row.response_text is not None and row.outcome in (None, 'ok'):
                if not same:
                    continue  # produced from an older view of the conversation: never reuse
                verdict = judge(row.response_text, jctx)  # a stored answer: reuse, never pay twice
                if verdict.outcome != row.outcome:
                    ledger.set_outcome(turn.account, row.call_id, verdict.outcome)
                if verdict.outcome == 'ok':
                    turn.recheck()
                    return verdict.proposal
                pos, content_failed = max(pos, row.chain_pos + 1), True
                fix = list(verdict.reasons) if verdict.outcome == 'invalid' else []
            elif row.outcome in CONTENT_FAILURES:
                if same:
                    pos, content_failed = max(pos, row.chain_pos + 1), True
            elif row.status == 'released' and row.outcome == 'lease_expired':
                continue  # never reached the wire: simply try that step again
            elif now - row.updated_at < timedelta(seconds=cfg.transport_retry_seconds):
                dead.add(row.model)  # recent transport failure (about the provider, not the content): don't hammer it
                pos = max(pos, row.chain_pos + 1)
        while True:
            step = next((s for s in chain if s[0] >= pos and s[2] not in dead), None)
            if step is None:
                break
            chain_pos, chain_step, model, prefixed = step
            turn.recheck()  # stale work never spends money
            offer = pick_offer(turn.snapshot, turn.snapshot['current_message']['body'])
            history_hers = [t for who, t in build_context(turn.snapshot, profile, budget=self.budget).history if who == 'HER']
            examples = self.examples.retrieve(turn.account, turn.snapshot['current_message']['body'],
                                              allow_selling=offer is not None, exclude=frozenset(history_hers[-8:]))
            ctx = build_context(turn.snapshot, profile, examples=examples, fix=fix, prefixed=prefixed, budget=self.budget)
            rendered = self.prompts.render(ctx)
            reservation = ledger.reserve(account=turn.account, request_id=turn.request_id, generation=turn.generation,
                fan_id=turn.fan_id, binding_id=turn.binding_id, owner_epoch=turn.owner_epoch, chain_pos=chain_pos,
                chain_step=chain_step, model=model, prefixed=prefixed, snapshot_sha256=turn.snapshot_sha256,
                prompt_sha256=rendered.sha256, worker_id=self.worker_id, provider=cfg.provider)
            ledger.mark_calling(turn.account, reservation.call_id, self.worker_id)
            request = ChatRequest(model=model, messages=rendered.messages, max_tokens=cfg.max_tokens,
                                  temperature=cfg.temperature, json_mode=True, timeout_seconds=cfg.timeout_seconds)
            try:
                result = self.client.chat(request)  # the only network step; no database transaction is open
            except (ModelNotSent, ModelHTTPStatus) as exc:
                ledger.release(turn.account, reservation.call_id, self.worker_id,
                               outcome=exc.error_class, error_class=exc.error_class)
                dead.add(model); pos = chain_pos + 1
                continue
            except ModelOutcomeUnknown as exc:
                ledger.mark_unknown(turn.account, reservation.call_id, self.worker_id, outcome='timeout', error_class=exc.error_class)
                dead.add(model); pos = chain_pos + 1
                continue
            except Exception as exc:  # unclassified client failure: assume it may have been charged
                ledger.mark_unknown(turn.account, reservation.call_id, self.worker_id, outcome='client_error',
                                    error_class=type(exc).__name__[:60])
                dead.add(model); pos = chain_pos + 1
                continue
            text = result.text if type(getattr(result, 'text', None)) is str else ''
            ledger.settle(turn.account, reservation.call_id, self.worker_id, text_value=text, usage=result.usage)
            turn.recheck()  # the world may have moved during the call: a stale answer is never accepted
            verdict = judge(text, jctx)
            ledger.set_outcome(turn.account, reservation.call_id, verdict.outcome)
            if verdict.outcome == 'ok':
                return verdict.proposal
            content_failed = True
            fix = list(verdict.reasons) if verdict.outcome == 'invalid' else []
            pos = chain_pos + 1
        if content_failed:
            turn.handover('model_refused')
            raise Refused('model could not write an acceptable reply; request handed to a human')
        raise ModelUnavailable('model is unavailable; the request stays open')
