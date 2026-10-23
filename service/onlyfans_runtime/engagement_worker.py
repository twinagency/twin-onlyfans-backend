"""Worker glue for proactive engagement: schedule from canonical history, queue one due proposal.

Only the native worker calls this, and only when ONLYFANS_ENGAGEMENT_ENABLED=1. Per-account settings stay
default-off and every send still passes TextAuthority (prepare, admit and the final consume re-validate).
No second model call, no legacy sender, no provider mass-send.
"""
from datetime import timedelta
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from db.models import Chat, Message, OfConversationState
from .launch_engagement.contracts import Policy, digest, load_object
from .launch_engagement.models import Action, Settings
from .launch_engagement.service import ACTIVE, EngagementService
from .store import Refused

ACTOR = "native-engagement-worker"
SCAN_SECONDS = 60
BATCH = 20
KINDS = (("offer", "offers", "offer_delay_hours"), ("follow_up", "follow_ups", "follow_up_delay_hours"),
         ("reengage", "reengagement", "reengage_delay_hours"))
_scanned = {}


def command_key(binding_id, fan_id, kind, anchor):
    return digest({"purpose": "onlyfans-engagement-schedule.v1", "binding": binding_id,
                   "fan": fan_id, "kind": kind, "anchor": anchor})


def _candidates(store, authority, tenant, account, policy):
    """Fans whose newest message is ours, with inbound history, inside a kind's window."""
    kinds = [(k, getattr(policy, delay)) for k, switch, delay in KINDS if getattr(policy, switch)]
    if not kinds:
        return None, []
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        now = authority._db_now(s)
        widest = max(hours for _, hours in kinds) + policy.expiry_hours
        floor, ceiling = now - timedelta(hours=widest), now - timedelta(hours=min(hours for _, hours in kinds) - 1)
        bucket = int(time.time() // 300)  # rotate so a refused cohort cannot starve the rest
        active = select(Action.fan_id).where(Action.account_id == account, Action.status.in_(ACTIVE))
        earlier = aliased(Message)
        inbound = select(earlier.fan_id).where(earlier.account_id == account, earlier.fan_id == Chat.fan_id,
                                               earlier.direction == "in").exists()
        rows = s.execute(select(Chat.fan_id, Message.message_id, Message.created_at)
            .join(Message, (Message.account_id == Chat.account_id) & (Message.fan_id == Chat.fan_id)
                  & (Message.message_id == Chat.last_message_id))
            .where(Chat.account_id == account, Chat.last_message_at > floor, Chat.last_message_at <= ceiling,
                   Message.direction == "out", Message.is_unsent.is_(False), inbound, Chat.fan_id.not_in(active))
            .order_by(func.md5(func.concat(Chat.fan_id, bucket)), Chat.fan_id).limit(BATCH)).all()
        return (runtime.binding_id, kinds, now), rows


def schedule_due(store, authority, service, tenant, account):
    """Create at most BATCH scheduled actions; every eligibility rule lives in EngagementService.schedule."""
    key = (id(store), account)
    if time.monotonic() - _scanned.get(key, -SCAN_SECONDS) < SCAN_SECONDS:
        return 0
    _scanned[key] = time.monotonic()
    with Session(store.engine) as s, s.begin():
        row = s.get(Settings, account)
        raw = row.settings_json if row is not None else None
    if raw is None:
        return 0
    policy = Policy.parse(load_object(raw))
    if not policy.enabled:
        return 0
    context, rows = _candidates(store, authority, tenant, account, policy)
    created = 0
    for fan_id, anchor, stamp in rows:
        age = context[2] - stamp
        for kind, hours in context[1]:
            if age < timedelta(hours=hours - 1):
                continue
            try:
                service.schedule(tenant, account, fan_id, kind=kind, actor=ACTOR,
                                 command_key=command_key(context[0], fan_id, kind, anchor))
                created += 1
                break
            except Refused:
                continue
    return created


def queue_due(store, authority, service, tenant, account):
    """One due proposal per account/cycle through the shared authority; returns the intent id or None."""
    proposal = service.due(tenant, account)
    if proposal is None:
        return None
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant, account)
        state = s.get(OfConversationState, (account, proposal["fan_id"]))
        revision = state.revision if state is not None else None
    if revision is None:
        return None
    terms = proposal["terms"]
    return authority.prepare(tenant, account, proposal["fan_id"], command_key=proposal["command_key"],
        expected_revision=revision, body=proposal["body"], origin="engagement", actor=proposal["actor"],
        catalog_item_id=terms["item"]["id"] if terms else None, engagement_action_id=proposal["action_id"])
