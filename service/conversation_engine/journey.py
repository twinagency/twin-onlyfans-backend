"""Journey rules for the real (fansapi) path, as engine behaviour rather than runner policy.

Everything here is deterministic and reads/writes the records the engine already owns:

* sales state      -> ``of_engine_owner.sales_mode`` (R1/R8: paused / reopened_scoped / open)
* ownership        -> ``Store.control`` (request_takeover) which flips ``of_engine_owner.mode`` to ``human``
* requests         -> ``of_engine_request`` (+ ``EngineService.request_handover``)
* operator records -> ``of_operator_case`` (flags and human holds)
* money            -> ``of_fans_evidence`` (applied, paired transactions and chargebacks) and ``of_engine_access_evidence``
* welcome          -> one ``of_engine_offer_grant`` per fan, marked by its ``vault:welcome:`` coverage reference

No new table, no model call, no send. The module is inert unless the account's delivery policy is ``fansapi``.
Synthetic accounts never see any of it.

Two entry points sit in the engine (marked ``JOURNEY-HOOK`` in engine.py): ``journey_context`` adds the facts to the
model snapshot and ``enforce_journey`` rejects a proposal that breaks a rule. A proposal is never trusted to
follow a rule that only exists in prose.

Known limit (schema): an engine request is anchored to a canonical INBOUND message (memory binding and the
source-message foreign keys). The welcome and the post-purchase reaction therefore ride the fan's next inbound
message; nothing is sent proactively. See NEEDS-JOIN.md.
"""
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineOfferGrant, OfEngineOwner, OfEngineRequest
from db.onlyfans_fans_intake_models import OfFansBackfill, OfFansEvidence, OfFansProjection
from onlyfans_runtime.store import Refused, utcnow
from .history_scope import current_binding_message

WELCOME_PREFIX = 'vault:welcome:'
MAX_REACTION_CHARS = 160
HISTORY_LIMIT = 200


@dataclass(frozen=True)
class JourneyConfig:
    """Operator-approved journey settings. Every default is the conservative one."""
    welcome_item_id: int | None = None        # None: no welcome offer (the item must be an approved catalogue item)
    welcome_ttl_hours: int = 168
    whale_hold_cents: int = 100_000           # verified spend at which a human takes over (no model reply)
    whale_flag_cents: int = 50_000            # softer flag, engine keeps talking
    identity_line: str | None = None          # approved answer to "are you real / who is this"; None -> human
    limit_terms: tuple[str, ...] = ()         # creator limits (lower-case words/phrases), pushed past = limit push
    handover_target: str = 'human-queue'
    auto_backfill: bool = False               # run the fan's backfill from the worker (reads only); default: wait
    backfill_start: str = '2015-01-01 00:00:00'
    backfill_pages: int = 20


_CONFIG = JourneyConfig()


def configure_journey(config):
    global _CONFIG
    if type(config) is not JourneyConfig:
        raise Refused('journey configuration required')
    if (config.whale_flag_cents < 0 or config.whale_hold_cents < config.whale_flag_cents
            or config.welcome_ttl_hours < 1 or not config.handover_target.strip()):
        raise Refused('journey configuration is inconsistent')
    _CONFIG = config
    return config


def journey_config():
    return _CONFIG


# ------------------------------------------------------------------ text signals
# Narrow, reviewed word lists. A miss means the model answers normally; a hit never sends anything.
_IDENTITY = re.compile(
    r"\b(?:are you (?:an? )?(?:real|human|bot|ai|robot|actually (?:her|real|you)|a person|a girl|automated)"
    r"|is this (?:an? )?(?:bot|ai|real|robot|automated|really (?:her|you))"
    r"|am i (?:talking|chatting|speaking|texting) (?:to|with) (?:an? )?(?:real|bot|ai|human|person|someone)"
    r"|who am i (?:talking|chatting|speaking|texting) (?:to|with)"
    r"|(?:talking|chatting|speaking|texting) (?:to|with) (?:an? )?(?:agency|chatter|team|staff|employee|someone else)"
    r"|who runs this account|do you work for an? agency|agency|chatter(?:s)?)\b")
_OFFPLATFORM = re.compile(
    r"\b(?:whatsapp|telegram|snap(?:chat)?|insta(?:gram)?|kik|discord|signal app|wechat|skype|facetime|"
    r"your (?:phone )?number|phone number|text me|call me|email|e-mail|gmail|add me|dm me on|"
    r"off (?:of )?(?:onlyfans|of|here|the app|platform)|outside (?:of )?(?:onlyfans|of|here|the app|platform)|"
    r"venmo|cash ?app|paypal|zelle)\b")
_MEETUP = re.compile(
    r"\b(?:meet up|meet you|meet in person|meet irl|in person|irl|hook ?up|come over|see you in person|"
    r"go on a date|take you (?:out|to dinner)|grab (?:dinner|drinks|coffee)|where do you live|visit you)\b")
_CUSTOM = re.compile(
    r"\b(?:custom|personali[sz]ed|(?:make|film|record|shoot) (?:me )?(?:a |an )?(?:video|vid|pic|photo|clip|set)"
    r"|(?:video|vid|pic|photo|clip) (?:just )?for me|say my name|special request|do (?:a|an) .{1,40} for me)\b")
_REFUSAL = re.compile(
    r"\b(?:no thanks|no thank you|not interested|not now|maybe later|too (?:expensive|much|pricey|steep)"
    r"|can(?:'|)t afford|cannot afford|don(?:'|)t have (?:the )?(?:money|cash)|i(?:'m| am) (?:broke|poor)"
    r"|out of (?:money|budget)|no money|stop (?:trying to )?(?:sell|pitch)\w*|not buying|"
    r"(?:don(?:'|)t|do not) want (?:it|that|to buy)|save my money|tight (?:on )?(?:money|budget))\b")
_PROMISE = re.compile(
    r"\b(?:i(?:'ll| will| can)|ill|i(?:'m| am) (?:going to|gonna)|im gonna|definitely will|sure[, ]+i(?:'ll| will))"
    r" (?:definitely |totally |absolutely |happily |gladly )?(?:make|film|record|shoot|create|personali[sz]e)\b"
    r"|\bi (?:promise|guarantee)\b|\bpromise you\b")


def _norm(text):
    return re.sub(r'\s+', ' ', str(text or '').lower().replace('’', "'")).strip()


def signals(text, config=None):
    """The set of journey signals in one fan message."""
    value = _norm(text)
    config = config or _CONFIG
    found = set()
    if _IDENTITY.search(value):
        found.add('identity')
    if _OFFPLATFORM.search(value):
        found.add('offplatform')
    if _MEETUP.search(value):
        found.add('meetup')
    if any(term and term in value for term in config.limit_terms):
        found.add('limit')
    if _CUSTOM.search(value):
        found.add('custom')
    if _REFUSAL.search(value):
        found.add('refusal')
    return frozenset(found)


def promises_custom(*bodies):
    return any(_PROMISE.search(_norm(body)) for body in bodies)


# ------------------------------------------------------------------ facts from existing records
def active(s, account):
    policy = s.get(OfDeliveryPolicy, account)
    return policy is not None and policy.transport_kind == 'fansapi'


def backfill_complete(s, account, fan_id):
    """True once a COMPLETE backfill run walked this fan's chat (history) after replaying transactions.

    A run replays the account's transactions first and then each requested chat, and records the chats it
    finished (`done_chats`). A fan that no finished run covers has history and payments we may not know about,
    so a paid offer to him waits. Plain replies are not gated.
    """
    for run in s.scalars(select(OfFansBackfill).where(OfFansBackfill.account_id == account,
                                                      OfFansBackfill.status == 'complete')):
        try:
            state = json.loads(run.marker or '{}')
        except (TypeError, ValueError):
            continue
        if str(fan_id) in {str(v) for v in state.get('done_chats', [])}:
            return True
    return False


def verified_spend(s, account, fan_id):
    """Cents the fan really paid: exactly paired transactions the commerce owner applied, minus applied chargebacks."""
    def total(kind):
        return s.scalar(select(func.coalesce(func.sum(OfFansEvidence.amount_cents), 0)).where(
            OfFansEvidence.account_id == account, OfFansEvidence.fan_id == fan_id, OfFansEvidence.kind == kind,
            OfFansEvidence.status == 'applied')) or 0
    return int(total('transaction') - total('chargeback'))


def _inbound(s, runtime, fan_id, *, upto=None):
    query = select(Message).where(Message.account_id == runtime.account_id, Message.fan_id == fan_id,
        Message.direction == 'in', Message.is_unsent.is_(False), current_binding_message(runtime.binding_id))
    rows = list(s.scalars(query.order_by(Message.created_at.desc(), Message.message_id.desc()).limit(HISTORY_LIMIT)))
    rows.reverse()
    if upto is not None:
        keep = []
        for row in rows:
            keep.append(row)
            if row.message_id == upto:
                break
        rows = keep
    return rows


def decide(s, runtime, owner, message, config=None):
    """Pure verdict for one inbound message. Returns a dict; writes nothing.

    human: reason that needs a person (engine stays silent), pause: selling must be paused by this message,
    flags: operator flags to raise, no_promise: the reply must not promise anything.
    """
    config = config or _CONFIG
    found = signals(message.body, config)
    verdict = {'signals': sorted(found), 'human': None, 'pause': None, 'flags': [], 'no_promise': False}
    spend = verified_spend(s, runtime.account_id, owner.fan_id)
    if spend >= config.whale_hold_cents:
        verdict['human'] = 'whale_hold'
    elif spend >= config.whale_flag_cents:
        verdict['flags'].append('vip_flag')
    if 'identity' in found and config.identity_line is None and verdict['human'] is None:
        verdict['human'] = 'identity'
    history = _inbound(s, runtime, owner.fan_id, upto=message.message_id)
    ask_count = sum(1 for row in history if signals(row.body, config) & {'offplatform', 'meetup'})
    push_count = sum(1 for row in history if signals(row.body, config) & {'offplatform', 'meetup', 'limit'})
    if found & {'offplatform', 'meetup', 'limit'} and push_count >= 3 and verdict['human'] is None:
        verdict['human'] = 'limit_push'
    if found & {'offplatform', 'meetup'} and ask_count >= 2:
        verdict['pause'] = 'offplatform_ask'
        verdict['flags'].append('offplatform_ask')
    if 'refusal' in found and verdict['pause'] is None:
        verdict['pause'] = 'refusal'
    if 'custom' in found:
        verdict['no_promise'] = True
        verdict['flags'].append('custom_request')
    return verdict


def _welcome_grant(s, account, fan_id):
    return s.scalar(select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == account,
        OfEngineOfferGrant.fan_id == fan_id, OfEngineOfferGrant.coverage_ref.like(WELCOME_PREFIX + '%'))
        .order_by(OfEngineOfferGrant.created_at).limit(1))


def _first_request(s, account, fan_id, request):
    earlier = s.scalar(select(func.count()).select_from(OfEngineRequest).where(
        OfEngineRequest.account_id == account, OfEngineRequest.fan_id == fan_id,
        OfEngineRequest.created_at < request.created_at, OfEngineRequest.request_id != request.request_id))
    return not earlier


def _post_purchase(s, runtime, fan_id, message):
    """True when this inbound message is the first one after the fan's latest verified purchase."""
    latest = s.scalar(select(OfEngineAccessEvidence).where(OfEngineAccessEvidence.account_id == runtime.account_id,
        OfEngineAccessEvidence.fan_id == fan_id, OfEngineAccessEvidence.applied.is_(True))
        .order_by(OfEngineAccessEvidence.occurred_at.desc(), OfEngineAccessEvidence.created_at.desc()).limit(1))
    if latest is None or latest.status != 'confirmed' or message.created_at <= latest.occurred_at:
        return False
    first = s.scalar(select(Message.message_id).where(Message.account_id == runtime.account_id,
        Message.fan_id == fan_id, Message.direction == 'in', Message.is_unsent.is_(False),
        current_binding_message(runtime.binding_id), Message.created_at > latest.occurred_at)
        .order_by(Message.created_at, Message.message_id).limit(1))
    return first == message.message_id


def journey_context(s, runtime, owner, request, message):
    """Facts for the model snapshot (engine hook 1). Empty for every account that is not 'fansapi'."""
    if not active(s, runtime.account_id) or owner is None:
        return {}
    config = _CONFIG
    verdict = decide(s, runtime, owner, message, config)
    ctx = {'signals': verdict['signals']}
    if verdict['human']:
        ctx['human_required'] = verdict['human']
    post = _post_purchase(s, runtime, owner.fan_id, message)
    gate = backfill_complete(s, runtime.account_id, owner.fan_id)
    # Hard blocks only. The owner's sales state (paused / scoped) is the existing R1/R8 mechanism and is judged
    # separately so an explicit, scoped reopening by the fan can still be honoured in the same turn.
    ctx['offers_allowed'] = bool(verdict['pause'] is None and not post and gate and not verdict['human'])
    ctx['sales_mode'] = owner.sales_mode
    ctx['backfill_complete'] = gate
    if post:
        ctx['post_purchase'] = {'max_chars': MAX_REACTION_CHARS,
                                'rule': 'one short reaction, no offer, no price, no pitch; the next offer waits for his next message'}
    if verdict['no_promise']:
        ctx['no_promise'] = True
    if config.identity_line is not None and 'identity' in verdict['signals']:
        ctx['approved_identity_line'] = config.identity_line
    grant = _welcome_grant(s, runtime.account_id, owner.fan_id)
    if (grant is not None and grant.used_intent_id is None and grant.expires_at > utcnow()
            and ctx['offers_allowed'] and request is not None and _first_request(s, runtime.account_id, owner.fan_id, request)):
        ctx['welcome'] = {'grant_id': grant.grant_id, 'item_id': grant.item_id,
                          'rule': 'at most this one approved catalogue offer, once ever per fan'}
    return ctx


def enforce_journey(ctx, proposal):
    """Reject a proposal that breaks a journey rule (engine hook 2). `ctx` is the snapshot's journey facts."""
    if not ctx:
        return
    if ctx.get('human_required'):
        raise Refused('journey: this conversation needs a human (' + ctx['human_required'] + ')')
    if proposal.action == 'offer' and not ctx.get('offers_allowed', True):
        raise Refused('journey: no offer is allowed in this turn')
    if proposal.action == 'offer' and ctx.get('sales_mode') == 'paused' and proposal.sales_signal != 'reopen':
        raise Refused('journey: selling is paused and only his explicit, scoped interest reopens it')
    welcome = ctx.get('welcome')
    if welcome and proposal.action == 'offer' and proposal.offer_grant_id != welcome['grant_id']:
        raise Refused('journey: the welcome turn offers only its approved item')
    post = ctx.get('post_purchase')
    if post and (proposal.action != 'reply' or proposal.parts or proposal.pitch or proposal.price_claims
                 or proposal.sales_signal == 'reopen' or len(proposal.body) > post['max_chars']):
        raise Refused('journey: after a purchase send one short reaction and no offer')
    if ctx.get('no_promise') and promises_custom(proposal.body, *proposal.parts):
        raise Refused('journey: a custom request is flagged and must not be promised')


# ------------------------------------------------------------------ service (writes)
def _key(*parts):
    return sha256(':'.join(str(p) for p in parts).encode()).hexdigest()[:40]


class JourneyService:
    """Applies the verdicts that need a write. The worker calls it before planning and after payment evidence."""
    def __init__(self, store, engine=None, config=None):
        self.store = store
        self.db = store.engine
        self.engine_service = engine
        self._config = config

    @property
    def config(self):
        return self._config or _CONFIG   # read at use time: configure_journey() applies to a running worker

    # -- public
    def before_plan(self, tenant, account, fan_id, message_id):
        """Run the deterministic rules for one inbound message. {'action': 'plan'|'handled', ...}."""
        with Session(self.db) as s:
            runtime = self.store._runtime(s, tenant, account)
            owner = s.get(OfEngineOwner, (account, fan_id))
            message = s.get(Message, (account, fan_id, message_id))
            if (not active(s, account) or owner is None or owner.mode != 'active' or message is None
                    or message.direction != 'in'):
                return {'action': 'plan', 'reason': 'journey_inactive'}
            verdict = decide(s, runtime, owner, message, self.config)
        done = []
        if verdict['pause']:
            done.append(self._pause_selling(tenant, account, fan_id, message_id, verdict['pause']))
        for flag in verdict['flags']:
            self._flag(tenant, account, fan_id, message_id, flag)
        if verdict['human']:
            self._to_human(tenant, account, fan_id, message_id, verdict['human'])
            return {'action': 'handled', 'reason': verdict['human'], 'signals': verdict['signals']}
        self._ensure_welcome(tenant, account, fan_id, message_id)
        return {'action': 'plan', 'reason': None, 'signals': verdict['signals'], 'paused': verdict['pause']}

    def after_evidence(self, tenant, account, fan_id):
        """Re-evaluate the spend thresholds after payment evidence changed. Idempotent."""
        with Session(self.db) as s:
            if not active(s, account):
                return {'action': 'none'}
            spend = verified_spend(s, account, fan_id)
            owner = s.get(OfEngineOwner, (account, fan_id))
        if spend >= self.config.whale_hold_cents:
            self._to_human(tenant, account, fan_id, None, 'whale_hold')
            return {'action': 'hold', 'spend_cents': spend}
        if spend >= self.config.whale_flag_cents and owner is not None:
            self._flag(tenant, account, fan_id, None, 'vip_flag')
            return {'action': 'flag', 'spend_cents': spend}
        return {'action': 'none', 'spend_cents': spend}

    # -- writes
    def _pause_selling(self, tenant, account, fan_id, message_id, why):
        with Session(self.db) as s, s.begin():
            self.store._runtime(s, tenant, account)
            owner = s.get(OfEngineOwner, (account, fan_id), with_for_update=True)
            if owner is None or owner.sales_mode == 'paused':
                return 'already_paused'
            owner.sales_mode, owner.sales_scope_json, owner.sales_source_message_id = 'paused', '[]', message_id
            owner.revision += 1
            owner.updated_at = utcnow()
            return 'paused'

    def _flag(self, tenant, account, fan_id, message_id, category):
        from onlyfans_runtime.operator_cases import open_case
        with Session(self.db) as s, s.begin():
            self.store._runtime(s, tenant, account)
            open_case(s, account, fan_id, category, reason='journey_' + category,
                      evidence={'rule': category, 'message_id': message_id}, source_message_id=message_id,
                      origin='detector', scope='offer')

    def _ensure_request(self, s, runtime, owner, fan_id, message_id, next_step):
        import uuid
        request = s.scalar(select(OfEngineRequest).where(OfEngineRequest.account_id == runtime.account_id,
            OfEngineRequest.fan_id == fan_id, OfEngineRequest.source_message_id == message_id))
        if request is None:
            request = OfEngineRequest(account_id=runtime.account_id, request_id=str(uuid.uuid4()), fan_id=fan_id,
                binding_id=runtime.binding_id, source_message_id=message_id, status='open', owner='engine',
                next_step=next_step, revision=0, created_at=utcnow(), updated_at=utcnow())
            s.add(request)
            s.flush()
        return request.request_id

    def _to_human(self, tenant, account, fan_id, message_id, reason):
        """Engine ownership is held for a human: handover request, durable hold case, then the audited takeover."""
        from onlyfans_runtime.operator_cases import open_case
        request_id = None
        with Session(self.db) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            owner = s.get(OfEngineOwner, (account, fan_id))
            if owner is None or owner.binding_id != runtime.binding_id:
                raise Refused('engine owner is unavailable')
            if message_id is None:
                latest = _inbound(s, runtime, fan_id)
                message_id = latest[-1].message_id if latest else None
            if message_id is not None:
                request_id = self._ensure_request(s, runtime, owner, fan_id, message_id, 'journey:human_' + reason)
            open_case(s, account, fan_id, 'journey_' + reason, reason='journey_' + reason,
                      evidence={'rule': reason, 'message_id': message_id}, source_message_id=message_id,
                      origin='detector', scope='all')
        if request_id is not None and self.engine_service is not None:
            try:
                self.engine_service.request_handover(tenant, account, request_id, target=self.config.handover_target)
            except Refused:
                pass  # already terminal or already handed over
        elif request_id is not None:
            with Session(self.db) as s, s.begin():
                request = s.get(OfEngineRequest, (account, request_id), with_for_update=True)
                if request.status in {'open', 'waiting_fan', 'waiting_provider'}:
                    request.status, request.handover_target = 'handover_pending', self.config.handover_target
                    request.next_step, request.updated_at = 'await_handover_acceptance', utcnow()
                    request.revision += 1
        self._takeover(tenant, account, fan_id, reason, message_id)

    def _takeover(self, tenant, account, fan_id, reason, message_id):
        for _ in range(3):
            with Session(self.db) as s:
                state = s.get(OfConversationState, (account, fan_id))
                if state is None or state.mode == 'human_controlled' or state.takeover_state != 'none':
                    return 'already_human'
                if state.mode != 'automated':
                    return 'not_automated'
                revision = state.revision
            try:
                self.store.control(tenant, account, fan_id, mode='human_controlled', expected_revision=revision,
                    command_key='journey:' + _key(account, fan_id, reason, message_id, revision), actor='engine-journey',
                    reason=('Journey rule needs a person: ' + reason)[:512], action='request_takeover')
                return 'takeover_requested'
            except Refused as exc:
                if 'revision' not in str(exc):
                    raise
        return 'takeover_deferred'

    def _ensure_welcome(self, tenant, account, fan_id, message_id):
        """Create the one welcome grant per fan, once ever, when a subscriber's first message arrives."""
        config = self.config
        if config.welcome_item_id is None:
            return None
        with Session(self.db) as s:
            if (_welcome_grant(s, account, fan_id) is not None):
                return None
            sub = s.scalar(select(OfFansProjection).where(OfFansProjection.account_id == account,
                OfFansProjection.status == 'projected', OfFansProjection.detail == f'subscription_new:{fan_id}')
                .order_by(OfFansProjection.updated_at).limit(1))
            if sub is None:
                return None
            requests = s.scalars(select(OfEngineRequest.source_message_id).where(OfEngineRequest.account_id == account,
                OfEngineRequest.fan_id == fan_id)).all()
            if any(m != message_id for m in requests):
                return None  # not his first conversation turn: the welcome moment has passed
            owner = s.get(OfEngineOwner, (account, fan_id))
            if owner is None or owner.sales_mode != 'open':
                return None
            ref = WELCOME_PREFIX + _key(account, fan_id, sub.epoch, sub.event_key)[:24]
        from .commerce import RealCatalogueCoverage, approve_catalogue_offer
        try:
            return approve_catalogue_offer(self.store, tenant, account, fan_id, config.welcome_item_id,
                RealCatalogueCoverage(account, fan_id, config.welcome_item_id, ref,
                                      utcnow() + timedelta(hours=config.welcome_ttl_hours)))
        except Refused:
            return None
