"""Real model invocation for the engine: preflight, reserve, call OUTSIDE any lock, settle, validate, regenerate, hand over.

The engine gives `ModelRuntime.propose()` a snapshot and two callbacks. Everything here runs outside any engine
database transaction: the ledger opens short transactions around the call, never across it.

Order for every attempt (any failure before step 4 means no network traffic at all):
  1. preflight  config enabled, account enabled, the ONE model on the allow-list, provider matches, an approved creator
                profile with documented boundaries (else the request goes to a human), identity questions only with
                approved wording (else a human), no journey rule that needs a person
  2. recheck    owner, binding, control revision, input revision, memory/offer fingerprints still current (engine callback)
  3. reserve    the conservative UPPER BOUND (configured input/output tokens x price, for this call and every retry still
                allowed) against the daily and per-creator caps + a lease, atomically (ledger); an over-size prompt is
                never sent
  4. call       one request; a lost response is `unknown` (keeps its reservation), a refused/unsent request is released
  5. settle     actual usage (or the conservative unknown), the RETURNED model id is recorded, then recheck again
  6. judge      strict schema + safety and writing rules; a bad draft gets ONE corrected regeneration, then a human

Chain (ModelConfig.chain): the primary, then one regeneration on the primary that carries the rejection reasons, then
(only when configured, OFF by default) ONE fallback model. The fallback runs ONLY after the primary failed for
AVAILABILITY reasons (connect error, rate limit 429, 5xx, timeout); never after a refusal, an empty completion or a
validator rejection, because that would be evading a refusal. The fallback has its own prices and reservation inside the
same caps and the same total-attempt bound.
A refusal by the provider/model, or an empty completion (how DeepSeek via OpenRouter refuses), is never re-asked: it goes straight to a human, as does every other rejected or unusable
answer (a handover plus an operator case that lists the outcomes and reasons). No canned line is ever produced. A stored
response is reused after a crash only when it was produced from exactly the same snapshot, so a restart never pays twice
or sends a stale answer.
"""
from dataclasses import dataclass
from datetime import timedelta
import os
import uuid

from onlyfans_runtime.store import Refused

from .model_client import (ChatRequest, ModelBusy, ModelCallError, ModelHTTPStatus, ModelIncompleteResponse,
                           ModelNotSent, ModelOutcomeUnknown, ModelUnavailable)
from .model_config import input_token_upper_bound
from .model_ledger import CONTENT_FAILURES, IDENTITY_UNSETTLED, LENGTH_LIMITED, ModelLedger
from .model_output import JudgeContext, Verdict, judge
from .prompt_registry import get_prompt_provider
from .writer_context import PromptBudget, build_context, classify_turn, offer_for
from .writer_examples import NullExamples

# Refusals come back as an explicit refusal OR as an EMPTY completion (known DeepSeek/OpenRouter behaviour). Neither is retried.
SILENT_OR_REFUSED = frozenset({'refusal', 'empty'})
LENGTH_REASON = 'the previous answer was cut off by the output length limit; write one shorter reply'


def length_verdict(verdict):
    """The provider stopped at the length limit: the text is incomplete (or empty because the limit was spent before any
    text). That is not a refusal and not a silent completion; it gets the normal single corrected, costed regeneration,
    never more. A genuine refusal in the text stays a refusal. Shared by the live path and by replay after a restart."""
    return verdict if verdict.outcome == 'refusal' else Verdict('invalid', reasons=(LENGTH_REASON,))


def is_availability_failure(outcome):
    """True when a failed call says the PROVIDER was unavailable (not that it declined or that we sent something bad)."""
    if outcome in ('not_sent', 'timeout', 'lease_expired'):
        return True
    if type(outcome) is str and outcome.startswith('http_') and outcome[5:].isdigit():
        status = int(outcome[5:])
        return status in (408, 429) or 500 <= status <= 599
    return False  # 400/401/403/404/422 (config, auth, moderation) and unclassified client errors never fall back


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
    handover: object  # callable(reason: str, detail: dict | None = None) -> None; moves the request to a human


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
            turn.handover('creator_profile_missing')
            raise Refused('creator facts are unavailable for this account; request handed to a human')
        scope = turn.snapshot.get('pilot_scope')
        if scope is not None:
            from .pilot_scope import _fingerprint
            from .writer_context import profile_data
            if _fingerprint(profile_data(profile)) != scope.get('creator_profile_hash'):
                raise Refused('creator profile does not match the planning snapshot')
        missing = profile.missing_inputs()
        if missing:
            turn.handover('creator_profile_incomplete', {'missing': list(missing)})
            raise Refused('creator profile is missing ' + ', '.join(missing) + '; request handed to a human')
        journey = turn.snapshot.get('journey') or {}
        if journey.get('human_required'):
            turn.handover('journey_' + str(journey['human_required']))
            raise Refused('this conversation needs a human; request handed to a human')
        latest = turn.snapshot['current_message']['body']
        if type(latest) is str and len(latest) > self.budget.latest_review_chars:
            # Explicit handled outcome for oversized input: no prompt is built, no model call or cost, and no invented answer.
            turn.handover('latest_input_oversized', {'chars': len(latest), 'limit': self.budget.latest_review_chars})
            raise Refused('latest message is too long to answer automatically; request handed to a human')
        if classify_turn(turn.snapshot['current_message']['body']) == 'role_question' and not self._identity_line(profile, journey):
            turn.handover('identity_no_approved_wording')
            raise Refused('no approved identity wording; request handed to a human')
        return profile

    @staticmethod
    def _identity_line(profile, journey):
        return (getattr(profile, 'role_statement', '') or journey.get('approved_identity_line') or '').strip()

    def _judge_context(self, turn, profile):
        snap = turn.snapshot
        scope_ids = {str(i['itemId']) for i in (snap.get('catalogue_search') or {}).get('items', [])}
        scope_ids |= {str(g['itemId']) for g in snap.get('offer_grants', [])}
        scope_ids |= {str(x) for x in snap.get('sales_scope', [])}
        scope_ids = {i for i in scope_ids if profile.may_offer(i)}  # only the creator-approved product(s)
        offer = offer_for(snap, profile, snap['current_message']['body'])
        return JudgeContext(snapshot=snap, profile=profile,
                            allowed_grant_ids=frozenset({offer['grantId']} if offer else ()),
                            allowed_scope_ids=frozenset(scope_ids),
                            identity_line=self._identity_line(profile, snap.get('journey') or {}))

    # ---- the chain -------------------------------------------------------------------
    def _reject(self, turn, reason, outcomes, reasons):
        """The writer produced nothing usable: visible human work (handover + operator case), never a silent loss."""
        turn.handover(reason, {'outcomes': list(outcomes), 'reasons': [str(r)[:160] for r in reasons][:8]})
        raise Refused('model could not write an acceptable reply; request handed to a human')

    def render(self, turn, profile, fix=()):
        """The prompt for one turn. MODEL-AGNOSTIC: it depends only on the snapshot, the creator profile, the examples and the
        rejection notes, never on which model will answer, so a simulation (or the fallback attempt) can send the SAME
        messages to either model."""
        body = turn.snapshot['current_message']['body']
        offer = offer_for(turn.snapshot, profile, body)
        history_hers = [t for who, t in build_context(turn.snapshot, profile, budget=self.budget).history if who == 'HER']
        examples = self.examples.retrieve(turn.account, body, allow_selling=offer is not None, exclude=frozenset(history_hers[-8:]))
        ctx = build_context(turn.snapshot, profile, examples=examples, fix=fix, budget=self.budget)
        return self.prompts.render(ctx)

    def propose(self, turn):
        cfg, ledger = self.config, self.ledger
        profile = self._preflight(turn)
        ledger.sweep(turn.account, turn.request_id, turn.generation)
        rows = ledger.rows(turn.account, turn.request_id, turn.generation)
        observed_seq = rows[-1].seq if rows else -1
        jctx = self._judge_context(turn, profile)
        chain = [(i, step, model, prefixed) for i, (step, model, prefixed) in enumerate(cfg.chain())]
        pos, dead, content_failed, fix, outcomes, refused = 0, set(), False, [], [], False
        unavailable, blocked = False, False  # primary failed for availability / failed in a way that never falls back
        now = self._clock()
        for row in rows:
            same = row.snapshot_sha256 == turn.snapshot_sha256
            if row.status in ('reserved', 'calling'):
                raise ModelBusy('a model call is already in flight for this request')
            if cfg.upstream_provider and row.response_text is not None and same and row.outcome in (None, 'ok', 'identity_unverified') + IDENTITY_UNSETTLED:
                turn.handover('model_identity_unverified', {'outcome': row.outcome or 'not_recorded'})
                raise Refused('model identity is unverified; request handed to a human')
            if row.response_text is not None and row.outcome in (None, 'ok', 'identity_verified', 'verified_ok', LENGTH_LIMITED):
                if not same:
                    continue  # produced from an older view or model configuration: never reuse
                verdict = judge(row.response_text, jctx)  # a stored answer: reuse, never pay twice
                if row.outcome == LENGTH_LIMITED:
                    verdict = length_verdict(verdict)  # the persisted completion evidence: same verdict as uninterrupted
                recorded_outcome = 'verified_ok' if cfg.upstream_provider and verdict.outcome == 'ok' else verdict.outcome
                if recorded_outcome != row.outcome:
                    ledger.set_outcome(turn.account, row.call_id, recorded_outcome)
                if verdict.outcome == 'ok':
                    turn.recheck()
                    return verdict.proposal
                pos, content_failed = max(pos, row.chain_pos + 1), True
                outcomes.append(verdict.outcome)
                fix = list(verdict.reasons)
                refused = refused or verdict.outcome in SILENT_OR_REFUSED
            elif row.outcome in CONTENT_FAILURES:
                if same:
                    pos, content_failed = max(pos, row.chain_pos + 1), True
                    outcomes.append(row.outcome)
                    refused = refused or row.outcome in SILENT_OR_REFUSED
            elif row.status == 'released' and row.outcome == 'lease_expired':
                continue  # never reached the wire: simply try that step again
            else:  # a transport-type failure: about the provider, not about the content
                if is_availability_failure(row.outcome):
                    unavailable = True
                else:
                    blocked = True  # 400/401/403 and unclassified failures never fall back
                if now - row.updated_at < timedelta(seconds=cfg.transport_retry_seconds):
                    dead.add(row.model)  # a recent failure: don't hammer that model
                    pos = max(pos, row.chain_pos + 1)
        if len(rows) >= cfg.max_calls_per_generation:
            turn.handover('model_call_limit', {'outcomes': [r.outcome or r.status for r in rows]})
            raise Refused('model call limit reached; request handed to a human')
        if refused:
            # A refusal (or the silent empty completion DeepSeek/OpenRouter return for one) is not re-asked in other
            # words or on another model/provider route: it is human work.
            self._reject(turn, 'model_empty' if outcomes and outcomes[-1] == 'empty' else 'model_refused', outcomes, fix)
        while True:
            def eligible(step):
                position, name, model, _ = step
                if position < pos or model in dead:
                    return False
                if name == 'fallback':
                    # Only after the primary was UNAVAILABLE; never after a content failure (refusal, empty, rejected).
                    return unavailable and not content_failed and not blocked
                return True
            step = next((s for s in chain if eligible(s)), None)
            if step is None:
                break
            chain_pos, chain_step, model, prefixed = step
            turn.recheck()  # stale work never spends money
            rendered = self.render(turn, profile, fix)
            size = input_token_upper_bound(rendered.messages)
            if size > cfg.max_input_tokens:
                # Never send (or pay for) a prompt above the configured bound; the same turn would render the same size.
                turn.handover('model_input_too_large', {'input_bound': size, 'limit': cfg.max_input_tokens})
                raise Refused('prompt exceeds the configured model input bound; request handed to a human')
            reservation = ledger.reserve(account=turn.account, request_id=turn.request_id, generation=turn.generation,
                fan_id=turn.fan_id, binding_id=turn.binding_id, owner_epoch=turn.owner_epoch, chain_pos=chain_pos,
                chain_step=chain_step, model=model, prefixed=prefixed, snapshot_sha256=turn.snapshot_sha256,
                prompt_sha256=rendered.sha256, worker_id=self.worker_id, provider=cfg.provider, expected_seq=observed_seq)
            observed_seq = reservation.seq
            ledger.mark_calling(turn.account, reservation.call_id, self.worker_id)
            request = ChatRequest(model=model, messages=rendered.messages, max_tokens=cfg.max_tokens,
                                  temperature=cfg.temperature, json_mode=True, timeout_seconds=cfg.timeout_seconds)
            try:
                result = self.client.chat(request)  # the only network step; no database transaction is open
            except (ModelNotSent, ModelHTTPStatus) as exc:
                ledger.release(turn.account, reservation.call_id, self.worker_id,
                               outcome=exc.error_class, error_class=exc.error_class)
                dead.add(model); pos = chain_pos + 1
                unavailable, blocked = unavailable or is_availability_failure(exc.error_class), blocked or not is_availability_failure(exc.error_class)
                continue
            except ModelOutcomeUnknown as exc:
                incomplete = isinstance(exc, ModelIncompleteResponse)
                ledger.mark_unknown(turn.account, reservation.call_id, self.worker_id,
                                    outcome='provider_incomplete' if incomplete else 'timeout', error_class=exc.error_class,
                                    generation_id=getattr(exc,'generation_id',''), endpoint_sha256=getattr(exc,'endpoint_sha256',''))
                dead.add(model); pos = chain_pos + 1
                if incomplete:
                    blocked = True  # an error-bearing partial response is not a fallback permission
                else:
                    unavailable = True  # unknown usage keeps its reservation
                continue
            except Exception as exc:  # unclassified client failure: assume it may have been charged
                ledger.mark_unknown(turn.account, reservation.call_id, self.worker_id, outcome='client_error',
                                    error_class=type(exc).__name__[:60])
                dead.add(model); pos = chain_pos + 1
                blocked = True  # not a proven availability failure: never falls back
                continue
            raw = result.text if type(getattr(result, 'text', None)) is str else ''
            oversize = len(raw.encode('utf-8')) > cfg.max_output_bytes
            text = raw.encode('utf-8')[:cfg.max_output_bytes].decode('utf-8', 'ignore') if oversize else raw
            ledger.settle(turn.account, reservation.call_id, self.worker_id, text_value=text, usage=result.usage,
                          identity_pending=bool(cfg.upstream_provider), generation_id=getattr(result,'generation_id',''),
                          endpoint_sha256=getattr(result,'endpoint_sha256',''),
                          finish_reason=getattr(result, 'finish_reason', None))
            returned_model = getattr(result, 'model', '') if type(getattr(result, 'model', '')) is str else ''
            returned_provider = getattr(result, 'provider', '') if type(getattr(result, 'provider', '')) is str else ''
            ledger.note_identity(turn.account, reservation.call_id, self.worker_id, requested=model,
                                 returned=returned_model, provider=returned_provider,
                                 prompt_version=getattr(self.prompts, 'name', ''), config_version=cfg.version(),
                                 chain_step=chain_step)
            identity_verified = (not cfg.upstream_provider or (returned_model == model
                and returned_provider.casefold() == cfg.upstream_reported_name.casefold()))
            try:
                turn.recheck()  # a stale answer still cannot be judged, planned or authorized
            except Refused:
                # A controlled stale-context refusal is not a lost response:
                # preserve its actual identity verdict without accepting its text.
                # Unexpected exceptions/crashes retain identity_pending, as before.
                if cfg.upstream_provider:
                    ledger.finish_identity(turn.account, reservation.call_id, self.worker_id, verified=identity_verified)
                raise
            if cfg.upstream_provider and not identity_verified:
                ledger.finish_identity(turn.account, reservation.call_id, self.worker_id, verified=False)
                # Do not auto-retry or use an unverified answer; handover still
                # requires the current conversation, as before.
                turn.handover('model_identity_unverified', {'requested_model': model,
                    'returned_model': returned_model[:128], 'expected_provider': cfg.upstream_reported_name,
                    'returned_provider': returned_provider[:64]})
                raise Refused('model identity is unverified; request handed to a human')
            if cfg.upstream_provider:
                ledger.finish_identity(turn.account, reservation.call_id, self.worker_id, verified=True)
            verdict = Verdict('invalid', reasons=('answer was longer than the output bound',)) if oversize else judge(text, jctx)
            if getattr(result, 'finish_reason', None) == 'length':
                verdict = length_verdict(verdict)  # already persisted by settle; replay reaches the same verdict
            recorded_outcome = 'verified_ok' if cfg.upstream_provider and verdict.outcome == 'ok' else verdict.outcome
            ledger.set_outcome(turn.account, reservation.call_id, recorded_outcome)
            if verdict.outcome == 'ok':
                return verdict.proposal
            content_failed = True
            outcomes.append(verdict.outcome)
            fix = list(verdict.reasons)
            pos = chain_pos + 1
            if verdict.outcome in SILENT_OR_REFUSED:
                self._reject(turn, 'model_empty' if verdict.outcome == 'empty' else 'model_refused', outcomes, fix)
        if content_failed:
            self._reject(turn, 'model_rejected', outcomes, fix)
        raise ModelUnavailable('model is unavailable; the request stays open')
