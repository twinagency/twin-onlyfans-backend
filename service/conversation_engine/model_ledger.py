"""Durable model-call ledger: reserve before the call, settle after, release on failure, count unknown as spent.

Every function opens its own short transaction. Nothing here is ever held open across a provider call.

Cost accounting, per call row (micro-USD):
  reserved / calling  -> counts `reserved_microusd` (the call may be in flight)
  settled             -> counts `actual_microusd` (usage known)
  unknown             -> counts `reserved_microusd` (request may have been charged; conservative)
  released            -> counts 0 (proved not sent, or the provider refused without a completion)

`reserved_microusd` is ModelConfig.call_bound_microusd(): every allowed input token and output token at the configured
price, i.e. an upper bound, not an estimate. Before a call is reserved the caps must also have headroom for every
attempt this request generation is still allowed to make (the retry allowance), so a chain of retries cannot
overshoot a cap. An `unknown` call keeps its full reservation until `reconcile_unknown` records its real cost.

Leases: a reservation carries a lease. One live row per (account, request, generation, seq), and a new
reservation is refused while another worker holds an unexpired lease on the same request generation, so two
workers produce one invocation. An expired `reserved` lease (never reached the wire) is released; an expired
`calling` lease (may have reached the wire) becomes `unknown`, still counted against the cap.
"""
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import json
import logging
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from db.onlyfans_engine_models import OfEngineModelCall, OfEngineOwner, OfEngineRequest
from onlyfans_runtime.store import Refused, utcnow

from .model_client import ModelBudgetExhausted, ModelBusy

log = logging.getLogger('conversation_engine.model')
_BUDGET_LOCK = 7811071
CONTENT_FAILURES = frozenset({'refusal', 'malformed', 'invalid', 'empty'})


@dataclass(frozen=True)
class Reservation:
    call_id: str
    seq: int
    cost_day: object


@dataclass(frozen=True)
class CallRow:
    call_id: str
    seq: int
    chain_pos: int
    chain_step: str
    model: str
    status: str
    outcome: str | None
    snapshot_sha256: str
    response_text: str | None
    updated_at: object
    reserved_microusd: int
    actual_microusd: int | None


def _row(r):
    return CallRow(r.call_id, r.seq, r.chain_pos, r.chain_step, r.model, r.status, r.outcome, r.snapshot_sha256,
                   r.response_text, r.updated_at, r.reserved_microusd, r.actual_microusd)


def _counted():
    return func.coalesce(func.sum(text("CASE status WHEN 'released' THEN 0 WHEN 'settled' THEN actual_microusd ELSE reserved_microusd END")), 0)


class ModelLedger:
    def __init__(self, engine, config, *, clock=utcnow):
        self.engine, self.config, self.clock = engine, config, clock

    # ---- lease recovery ------------------------------------------------------
    def _expire(self, s, rows, now):
        for r in rows:
            if r.lease_expires_at > now:
                continue
            if r.status == 'reserved':
                r.status, r.outcome, r.updated_at = 'released', 'lease_expired', now
            elif r.status == 'calling':
                r.status, r.outcome, r.error_class, r.updated_at = 'unknown', 'lease_expired', 'lease_expired', now

    def sweep(self, account, request_id, generation):
        with Session(self.engine) as s, s.begin():
            rows = list(s.scalars(select(OfEngineModelCall).where(OfEngineModelCall.account_id == account,
                OfEngineModelCall.request_id == request_id, OfEngineModelCall.generation == generation,
                OfEngineModelCall.status.in_(('reserved', 'calling'))).with_for_update()))
            before = sum(1 for r in rows if r.status in ('reserved', 'calling'))
            self._expire(s, rows, self.clock())
            return before - sum(1 for r in rows if r.status in ('reserved', 'calling'))

    def rows(self, account, request_id, generation):
        with Session(self.engine) as s:
            return [_row(r) for r in s.scalars(select(OfEngineModelCall).where(OfEngineModelCall.account_id == account,
                OfEngineModelCall.request_id == request_id, OfEngineModelCall.generation == generation)
                .order_by(OfEngineModelCall.seq))]

    # ---- reservation -----------------------------------------------------------
    def committed(self, *, day, account=None):
        with Session(self.engine) as s:
            return self._committed(s, day, account)

    @staticmethod
    def _committed(s, day, account):
        query = select(_counted()).select_from(OfEngineModelCall).where(OfEngineModelCall.cost_day == day)
        if account is not None:
            query = query.where(OfEngineModelCall.account_id == account)
        return int(s.scalar(query) or 0)

    def reserve(self, *, account, request_id, generation, fan_id, binding_id, owner_epoch, chain_pos, chain_step,
                model, prefixed, snapshot_sha256, prompt_sha256, worker_id, provider):
        cfg = self.config
        bound = cfg.call_bound_microusd(model)
        if bound <= 0:
            raise Refused('model price and token bounds are not configured')
        with Session(self.engine) as s, s.begin():
            # One global budget lock: caps are checked and the reservation inserted atomically.
            s.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': _BUDGET_LOCK})
            now = self.clock()
            request = s.get(OfEngineRequest, (account, request_id), with_for_update=True)
            owner = s.get(OfEngineOwner, (account, fan_id))
            if (request is None or request.status != 'open' or request.binding_id != binding_id
                    or owner is None or owner.mode != 'active' or owner.binding_id != binding_id or owner.epoch != owner_epoch):
                raise Refused('conversation changed during generation')
            existing = list(s.scalars(select(OfEngineModelCall).where(OfEngineModelCall.account_id == account,
                OfEngineModelCall.request_id == request_id, OfEngineModelCall.generation == generation)
                .order_by(OfEngineModelCall.seq).with_for_update()))
            self._expire(s, existing, now)
            if any(r.status in ('reserved', 'calling') for r in existing):
                raise ModelBusy('a model call is already in flight for this request')
            remaining = cfg.max_calls_per_generation - len(existing)
            if remaining < 1:
                raise Refused('model call limit reached for this request')
            day = now.date()
            # Headroom for THIS call (this model's bound) and every attempt still allowed for the generation, each at
            # the largest bound of any model this config can call, so a fallback can never overshoot a cap.
            need = bound + cfg.max_call_bound_microusd() * (remaining - 1)
            if cfg.daily_cap_microusd <= 0 or self._committed(s, day, None) + need > cfg.daily_cap_microusd:
                raise ModelBudgetExhausted('daily model budget exhausted')
            if cfg.creator_cap_microusd <= 0 or self._committed(s, day, account) + need > cfg.creator_cap_microusd:
                raise ModelBudgetExhausted('creator model budget exhausted')
            seq = (existing[-1].seq + 1) if existing else 0
            call_id = str(uuid.uuid4())
            s.add(OfEngineModelCall(account_id=account, call_id=call_id, request_id=request_id, generation=generation,
                seq=seq, chain_pos=chain_pos, chain_step=chain_step, fan_id=fan_id, binding_id=binding_id,
                owner_epoch=owner_epoch, provider=provider, model=model, prefixed=prefixed, status='reserved',
                reserved_microusd=bound, usage_known=False, lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=cfg.lease_seconds), attempt=1,
                snapshot_sha256=snapshot_sha256, prompt_sha256=prompt_sha256, cost_day=day,
                created_at=now, updated_at=now))
            return Reservation(call_id, seq, day)

    # ---- transitions -------------------------------------------------------------
    def _mine(self, s, account, call_id, worker_id, *statuses):
        row = s.get(OfEngineModelCall, (account, call_id), with_for_update=True)
        if row is None or row.lease_owner != worker_id or row.status not in statuses:
            raise Refused('model call lease is no longer held')
        return row

    def mark_calling(self, account, call_id, worker_id):
        """Last step before the wire: only the live lease holder may proceed, and the lease must not have lapsed."""
        with Session(self.engine) as s, s.begin():
            row = self._mine(s, account, call_id, worker_id, 'reserved')
            now = self.clock()
            lapsed = row.lease_expires_at <= now
            if lapsed:
                self._expire(s, [row], now)  # committed below: a lapsed reservation is released, not left reserved
            else:
                row.status, row.wire_started_at, row.updated_at = 'calling', now, now
        if lapsed:
            raise Refused('model call lease expired before the call')

    def settle(self, account, call_id, worker_id, *, text_value, usage, identity_pending=False):
        if type(identity_pending) is not bool:
            raise Refused('invalid identity verification state')
        cfg = self.config
        cost = usage.cost_microusd
        with Session(self.engine) as s, s.begin():
            row = s.get(OfEngineModelCall, (account, call_id), with_for_update=True)
            if row is None or row.lease_owner != worker_id or row.status not in ('calling', 'unknown'):
                raise Refused('model call lease is no longer held')
            prompt_price, completion_price = cfg.prices_for(row.model)  # THIS call's model, not the primary's
            if cost is None and usage.prompt_tokens is not None and usage.completion_tokens is not None and (
                    prompt_price or completion_price):
                cost = -(-(usage.prompt_tokens * prompt_price + usage.completion_tokens * completion_price) // 1_000_000)
            now = self.clock()
            row.response_text = text_value
            row.response_sha256 = sha256(text_value.encode('utf-8')).hexdigest()
            if identity_pending:
                row.outcome = 'identity_pending'  # never replay until the provider identity is verified
            row.prompt_tokens, row.completion_tokens = usage.prompt_tokens, usage.completion_tokens
            row.settled_at = row.updated_at = now
            if cost is not None:
                row.status, row.actual_microusd, row.usage_known = 'settled', int(cost), True
                if int(cost) > row.reserved_microusd and row.error_class is None:
                    row.error_class = 'cost_over_bound'  # the configured prices are stale: the operator must see it
            else:
                row.status, row.actual_microusd, row.usage_known = 'unknown', None, False  # counted at the reservation
                row.error_class = 'usage_missing'
            return row.status

    def release(self, account, call_id, worker_id, *, outcome, error_class):
        with Session(self.engine) as s, s.begin():
            row = self._mine(s, account, call_id, worker_id, 'reserved', 'calling')
            row.status, row.outcome, row.error_class, row.updated_at = 'released', outcome, error_class, self.clock()

    def mark_unknown(self, account, call_id, worker_id, *, outcome, error_class):
        with Session(self.engine) as s, s.begin():
            row = self._mine(s, account, call_id, worker_id, 'calling')
            row.status, row.outcome, row.error_class, row.updated_at = 'unknown', outcome, error_class, self.clock()

    def finish_identity(self, account, call_id, worker_id, *, verified):
        if type(verified) is not bool:
            raise Refused('invalid model identity verdict')
        with Session(self.engine) as s, s.begin():
            row = s.get(OfEngineModelCall, (account, call_id), with_for_update=True)
            if (row is None or row.lease_owner != worker_id or row.status not in ('settled', 'unknown')
                    or row.outcome != 'identity_pending'):
                raise Refused('model identity verification is no longer current')
            row.outcome = 'identity_verified' if verified else 'identity_unverified'
            row.updated_at = self.clock()

    def set_outcome(self, account, call_id, outcome):
        with Session(self.engine) as s, s.begin():
            row = s.get(OfEngineModelCall, (account, call_id), with_for_update=True)
            if (row is not None and row.status in ('settled', 'unknown')
                    and row.outcome in (None, 'ok', 'identity_verified', 'verified_ok') + tuple(CONTENT_FAILURES)):
                row.outcome, row.updated_at = outcome, self.clock()

    # ---- identity, reconciliation -----------------------------------------------------
    def note_identity(self, account, call_id, worker_id, *, requested, returned, provider='', prompt_version='',
                      config_version='', chain_step=''):
        """Record who really answered. The ledger has no column for the returned id, so a DIFFERENT returned model is
        flagged in `error_class` (`ret_model:<id>`), and the full identity goes to the structured log line below.

        Returns the identity dict. A response that names no model is recorded as 'not_reported', never as a match.
        """
        identity = {'call_id': call_id, 'account': account, 'requested_model': requested,
                    'returned_model': returned or None, 'returned_provider': provider or None,
                    'prompt_version': prompt_version, 'config_version': config_version,
                    'chain_step': chain_step, 'is_fallback': chain_step == 'fallback'}
        if not returned or returned != requested:
            tag = ('ret_model:' + returned) if returned else 'ret_model:not_reported'
            with Session(self.engine) as s, s.begin():
                row = s.get(OfEngineModelCall, (account, call_id), with_for_update=True)
                if row is not None and row.lease_owner == worker_id and row.error_class is None:
                    row.error_class = tag[:64]
        log.info('model_call_identity %s', json.dumps(identity, sort_keys=True))
        return identity

    def unreconciled(self, *, day=None):
        """`unknown` calls still counted at their full reservation: the operator's reconciliation list."""
        with Session(self.engine) as s:
            query = select(OfEngineModelCall).where(OfEngineModelCall.status == 'unknown')
            if day is not None:
                query = query.where(OfEngineModelCall.cost_day == day)
            return [_row(r) for r in s.scalars(query.order_by(OfEngineModelCall.created_at))]

    def reconcile_unknown(self, account, call_id, *, actual_microusd):
        """Replace an `unknown` call's reservation with its verified real cost (0 when the provider proves no charge)."""
        if type(actual_microusd) is not int or isinstance(actual_microusd, bool) or actual_microusd < 0:
            raise Refused('reconciled cost must be a non-negative integer')
        with Session(self.engine) as s, s.begin():
            row = s.get(OfEngineModelCall, (account, call_id), with_for_update=True)
            if row is None or row.status != 'unknown':
                raise Refused('only an unknown model call can be reconciled')
            row.status, row.actual_microusd, row.usage_known, row.settled_at = 'settled', actual_microusd, True, self.clock()
            row.updated_at = row.settled_at
            row.error_class = ('reconciled:' + (row.error_class or ''))[:64]
