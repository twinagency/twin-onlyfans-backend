"""Journey rules for the real (fansapi) path (Plan C pilot subset), as engine behaviour rather than runner policy.

Everything here is deterministic and reads/writes the records the engine already owns:

* sales state      -> ``of_engine_owner.sales_mode`` (paused / reopened_scoped / open)
* ownership        -> ``Store.control`` (request_takeover) which flips ``of_engine_owner.mode`` to ``human``
* requests         -> ``of_engine_request`` (+ ``EngineService.request_handover``)
* operator records -> ``of_operator_case`` (flags and human holds)
* money            -> ``of_fans_evidence`` (applied provider unlocks tied to our own sends; unlinked transactions and reversals are NOT counted) and ``of_engine_access_evidence``

Plan C scope. KEPT: refusal / affordability pause (stops the pitch and invalidates unsent sales drafts; an explicit
"stop offering" is a distinct opt-out that only the fan's clear request reopens; a temporary "not now" may, when
`temporary_pause_reopen_hours` is configured (off by default), become eligible for a fresh operator-approved offer on a
LATER inbound message, never by timed outreach), identity -> human unless an approved identity wording exists, custom-request no-promise, limit /
off-platform / meetup human routing, "no new card just for a compliment after a purchase", and the history-coverage
check (`backfill_complete` and unscoped messages) that holds a paid offer until the fan's history is known.
DEFERRED (removed, handoff section 2): the subscriber welcome offer, whale hold/flag thresholds, ladder-type defaults.

No new table, no model call, no send. The module is inert unless the account's delivery policy is ``fansapi``.
Synthetic accounts never see any of it.

Two entry points sit in the engine (marked ``JOURNEY-HOOK`` in engine.py): ``journey_context`` adds the facts to the
model snapshot and ``enforce_journey`` rejects a proposal that breaks a rule. ``journey_problems`` is the same check as
a list, so the model judge can send a violation back for ONE corrected draft instead of losing the turn.
"""
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineDecision, OfEngineOwner, OfEngineRequest
from db.onlyfans_fans_intake_models import OfFansBackfill, OfFansEvidence
from onlyfans_runtime.store import Refused, utcnow
from .history_scope import current_binding_message, has_unscoped_messages
from .writer_context import active_profile

HISTORY_LIMIT = 200


@dataclass(frozen=True)
class JourneyConfig:
    """Operator-approved journey settings. Every default is the conservative one."""
    identity_line: str | None = None          # approved answer to "are you real / who is this"; None -> profile wording, else human
    limit_terms: tuple[str, ...] = ()         # creator limits (lower-case words/phrases), pushed past = limit push
    handover_target: str = 'human-queue'
    auto_backfill: bool = False               # run the fan's backfill from the worker (reads only); default: wait
    backfill_start: str = '2015-01-01 00:00:00'
    backfill_pages: int = 20
    # A temporary "not now" / affordability pause may become eligible for a fresh, operator-approved offer on a later
    # inbound message once this many hours passed since it (the handoff's 12-hour proposal). None (default) disables:
    # a pause then ends only when the fan clearly asks to buy. Never applies to an explicit opt-out.
    temporary_pause_reopen_hours: int | None = None   # OFF until Jesse records the 12-hour decision


_CONFIG = JourneyConfig()


def configure_journey(config):
    global _CONFIG
    if type(config) is not JourneyConfig:
        raise Refused('journey configuration required')
    if ((config.temporary_pause_reopen_hours is not None and (type(config.temporary_pause_reopen_hours) is not int
                                                              or config.temporary_pause_reopen_hours < 1))
            or not config.handover_target.strip()):
        raise Refused('journey configuration is inconsistent')
    _CONFIG = config
    return config


def journey_config():
    return _CONFIG


def identity_line_for(account, config=None):
    """The APPROVED identity wording for this account: explicit journey config first, then the creator profile."""
    config = config or _CONFIG
    if config.identity_line and config.identity_line.strip():
        return config.identity_line.strip()
    profile = active_profile(account)
    line = (getattr(profile, 'role_statement', '') or '').strip()
    return line or None


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
_SALES_OPTOUT = re.compile(
    r"\b(?:stop (?:trying to )?(?:sell|pitch|offer)\w*|stop (?:the )?(?:offers|pitch\w*|selling)|quit (?:selling|pitching)|"
    r"(?:don'?t|do not|never) (?:offer|pitch|sell|send)\w* (?:me )?(?:anything|any|offers?|content|ppv|stuff|more)|"
    r"no more (?:offers|ppv|pitches|selling)|(?:just|only) (?:chat|talk)\w*,? (?:no|not|don'?t)\b.{0,24}\b(?:sell|buy|offer|pitch))\b")
_COMPLIMENT = re.compile(
    r"\b(?:thank(?:s| you)|thx|love(?:d)? (?:it|that|them|this)|loved|so (?:hot|good|sexy|pretty|beautiful)|amazing|perfect|"
    r"gorgeous|beautiful|stunning|incredible|worth it|wow|damn|that was (?:great|amazing|hot|so good)|"
    r"you(?:'re| are) (?:so )?(?:hot|sexy|beautiful|amazing|incredible|gorgeous|perfect))\b")
_ASKS = re.compile(
    r"\?|\b(?:more|another|next|else|other|send|show|got any|do you have|have any|what about|buy|unlock|price|how much|"
    r"can i|could i|do you|is there|want (?:to|more|another))\b")
_PROMISE = re.compile(
    r"\b(?:i(?:'ll| will| can)|ill|i(?:'m| am) (?:going to|gonna)|im gonna|definitely will|sure[, ]+i(?:'ll| will))"
    r" (?:definitely |totally |absolutely |happily |gladly )?(?:make|film|record|shoot|create|personali[sz]e)\b"
    r"|\bi (?:promise|guarantee)\b|\bpromise you\b")


_BANNED_FAN = re.compile(
    r"\b(under ?age|under 18|minors?|jailbait|loli|lolita|shota|pedo\w*|school ?girl|teen(?:age|ager|s)?|(?:little|young) (?:girl|boy)|"
    r"step ?(?:sis|sister|bro|brother|daughter|son|mom|mum|mother|dad|father)|incest|rape|raping|roofie|drugged|"
    r"(?:passed out|unconscious) (?:and|while|girl|you)|bestiality|zoophil\w*|(?:dog|horse|animal)s? (?:fuck\w*|sex|cock|dick)|"
    r"scat|golden shower|piss on|(?:blood|bleeding) (?:play|kink)|necro\w*|(?:pay|paying|paid)\b[^.?!]{0,24}?\b(?:to|for) (?:meet|a meet|sex|fuck)|"
    r"how much (?:to|for) (?:meet|fuck|sex))\b", re.I)


def _norm(text):
    return re.sub(r'\s+', ' ', str(text or '').lower().replace('’', "'")).strip()


def signals(text, config=None):
    """The set of journey signals in one fan message."""
    value = _norm(text)
    config = config or _CONFIG
    found = set()
    from .safety_context import sensitive_match
    if sensitive_match(_BANNED_FAN, value):
        found.add('platform_banned')
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
    if _SALES_OPTOUT.search(value):
        found.add('sales_optout')
        found.add('refusal')
    if _COMPLIMENT.search(value) and not _ASKS.search(value) and not found:
        found.add('compliment')
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
    """Cleared, ledger-backed spending; None when no verified rows exist.

    A provider-reported unlock is an access observation, not a settled
    transaction. The synthetic lab keeps its explicitly synthetic fixture.
    """
    if active(s, account):
        from db.models import Transaction
        from db.onlyfans_ledger_models import OfLedgerEvidence
        proof = select(OfLedgerEvidence.evidence_hash).where(
            OfLedgerEvidence.account_id == account, OfLedgerEvidence.fan_id == fan_id,
            OfLedgerEvidence.provider_transaction_id == Transaction.provider_transaction_id,
            OfLedgerEvidence.transaction_id == Transaction.id,
            OfLedgerEvidence.outcome == 'applied').exists()
        amount, count = s.execute(select(func.coalesce(func.sum(Transaction.amount_cents), 0),
            func.count()).where(Transaction.account_id == account, Transaction.fan_id == fan_id,
            Transaction.status == 'cleared', Transaction.currency == 'USD',
            Transaction.source == 'ledger', Transaction.amount_cents >= 0, proof)).one()
        return int(amount) if count else None
    amount = s.scalar(select(func.coalesce(func.sum(OfFansEvidence.amount_cents), 0)).where(
        OfFansEvidence.account_id == account, OfFansEvidence.fan_id == fan_id,
        OfFansEvidence.kind == 'unlock', OfFansEvidence.status == 'applied'))
    return int(amount or 0)


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

    human: reason that needs a person (engine stays silent), pause: selling must be paused by this message
    ('refusal' temporary or 'sales_optout' explicit), flags: operator flags to raise,
    no_promise: the reply must not promise anything.
    """
    config = config or _CONFIG
    found = signals(message.body, config)
    verdict = {'signals': sorted(found), 'human': None, 'pause': None, 'flags': [], 'no_promise': False}
    if 'platform_banned' in found:
        verdict['human'] = 'platform_banned'   # a banned theme pushed by the fan: a person decides, the model never answers it
    if 'identity' in found and identity_line_for(runtime.account_id, config) is None:
        verdict['human'] = 'identity'
    history = _inbound(s, runtime, owner.fan_id, upto=message.message_id)
    ask_count = sum(1 for row in history if signals(row.body, config) & {'offplatform', 'meetup'})
    push_count = sum(1 for row in history if signals(row.body, config) & {'offplatform', 'meetup', 'limit'})
    if found & {'offplatform', 'meetup', 'limit'} and push_count >= 3 and verdict['human'] is None:
        verdict['human'] = 'limit_push'
    if found & {'offplatform', 'meetup'} and ask_count >= 2:
        # A contact request is not a sales refusal. Preserve the operator flag,
        # but don't pause unrelated approved on-platform selling by itself.
        verdict['flags'].append('offplatform_ask')
    if 'refusal' in found:
        verdict['pause'] = 'sales_optout' if 'sales_optout' in found else 'refusal'
    if 'custom' in found:
        verdict['no_promise'] = True
        verdict['flags'].append('custom_request')
    return verdict


def _unlock_before(s, runtime, fan_id, message):
    """True for a scoped historical unlock observation, not proof of payment or current access."""
    latest = s.scalar(select(OfEngineAccessEvidence).join(OfTextIntent,
        (OfTextIntent.account_id == OfEngineAccessEvidence.account_id)
        & (OfTextIntent.intent_id == OfEngineAccessEvidence.intent_id)).where(
        OfEngineAccessEvidence.account_id == runtime.account_id, OfEngineAccessEvidence.fan_id == fan_id,
        OfEngineAccessEvidence.applied.is_(True), OfEngineAccessEvidence.status == 'confirmed',
        OfTextIntent.fan_id == fan_id, OfTextIntent.binding_id == runtime.binding_id,
        OfTextIntent.status == 'accepted', OfTextIntent.provider_message_id == OfEngineAccessEvidence.message_id)
        .order_by(OfEngineAccessEvidence.occurred_at.desc(), OfEngineAccessEvidence.created_at.desc()).limit(1))
    return latest is not None and message.created_at > latest.occurred_at


def journey_context(s, runtime, owner, request, message):
    """Facts for the model snapshot (engine hook 1). Empty for every account that is not 'fansapi'."""
    if not active(s, runtime.account_id) or owner is None:
        return {}
    config = _CONFIG
    verdict = decide(s, runtime, owner, message, config)
    ctx = {'signals': verdict['signals']}
    if verdict['human']:
        ctx['human_required'] = verdict['human']
    # No new card solely because he complimented a previously reported unlock (he asked for nothing).
    compliment = 'compliment' in verdict['signals'] and _unlock_before(s, runtime, owner.fan_id, message)
    gate = backfill_complete(s, runtime.account_id, owner.fan_id)
    unscoped = has_unscoped_messages(s, runtime.account_id, owner.fan_id, runtime.binding_id)
    # Hard blocks only. The owner's sales state (paused / scoped) is the existing sales mechanism and is judged
    # separately so an explicit, scoped reopening by the fan can still be honoured in the same turn.
    ctx['offers_allowed'] = bool(verdict['pause'] is None and not compliment and gate and not unscoped
                                 and not verdict['human'])
    ctx['sales_mode'] = owner.sales_mode
    ctx['backfill_complete'] = gate
    ctx['history_scope_incomplete'] = bool(unscoped)
    if compliment:
        ctx['compliment_after_unlock'] = True
    if verdict['no_promise']:
        ctx['no_promise'] = True
    line = identity_line_for(runtime.account_id, config)
    if line is not None and 'identity' in verdict['signals']:
        ctx['approved_identity_line'] = line
    return ctx


def _norm_words(text):
    return ' '.join(re.sub(r"[^a-z0-9 ]+", ' ', str(text).lower()).split())


def journey_problems(ctx, proposal):
    """Every journey rule a proposal breaks, as plain sentences (engine hook 2 and the model judge)."""
    if not ctx:
        return []
    problems = []
    if ctx.get('human_required'):
        problems.append('journey: this conversation needs a human (' + ctx['human_required'] + ')')
    if proposal.action == 'offer' and not ctx.get('offers_allowed', True):
        problems.append('journey: no offer is allowed in this turn')
    if proposal.action == 'offer' and ctx.get('sales_mode') == 'paused' and proposal.sales_signal != 'reopen':
        problems.append('journey: selling is paused and only his explicit, scoped interest reopens it')
    if ctx.get('compliment_after_unlock') and (proposal.action != 'reply' or proposal.pitch):
        problems.append('journey: he only complimented an earlier unlock; send no new card or pitch')
    if ctx.get('no_promise') and promises_custom(proposal.body, *proposal.parts):
        problems.append('journey: a custom request is flagged and must not be promised')
    line = ctx.get('approved_identity_line')
    if line and _norm_words(line) not in _norm_words(' '.join([proposal.body, *proposal.parts])):
        problems.append('journey: answer his identity question with exactly the approved wording')
    return problems


def enforce_journey(ctx, proposal):
    """Reject a proposal that breaks a journey rule (engine hook 2). `ctx` is the snapshot's journey facts."""
    problems = journey_problems(ctx, proposal)
    if problems:
        raise Refused(problems[0])


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
        reopened = False
        if verdict['pause']:
            self._pause_selling(tenant, account, fan_id, message_id, verdict['pause'])
        else:
            reopened = self._lapse_temporary_pause(tenant, account, fan_id, message_id)
        for flag in verdict['flags']:
            self._flag(tenant, account, fan_id, message_id, flag)
        if verdict['human']:
            self._to_human(tenant, account, fan_id, message_id, verdict['human'])
            return {'action': 'handled', 'reason': verdict['human'], 'signals': verdict['signals']}
        return {'action': 'plan', 'reason': None, 'signals': verdict['signals'], 'paused': verdict['pause'],
                'reopened': reopened}

    def after_evidence(self, tenant, account, fan_id):
        """Payment evidence changed. The whale hold/flag thresholds are deferred for the pilot: nothing to apply."""
        return {'action': 'none'}

    # -- writes
    def _pause_selling(self, tenant, account, fan_id, message_id, why):
        """Stop the pitch NOW: pause selling and invalidate every unsent sales draft for this fan."""
        with Session(self.db) as s, s.begin():
            self.store._runtime(s, tenant, account)
            owner = s.get(OfEngineOwner, (account, fan_id), with_for_update=True)
            if owner is None:
                return 'no_owner'
            invalidated = self._invalidate_unsent_sales(s, account, fan_id, why)
            if owner.sales_mode == 'paused' and owner.sales_source_message_id == message_id:
                return 'already_paused'
            # A newer refusal restarts the "not now" clock; a scoped reopening is replaced by the pause.
            owner.sales_mode, owner.sales_scope_json, owner.sales_source_message_id = 'paused', '[]', message_id
            owner.revision += 1
            owner.updated_at = utcnow()
            return 'paused' if not invalidated else f'paused:invalidated={invalidated}'

    @staticmethod
    def _invalidate_unsent_sales(s, account, fan_id, why):
        """Cancel this fan's UNSENT offer drafts (ready, or queued and never dispatched). Anything dispatching,
        unknown or accepted is left alone: an ambiguous or finished send is never touched."""
        cancelled = 0
        rows = s.scalars(select(OfEngineDecision).where(OfEngineDecision.account_id == account,
            OfEngineDecision.fan_id == fan_id, OfEngineDecision.action == 'offer',
            OfEngineDecision.status.in_(('ready', 'queued'))).with_for_update()).all()
        for decision in rows:
            if decision.status == 'queued':
                intent = s.get(OfTextIntent, (account, decision.intent_id), with_for_update=True) if decision.intent_id else None
                policy = s.get(OfDeliveryPolicy, account, with_for_update=True)
                if intent is None or intent.status != 'queued' or policy is None or policy.queued_count < 1:
                    continue
                intent.status, intent.reason, intent.updated_at = 'cancelled', ('sales_' + why)[:32], utcnow()
                policy.queued_count -= 1
            decision.status, decision.updated_at = 'cancelled', utcnow()
            cancelled += 1
        return cancelled

    def _lapse_temporary_pause(self, tenant, account, fan_id, message_id):
        """A TEMPORARY 'not now' / affordability pause becomes eligible for a fresh offer on a later inbound message.

        Only when enough hours passed since the refusing message; never for an explicit sales opt-out, limit or
        off-platform pause, or a pause whose reason is not a recognised temporary refusal. This runs on an inbound
        message only: it never creates outreach, and the new offer still needs the operator's approval.
        """
        hours = self.config.temporary_pause_reopen_hours
        if hours is None:
            return False
        with Session(self.db) as s, s.begin():
            self.store._runtime(s, tenant, account)
            owner = s.get(OfEngineOwner, (account, fan_id), with_for_update=True)
            message = s.get(Message, (account, fan_id, message_id))
            if (owner is None or owner.sales_mode != 'paused' or owner.sales_source_message_id is None
                    or message is None or message.direction != 'in'):
                return False
            source = s.get(Message, (account, fan_id, owner.sales_source_message_id))
            if source is None or source.direction != 'in' or source.message_id == message_id:
                return False
            found = signals(source.body, self.config)
            if 'refusal' not in found or found & {'sales_optout', 'offplatform', 'meetup', 'limit'}:
                return False
            if message.created_at - source.created_at < timedelta(hours=hours):
                return False
            owner.sales_mode, owner.sales_scope_json = 'open', '[]'
            owner.revision += 1
            owner.updated_at = utcnow()
            return True

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
