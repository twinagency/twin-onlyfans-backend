"""Transactional intake, fair work claims, control evidence, and catch-up.

Every method owns one short transaction. Inputs come from a reader already
bound to an account; no public ingest endpoint exists. Provider/model calls
must occur outside these transactions. Returned work is an admission token,
never permission to send. A delivery authority is a later integration gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import uuid

from sqlalchemy import case, create_engine, or_, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from db.models import Chat, Fan, Message, OfConversationState, OfCreatorBinding, OfMessageDelivery
from db.onlyfans_models import OfControlAudit, OfIngress, OfRecovery, OfRuntime, OfWork, OfScan
from onlyfans_read.contract import UNRESOLVED_OUTCOMES, ID_PATTERN

MAX_PENDING = 1000
PAGE_LIMIT = 100
QUIET_SECONDS = 7
MAX_BURST_SECONDS = 30
LEASE_SECONDS = 30


class Refused(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise Refused("invalid numeric identity")
    return value


@dataclass(frozen=True)
class Event:
    fan_id: int
    message_id: int
    created_at: str
    direction: str
    body: str
    media_ids: tuple[int, ...] = ()
    price_cents: int = 0

    def normalized(self):
        _id(self.fan_id)
        _id(self.message_id)
        if self.direction not in {"in", "out"} or not isinstance(self.body, str) or len(self.body.encode()) > 20000:
            raise Refused("invalid message")
        if type(self.price_cents) is not int or not 0 <= self.price_cents <= 100_000_000:
            raise Refused("invalid integer money")
        if len(self.media_ids) > 100:
            raise Refused("too many media references")
        media = tuple(_id(v) for v in self.media_ids)
        try:
            date = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise Refused("invalid provider timestamp") from exc
        if date.tzinfo is None:
            raise Refused("provider timestamp must include timezone")
        stamp = date.astimezone(timezone.utc).isoformat()
        return Event(self.fan_id, self.message_id, stamp, self.direction, self.body, media, self.price_cents)

    def payload(self):
        return json.dumps(asdict(self.normalized()), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class Claim:
    account_id: str
    fan_id: int
    generation: int
    token: str
    control_revision: int
    target_message_id: int
    input_revision: int = 0


@dataclass(frozen=True)
class RecoveryClaim:
    account_id: str
    fan_id: int
    generation: int
    token: str
    high_water: int
    upper_bound: int | None
    before_id: int | None


@dataclass(frozen=True)
class ScanClaim:
    account_id: str
    generation: int
    token: str
    cursor: str | None



def fan_current_override(account_id):
    """Explicit owner-approved fans that may be served before the account-wide
    recent recovery finishes: ONLYFANS_FAN_CURRENT_OVERRIDE="acct:fan,acct:fan"."""
    out = set()
    for part in os.environ.get("ONLYFANS_FAN_CURRENT_OVERRIDE", "").split(","):
        account, _, fan = part.strip().partition(":")
        if account == account_id and fan.isdigit():
            out.add(int(fan))
    return out


def fan_history_current(s, account_id, fan_id):
    """An override fan whose own recent recovery is complete and unquarantined."""
    if fan_id not in fan_current_override(account_id):
        return False
    row = s.get(OfRecovery, (account_id, fan_id))
    quarantined = s.scalar(select(OfIngress.event_key).where(OfIngress.account_id == account_id,
        OfIngress.fan_id == fan_id, OfIngress.status == "quarantined").limit(1)) if hasattr(OfIngress, "fan_id") else None
    # The fan's own ingress must be fully applied (account-wide pending events
    # from other fans do not block this fan).
    unapplied = s.scalar(select(OfIngress.event_key).where(OfIngress.account_id == account_id,
        OfIngress.fan_id == fan_id, OfIngress.status != "applied").limit(1))
    return row is not None and row.status == "complete" and quarantined is None and unapplied is None

class Store:
    def __init__(self, url: str):
        parsed = make_url(url)
        if parsed.get_backend_name() != "postgresql":
            raise Refused("durable intake requires PostgreSQL")
        self.engine = create_engine(parsed.set(drivername="postgresql+psycopg2"), pool_size=2,
                                    max_overflow=0, pool_timeout=3, pool_pre_ping=True,
                                    connect_args={"options": "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=3000"})
        from db.schema import check_schema
        try:
            with self.engine.connect() as connection:
                check_schema(connection)
        except Exception:
            self.engine.dispose()
            raise

    def close(self):
        self.engine.dispose()

    @staticmethod
    def _runtime(s, tenant_id, account_id):
        # Lock ordering is runtime -> binding -> conversation everywhere.
        runtime = s.scalar(select(OfRuntime).where(OfRuntime.account_id == account_id).with_for_update())
        if runtime is None:
            raise Refused("scope unavailable")
        binding = s.scalar(select(OfCreatorBinding).where(
            OfCreatorBinding.id == runtime.binding_id,
            OfCreatorBinding.account_id == account_id,
            OfCreatorBinding.tenant_id == tenant_id,
            OfCreatorBinding.channel == "onlyfans",
            OfCreatorBinding.revoked_at.is_(None),
        ).with_for_update(read=True))
        from onlyfans_read.agency_scope import creator_allowed
        if binding is None or not creator_allowed(binding.creator_id):
            raise Refused("scope unavailable")
        return runtime

    @staticmethod
    def _read_connection(s, runtime, revision):
        """Fence inactive inbox work to the verified connection we loaded.

        Connection commands lock the same runtime before changing a revision.
        Recheck at both claim and commit; no DB lock spans a provider request.
        """
        if revision is None:
            return
        if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker"
                or type(revision) is not int or revision < 1):
            raise Refused("invalid inbox connection revision")
        from db.onlyfans_connection_models import OfProviderConnection
        connection = s.get(OfProviderConnection, runtime.account_id)
        if (connection is None or connection.binding_id != runtime.binding_id
                or connection.revision != revision or connection.status != "verified"
                or connection.last_error is not None
                or connection.verified_at is None or connection.expires_at <= utcnow()):
            raise Refused("inbox connection is no longer current")

    def register(self, tenant_id, account_id, *, activated_at, enabled=False):
        if activated_at.tzinfo is not None:
            activated_at = activated_at.astimezone(timezone.utc).replace(tzinfo=None)
        with Session(self.engine) as s, s.begin():
            binding = s.scalar(select(OfCreatorBinding).where(
                OfCreatorBinding.account_id == account_id, OfCreatorBinding.tenant_id == tenant_id,
                OfCreatorBinding.revoked_at.is_(None), OfCreatorBinding.channel == "onlyfans",
            ).with_for_update())
            if binding is None:
                raise Refused("scope unavailable")
            # Registration is deliberately not an update/allowance reset.
            if s.get(OfRuntime, account_id) is not None:
                raise Refused("runtime already registered")
            s.add(OfRuntime(account_id=account_id, binding_id=binding.id, activated_at=activated_at, enabled=enabled))

    def record(self, tenant_id, account_id, event: Event, *, source="ws"):
        payload = event.payload()
        if source not in {"ws", "rest", "catchup", "import"}:
            raise Refused("invalid intake source")
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, account_id)
            return self._record(s, runtime, event, payload, source, utcnow())

    def _record(self, s, runtime, event, payload, source, now):
        key = hashlib.sha256(payload.encode()).hexdigest()
        if s.get(OfIngress, (runtime.account_id, key)):
            return "duplicate"
        if runtime.pending_events >= MAX_PENDING:
            runtime.recovery_needed = True
            scan = s.get(OfScan, runtime.account_id)
            if scan is None:
                scan = OfScan(account_id=runtime.account_id)
                s.add(scan)
                s.flush()
                self._begin_cycle(s, scan)
            elif scan.status == "complete":
                self._begin_cycle(s, scan)
            else:
                scan.repeat_requested = True
            return "backpressure"  # Caller must not acknowledge; catch-up remains required.
        s.add(OfIngress(account_id=runtime.account_id, binding_id=runtime.binding_id, event_key=key,
                        fan_id=event.fan_id, message_id=event.message_id, payload=payload,
                        source=source, received_at=now))
        runtime.pending_events += 1
        s.flush()
        return "accepted"

    def drain(self, tenant_id, account_id, *, limit=PAGE_LIMIT, connection_revision=None):
        if not 1 <= limit <= PAGE_LIMIT:
            raise Refused("invalid batch limit")
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, account_id)
            self._read_connection(s, runtime, connection_revision)
            events = s.scalars(select(OfIngress).where(OfIngress.account_id == account_id,
                               OfIngress.status == "pending").order_by(OfIngress.received_at, OfIngress.event_key).limit(limit)).all()
            for row in events:
                if connection_revision is None:
                    self._apply(s, runtime, row)
                else:
                    self._apply(s, runtime, row, history_only=True)
            self._mark_current(s, runtime)
            return len(events)

    @staticmethod
    def _state(s, account_id, fan_id):
        state = s.get(OfConversationState, (account_id, fan_id))
        if state is None:
            # Absence of a migrated control record is not consent to automate.
            state = OfConversationState(account_id=account_id, fan_id=fan_id, mode="paused", revision=0,
                                        takeover_state="none", requires_attention=True)
            s.add(state)
            s.flush()
        return state

    def _apply(self, s, runtime, row, *, history_only=False):
        event = Event(**json.loads(row.payload)).normalized()
        account = runtime.account_id
        now = utcnow()
        key = (account, event.fan_id, event.message_id)
        existing = s.get(Message, key)
        if existing is not None:
            if s.get(Fan, (account, event.fan_id)) is None:
                s.add(Fan(account_id=account, fan_id=event.fan_id))
                s.flush()
            same = (existing.direction == event.direction and existing.body == event.body and
                    existing.price_cents == event.price_cents and json.loads(existing.media_ids or "[]") == list(event.media_ids))
            row.status = "applied" if same else "quarantined"
            row.reason = None if same else "conflicting_message_identity"
            if not same:
                state = self._state(s, account, event.fan_id)
                state.requires_attention = True
                runtime.recovery_needed = True
            row.applied_at = now
            runtime.pending_events -= 1
            return
        if row.binding_id != runtime.binding_id:
            row.status, row.reason, row.applied_at = "quarantined", "binding_changed", now
            runtime.recovery_needed = True
            runtime.pending_events -= 1
            return
        fan = s.get(Fan, (account, event.fan_id))
        new_fan = fan is None
        if fan is None:
            fan = Fan(account_id=account, fan_id=event.fan_id)
            s.add(fan)
            s.flush()
        new_state = s.get(OfConversationState, (account, event.fan_id)) is None
        state = self._state(s, account, event.fan_id)
        stamp = datetime.fromisoformat(event.created_at).replace(tzinfo=None)
        if stamp > now + timedelta(minutes=5):
            row.status, row.reason, row.applied_at = "quarantined", "future_timestamp", now
            state.requires_attention = True
            runtime.recovery_needed = True
            runtime.pending_events -= 1
            return
        historical = history_only or row.source == "import" or stamp < runtime.activated_at
        # Older imports outside the latest-100 prompt window cannot change a reply; they must not
        # invalidate a live claim. Anything live, or inside the window, still advances the revision.
        from .launch_history_memory.recovery import affects_recent_context
        advances = not historical or affects_recent_context(s, account, event.fan_id, event)
        if advances:
            state.input_revision += 1
        s.add(Message(account_id=account, fan_id=event.fan_id, message_id=event.message_id,
                      direction=event.direction, body=event.body, media_ids=json.dumps(event.media_ids),
                      media_count=len(event.media_ids), price_cents=event.price_cents,
                      created_at=stamp, raw_json=row.payload))
        chat = s.get(Chat, (account, event.fan_id))
        if chat is None:
            chat = Chat(account_id=account, fan_id=event.fan_id, unread_count=0)
            s.add(chat)
        if chat.last_message_at is None or (stamp, event.message_id) > (chat.last_message_at, chat.last_message_id or 0):
            chat.last_message_id, chat.last_message_at, chat.last_message_preview = event.message_id, stamp, event.body[:500]
        if new_fan and new_state and event.direction == "in" and not historical and runtime.enabled and not fan.is_muted:
            from db.onlyfans_connection_models import OfProviderConnection
            connection=s.get(OfProviderConnection,account)
            if (connection is not None and connection.binding_id==runtime.binding_id and
                connection.status=="verified" and connection.verified_at is not None and connection.expires_at>now and
                connection.automate_new_conversations and connection.automation_starts_at is not None and stamp>=connection.automation_starts_at):
                # The operator explicitly armed FUTURE new conversations. No
                # existing state, old history, takeover or stop is resumed here.
                state.mode="automated";state.revision=1;state.requires_attention=False;state.updated_at=now
                command=hashlib.sha256(f"initial-automation:{runtime.binding_id}:{event.fan_id}".encode()).hexdigest()
                s.add(OfControlAudit(account_id=account,command_key=command,fan_id=event.fan_id,mode="automated",
                    previous_revision=0,revision=1,actor=f"account-policy:{connection.revision}",
                    reason="Operator account policy enabled a new inbound conversation.",
                    consent_ref=f"connection:{connection.revision}",action="initial_automation",
                    takeover_from="none",takeover_to="none",created_at=now))
        if event.direction == "in" and not historical:
            if fan.last_message_received_at is None or stamp > fan.last_message_received_at:
                fan.last_message_received_at = stamp
            chat.unread_count += 1
            eligible = runtime.enabled and state.mode == "automated" and state.takeover_state == "none" and not state.unresolved_attempt_id and not fan.is_muted
            work = s.get(OfWork, (account, event.fan_id))
            if work is None:
                work = OfWork(account_id=account, fan_id=event.fan_id, generation=0, first_input_at=now,
                              last_input_at=now, target_message_id=event.message_id, control_revision=state.revision,
                              due_at=now, status="blocked")
                s.add(work)
            if work.status in {"done", "blocked"}:
                work.first_input_at = now
            # New input invalidates any former planning lease, even if it arrived
            # out of order. The hard burst deadline does not move indefinitely.
            work.generation += 1
            work.last_input_at = now
            work.due_at = min(now + timedelta(seconds=QUIET_SECONDS), work.first_input_at + timedelta(seconds=MAX_BURST_SECONDS))
            work.target_message_id = max(work.target_message_id, event.message_id)
            work.control_revision = state.revision
            work.status = "waiting" if eligible else "blocked"
            work.lease_token = work.lease_until = None
        if event.direction == "in" and advances:
            # Event notification shares the runtime lock; final dispatch still
            # revalidates if best-effort cancellation could not be recorded.
            s.flush()
            from .launch_engagement.events import cancel_for_fan
            cancel_for_fan(s, account, event.fan_id, "reply", now=now)
        row.status, row.applied_at = "applied", now
        runtime.pending_events -= 1
        s.flush()

    def control(self, tenant_id, account_id, fan_id, *, mode, expected_revision, command_key, actor, reason, consent_ref=None, action=None):
        _id(fan_id)
        if mode not in {"automated", "human_controlled", "paused", "no_contact"}:
            raise Refused("invalid control mode")
        actions = {"pause_automation":"paused", "resume_automation":"automated", "mark_no_contact":"no_contact",
                   "request_takeover":"human_controlled", "release_takeover":"automated"}
        if action is not None and (type(action) is not str or action not in actions or mode != actions[action] or consent_ref is not None):
            raise Refused("invalid scoped control action")
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**31-1:
            raise Refused("invalid control revision")
        if not 1 <= len(command_key) <= 64 or not 1 <= len(actor) <= 128 or not 1 <= len(reason) <= 512 or (consent_ref is not None and not 1 <= len(consent_ref) <= 128):
            raise Refused("invalid audit evidence")
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, account_id)
            prior = s.get(OfControlAudit, (account_id, command_key))
            if prior:
                if (prior.fan_id, prior.mode, prior.previous_revision, prior.actor, prior.reason, prior.consent_ref, prior.action) != (fan_id, mode, expected_revision, actor, reason, consent_ref, action):
                    raise Refused("idempotency key conflict")
                return prior.revision
            if s.get(Fan, (account_id, fan_id)) is None:
                raise Refused("conversation unavailable")
            state = self._state(s, account_id, fan_id)
            if state.revision != expected_revision:
                raise Refused("control revision conflict")
            if state.mode == "no_contact" and mode != "no_contact" and not consent_ref:
                raise Refused("recorded renewed consent is required")
            before_takeover = state.takeover_state
            if action == "pause_automation" and state.mode != "automated":
                raise Refused("replies are not automated")
            if action == "resume_automation" and state.mode != "paused":
                raise Refused("replies are not paused")
            if action == "release_takeover" and state.mode != "human_controlled":
                raise Refused("human control is not active")
            from .handover import staff_owns_takeover
            if action == "release_takeover" and not staff_owns_takeover(s, account_id, fan_id, state, actor):
                raise Refused("takeover belongs to another team member")
            if action in {"resume_automation", "release_takeover"}:
                if self._unresolved(s, account_id, fan_id, state):
                    raise Refused("unresolved delivery holds resumption")
                if action == "resume_automation" and state.takeover_state != "none":
                    raise Refused("explicit hand-back is required")
                if action == "release_takeover" and state.takeover_state != "effective":
                    raise Refused("takeover is not effective")
            if action == "request_takeover":
                if state.takeover_state != "none":
                    raise Refused("takeover already requested")
                state.takeover_state = "requested" if self._unresolved(s, account_id, fan_id, state) else "effective"
            elif action == "release_takeover":
                state.takeover_state = "none"
            state.mode, state.revision, state.updated_at = mode, state.revision + 1, utcnow()
            # Never clear an unresolved send or takeover as a side effect of resume.
            work = s.get(OfWork, (account_id, fan_id))
            if work:
                work.generation += 1
                work.status = "blocked"
                work.lease_token = work.lease_until = None
            s.add(OfControlAudit(account_id=account_id, command_key=command_key, fan_id=fan_id, mode=mode,
                                 previous_revision=expected_revision, revision=state.revision, actor=actor,
                                 reason=reason, consent_ref=consent_ref, action=action,
                                 takeover_from=before_takeover if action is not None else None,
                                 takeover_to=state.takeover_state if action is not None else None, created_at=utcnow()))
            s.flush()
            from .launch_engagement.events import cancel_for_fan
            cancel_for_fan(s, account_id, fan_id, {"human_controlled": "takeover", "paused": "pause",
                "no_contact": "no_contact"}.get(mode, "operator_cancelled"), now=utcnow())
            return state.revision

    @staticmethod
    def complete_takeover(s, account_id, fan_id):
        """Receipt projection calls this under the same runtime lock as controls.

        Settling a send can finish a requested handover; it cannot resume replies,
        clear attention, or undo a later pause/no-contact decision.
        """
        state = s.get(OfConversationState, (account_id, fan_id))
        s.flush()
        if state.mode != "human_controlled" or state.takeover_state != "requested" or Store._unresolved(s, account_id, fan_id, state):
            return
        previous = state.revision
        state.takeover_state, state.revision, state.updated_at = "effective", previous + 1, utcnow()
        key = hashlib.sha256(f"takeover-effective:{account_id}:{fan_id}:{previous}".encode()).hexdigest()
        s.add(OfControlAudit(account_id=account_id, command_key=key, fan_id=fan_id, mode=state.mode,
            previous_revision=previous, revision=state.revision, actor="delivery-authority",
            reason="Delivery evidence settled the requested takeover", action="complete_takeover",
            takeover_from="requested", takeover_to="effective", created_at=utcnow()))

    def claim(self, tenant_id, *, account_ids=None):
        if account_ids is not None and (type(account_ids) is not tuple or not 1 <= len(account_ids) <= 32
                or any(type(value) is not str or not ID_PATTERN.fullmatch(value) for value in account_ids)
                or len(set(account_ids)) != len(account_ids)):
            raise Refused("invalid worker account scope")
        now = utcnow()
        due = or_(OfWork.status == "waiting", (OfWork.status == "claimed") & (OfWork.lease_until <= now))
        with Session(self.engine) as s, s.begin():
            query = select(OfRuntime).join(OfCreatorBinding, OfCreatorBinding.id == OfRuntime.binding_id).where(
                OfRuntime.enabled.is_(True), OfCreatorBinding.tenant_id == tenant_id,
                OfCreatorBinding.revoked_at.is_(None),
                select(OfWork.account_id).where(OfWork.account_id == OfRuntime.account_id, due, OfWork.due_at <= now).exists(),
            )
            if account_ids is not None:query = query.where(OfRuntime.account_id.in_(account_ids))
            while True:
                account = s.scalar(query.order_by(OfRuntime.last_claim_at, OfRuntime.account_id).with_for_update(skip_locked=True, of=OfRuntime).limit(1))
                if account is None:
                    return None
                # Recheck binding under the lock so a concurrent revocation is ordered.
                self._runtime(s, tenant_id, account.account_id)
                work = s.scalar(select(OfWork).where(OfWork.account_id == account.account_id, due, OfWork.due_at <= now)
                                .order_by(OfWork.last_claim_at, OfWork.first_input_at, OfWork.fan_id).limit(1))
                if work is not None:
                    break
                # READ COMMITTED can select an account from an earlier snapshot
                # just as another worker exhausts it. Its runtime lock now keeps
                # it empty; reselect with a fresh snapshot and the same fairness
                # ordering/scope instead of dropping other eligible accounts.
            state = self._state(s, account.account_id, work.fan_id)
            fan = s.get(Fan, (account.account_id, work.fan_id))
            account.last_claim_at = work.last_claim_at = now
            if state.mode != "automated" or state.takeover_state != "none" or self._unresolved(s, account.account_id, work.fan_id, state) or fan.is_muted:
                work.status = "blocked"
                return None
            work.generation += 1
            work.lease_token = str(uuid.uuid4())
            work.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            work.status, work.control_revision = "claimed", state.revision
            return Claim(account.account_id, work.fan_id, work.generation, work.lease_token, state.revision, work.target_message_id, state.input_revision)

    def hold_claim(self, tenant_id, claim):
        """Release only the exact current lease; never disturb newer input/control."""
        if type(claim) is not Claim:raise Refused("invalid worker claim")
        with Session(self.engine) as s,s.begin():
            self._runtime(s,tenant_id,claim.account_id)
            work=s.get(OfWork,(claim.account_id,claim.fan_id))
            if (work is None or work.status!="claimed" or work.generation!=claim.generation
                    or work.lease_token!=claim.token or work.control_revision!=claim.control_revision
                    or work.target_message_id!=claim.target_message_id):return False
            work.status="blocked"
            work.lease_token=work.lease_until=None
            return True

    @staticmethod
    def _unresolved(s, account_id, fan_id, state):
        # Retained receipts are evidence even if an older control row never
        # acquired its pointer. Never infer resolution from a missing pointer,
        # or from the receipt falling outside the latest history page.
        return state.unresolved_attempt_id is not None or s.scalar(
            select(OfMessageDelivery.message_id).where(
                OfMessageDelivery.account_id == account_id,
                OfMessageDelivery.fan_id == fan_id,
                OfMessageDelivery.outcome.in_(UNRESOLVED_OUTCOMES),
            ).limit(1)
        ) is not None

    def complete(self, tenant_id, claim: Claim):
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, claim.account_id)
            work = s.get(OfWork, (claim.account_id, claim.fan_id))
            state = self._state(s, claim.account_id, claim.fan_id)
            fan = s.get(Fan, (claim.account_id, claim.fan_id))
            if not runtime.enabled or work is None or work.status != "claimed" or work.generation != claim.generation or work.lease_token != claim.token or work.lease_until <= utcnow():
                raise Refused("stale work claim")
            if state.revision != claim.control_revision or state.input_revision != claim.input_revision or state.mode != "automated" or state.takeover_state != "none" or self._unresolved(s, claim.account_id, claim.fan_id, state) or fan.is_muted:
                raise Refused("control changed")
            work.status = "done"
            work.lease_token = work.lease_until = None

    def need_recovery(self, tenant_id, account_id, fan_id):
        _id(fan_id)
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, account_id)
            runtime.recovery_needed = True
            row = s.get(OfRecovery, (account_id, fan_id))
            if row is None:
                s.add(OfRecovery(account_id=account_id, fan_id=fan_id))
            elif row.status == "complete":
                row.status = "needed"
                row.upper_bound = row.before_id = None
            else:
                # Preserve the lower cursor, but remember that messages may
                # have arrived above this window's frozen upper bound.
                scan = s.get(OfScan, account_id)
                if scan is None:
                    s.add(OfScan(account_id=account_id, repeat_requested=True))
                else:
                    scan.repeat_requested = True

    def claim_recovery(self, tenant_id, account_id, *, connection_revision=None):
        now = utcnow()
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, account_id)
            self._read_connection(s, runtime, connection_revision)
            # Lowest attempt generation first: an expired failing low-ID history
            # must yield to untouched histories. Lease/cursor fences stay intact.
            row = s.scalar(select(OfRecovery).where(OfRecovery.account_id == account_id,
                           or_(OfRecovery.status == "needed", (OfRecovery.status == "running") & (OfRecovery.lease_until <= now)))
                           .order_by(case((OfRecovery.fan_id.in_(fan_current_override(account_id) or {-1}), 0), else_=1),
                                     OfRecovery.generation.asc(), OfRecovery.last_complete_at.asc().nullsfirst(), OfRecovery.fan_id).limit(1))
            if row is None:
                return None
            row.status, row.generation, row.lease_token = "running", row.generation + 1, str(uuid.uuid4())
            row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            return RecoveryClaim(account_id, row.fan_id, row.generation, row.lease_token, row.high_water, row.upper_bound, row.before_id)

    def recovery_page(self, tenant_id, claim: RecoveryClaim, events: list[Event], *, has_more: bool, connection_revision=None,
                      observations=(), provider_read_claim=None):
        if type(has_more) is not bool or len(events) > PAGE_LIMIT or (has_more and not events):
            raise Refused("invalid recovery page")
        normalized = [event.normalized() for event in events]
        ids = [e.message_id for e in normalized]
        if ids != sorted(set(ids), reverse=True) or any(e.fan_id != claim.fan_id for e in normalized):
            raise Refused("invalid recovery order or scope")
        if claim.before_id is not None and any(i >= claim.before_id for i in ids):
            raise Refused("recovery cursor did not advance")
        from .offer_terms import OfferObservation
        if (type(observations) not in {list, tuple} or len(observations) > len(normalized)
                or any(type(o) is not OfferObservation for o in observations)):
            raise Refused("invalid offer observations")
        offers = [o.normalized() for o in observations]
        if any(o.source_kind != "message" or o.fan_id != claim.fan_id or o.source_id not in ids for o in offers):
            raise Refused("offer observation outside this recovery page")
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, claim.account_id)
            self._read_connection(s, runtime, connection_revision)
            if provider_read_claim is not None:
                from .provider_read_budget import current
                current(self,tenant_id,provider_read_claim,session=s)
            row = s.get(OfRecovery, (claim.account_id, claim.fan_id))
            if row is None or row.status != "running" or row.generation != claim.generation or row.lease_token != claim.token or row.lease_until <= utcnow():
                raise Refused("stale recovery claim")
            if runtime.pending_events + len(events) > MAX_PENDING:
                return "backpressure"
            if row.upper_bound is None:
                row.upper_bound = max(ids, default=row.high_water)
            # Commercial evidence only: no reply work, revision, takeover,
            # ownership or high-water change. Duplicates are idempotent.
            self._observe_offers(s, runtime, offers)
            for event in normalized:
                if event.message_id > row.high_water:
                    # Imports stay historical even if activation happens before
                    # their next drain. Reading never consents to an AI reply.
                    source = "import" if connection_revision is not None else "catchup"
                    self._record(s, runtime, event, event.payload(), source, utcnow())
            finished = not has_more or any(i <= row.high_water for i in ids)
            if finished:
                row.high_water = max(row.high_water, row.upper_bound)
                row.upper_bound = row.before_id = None
                row.status, row.last_complete_at = "complete", utcnow()
            else:
                row.before_id = min(ids)
                row.status = "needed"
            row.lease_token = row.lease_until = None
            # Do not mark the account current here: discovery of every chat is
            # a separate completeness obligation, not inferred from one page.
            self._mark_current(s, runtime)
            return "complete" if finished else "continue"

    @staticmethod
    def _observe_offers(s, runtime, offers):
        if not offers:
            return
        from sqlalchemy.dialects.postgresql import insert
        from db.onlyfans_offer_import_models import OfOfferObservation
        from .native_context import canonical
        from .offer_terms import PARSER_VERSION
        now = utcnow()
        rows = [dict(account_id=runtime.account_id, observation_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
            source_kind=o.source_kind, source_id=o.source_id, fan_id=o.fan_id, source_context=o.source_context,
            source_at=datetime.fromisoformat(o.source_at).replace(tzinfo=None), observed_at=now,
            media_json=canonical(list(o.media_ids)),
            preview_json=None if o.preview_ids is None else canonical(list(o.preview_ids)),
            locked_text=o.locked_text, price_cents=o.price_cents, currency=o.currency,
            parser_version=PARSER_VERSION, fingerprint=o.fingerprint(), media_key=o.media_key,
            reasons_json=canonical(list(o.reasons))) for o in offers]
        s.execute(insert(OfOfferObservation).values(rows).on_conflict_do_nothing(
            constraint="uq_of_offer_observation_source"))
        from .content_sync import record_current_observations
        record_current_observations(s, runtime, offers, now)

    @staticmethod
    def _begin_cycle(s, scan):
        scan.status, scan.cursor, scan.page_count = "needed", None, 0
        scan.generation += 1
        scan.repeat_requested = False
        scan.known_fan_after, scan.known_fans_complete = 0, False
        scan.lease_token = scan.lease_until = None
        s.execute(update(OfRecovery).where(OfRecovery.account_id == scan.account_id, OfRecovery.status == "complete")
                  .values(status="needed", before_id=None, upper_bound=None))

    def begin_scan(self, tenant_id, account_id):
        """Mark a reconnect/outage durably, without discarding an older gap."""
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, account_id)
            runtime.recovery_needed = True
            scan = s.get(OfScan, account_id)
            if scan is None:
                scan = OfScan(account_id=account_id)
                s.add(scan)
                s.flush()
                self._begin_cycle(s, scan)
                self._seed_known(s, scan)
            elif scan.status == "complete":
                self._begin_cycle(s, scan)
            else:
                scan.repeat_requested = True

    def request_poll(self, tenant_id, account_id, *, min_interval_seconds=5, connection_revision=None):
        """Start a later read cycle only after the previous one completed.

        This never activates an account, resets a high-water mark, renews a send
        or changes an allowance. In-flight recovery keeps its exact cursor and
        is not forced into repeated scans by periodic worker refreshes.
        """
        if type(min_interval_seconds) is not int or not 5 <= min_interval_seconds <= 300:
            raise Refused("invalid native polling interval")
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, account_id)
            self._read_connection(s, runtime, connection_revision)
            if not runtime.enabled and connection_revision is None:
                return False
            scan = s.get(OfScan, account_id)
            if scan is not None and (scan.status != "complete" or runtime.recovery_needed):
                return False
            if (scan is not None and runtime.last_sync_at is not None
                    and runtime.last_sync_at > utcnow() - timedelta(seconds=min_interval_seconds)):
                return False
            if scan is None:
                scan = OfScan(account_id=account_id)
                s.add(scan)
                s.flush()
            self._begin_cycle(s, scan)
            self._seed_known(s, scan)
            runtime.recovery_needed = True
            return True

    def claim_scan(self, tenant_id, account_id, *, connection_revision=None):
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, account_id)
            self._read_connection(s, runtime, connection_revision)
            scan = s.get(OfScan, account_id)
            if scan is not None and not scan.known_fans_complete:
                self._seed_known(s, scan)
                self._mark_current(s, runtime)
            if scan is None or scan.status == "complete" or (scan.status == "running" and scan.lease_until > utcnow()):
                return None
            scan.generation += 1
            scan.status, scan.lease_token = "running", str(uuid.uuid4())
            scan.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
            return ScanClaim(account_id, scan.generation, scan.lease_token, scan.cursor)

    @staticmethod
    def _seed_known(s, scan):
        # Bounded keyset sweep also covers archived chats absent from discovery.
        ids = s.scalars(select(Fan.fan_id).where(Fan.account_id == scan.account_id, Fan.fan_id > scan.known_fan_after)
                        .order_by(Fan.fan_id).limit(PAGE_LIMIT)).all()
        for fan_id in ids:
            if s.get(OfRecovery, (scan.account_id, fan_id)) is None:
                s.add(OfRecovery(account_id=scan.account_id, fan_id=fan_id))
        if ids:
            scan.known_fan_after = ids[-1]
        scan.known_fans_complete = len(ids) < PAGE_LIMIT

    def scan_page(self, tenant_id, claim: ScanClaim, fan_ids: list[int], *, next_cursor: str | None, connection_revision=None,
                  provider_read_claim=None):
        if len(fan_ids) > PAGE_LIMIT or len(set(fan_ids)) != len(fan_ids):
            raise Refused("invalid discovery page")
        for fan in fan_ids: _id(fan)
        if next_cursor is not None and (not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 1024 or next_cursor == claim.cursor or not fan_ids):
            raise Refused("invalid discovery cursor")
        with Session(self.engine) as s, s.begin():
            runtime = self._runtime(s, tenant_id, claim.account_id)
            self._read_connection(s, runtime, connection_revision)
            if provider_read_claim is not None:
                from .provider_read_budget import current
                current(self,tenant_id,provider_read_claim,session=s)
            scan = s.get(OfScan, claim.account_id)
            if scan is None or scan.status != "running" or scan.generation != claim.generation or scan.lease_token != claim.token or scan.lease_until <= utcnow():
                raise Refused("stale discovery claim")
            if scan.page_count >= 10000:
                raise Refused("discovery page budget exhausted")
            for fan_id in fan_ids:
                if s.get(OfRecovery, (claim.account_id, fan_id)) is None:
                    s.add(OfRecovery(account_id=claim.account_id, fan_id=fan_id))
            scan.page_count += 1
            scan.cursor = next_cursor
            scan.status = "needed" if next_cursor is not None else "complete"
            scan.lease_token = scan.lease_until = None
            self._mark_current(s, runtime)

    def _mark_current(self, s, runtime):
        s.flush()
        if not runtime.recovery_needed:
            return  # An idle poll is not a new successful synchronization.
        scan = s.get(OfScan, runtime.account_id)
        if scan is None or scan.status != "complete" or not scan.known_fans_complete or runtime.pending_events:
            return
        outstanding = s.scalar(select(OfRecovery.fan_id).where(OfRecovery.account_id == runtime.account_id, OfRecovery.status != "complete").limit(1))
        conflict = s.scalar(select(OfIngress.event_key).where(OfIngress.account_id == runtime.account_id, OfIngress.status == "quarantined").limit(1))
        if outstanding is not None or conflict is not None:
            return
        if scan.repeat_requested:
            self._begin_cycle(s, scan)
            return
        runtime.recovery_needed = False
        runtime.last_sync_at = utcnow()
