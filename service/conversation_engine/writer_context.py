"""Provider-neutral writer context: creator facts, prompt context, budgets and the PromptProvider interface.

A PromptProvider turns a PromptContext into chat messages. The engine builds the context from its own records
(corrected memory, approved offer, sales state, verified purchases, delivered history); a provider only words it.
Providers are looked up by name through prompt_registry.py and selected with ModelConfig.prompt_provider.
The model never gets send or charge authority from any provider.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
import re
from typing import Protocol

from onlyfans_runtime.store import Refused

TURN_KINDS = ('chat', 'content_request', 'role_question')


def is_price_question(text):
    """The existing informational-price prompt boundary, shared with dependencies."""
    return type(text) is str and bool(re.search(r'\b(?:prices?|costs?|how\s+much)\b', text, re.I))


# ---- creator facts ---------------------------------------------------------------
EXPLICIT_LEVELS = ('explicit', 'suggestive', 'none')


@dataclass(frozen=True)
class CreatorProfile:
    """The engine's creator facts as the writer sees them.

    `facts` mirrors the shared creator-fact keys (age, job, home_city, relationship, kids, family, tattoos, kinks,
    limits) plus free keys: only documented values. A fact that is absent is a documented gap, never a negative.
    `limits` are her documented boundaries (shown to the model and, as `limit_terms`, enforced in code). No other
    content restriction exists: permitted sexual conversation is never blocked by this layer.

    Missing inputs are never unlimited permission (Plan C): no documented `limits` means the profile is incomplete and
    the request goes to a human; no `role_statement` (the approved identity wording) means identity questions go to a
    human; `offers_permitted` is False and `approved_item_ids` empty by default, so nothing may be offered until the
    operator names the approved product(s).
    """
    name: str
    voice: str = ''                       # short description of her voice, optional
    facts: tuple = ()                     # ((key, value), ...)
    limits: tuple = ()                    # documented boundaries, plain language
    limit_terms: tuple = ()               # words/phrases that mark one of those limits being crossed (code-enforced)
    role_statement: str = ''              # APPROVED identity wording: the only answer to "what am I talking to"
    avoid_phrases: tuple = ()
    private_terms: tuple = ()
    explicit_chat: str = 'explicit'        # 'explicit' (adult site default) | 'suggestive' | 'none'; her documented chat level
    offers_permitted: bool = False        # commercial permission: may the engine offer anything at all
    approved_item_ids: tuple = ()         # catalogue item ids that may be offered (the pilot's one approved product)

    def __post_init__(self):
        def strs(values, limit, count):
            if type(values) is not tuple or len(values) > count or any(type(v) is not str or len(v) > limit for v in values):
                raise ValueError('invalid creator profile list')
        if type(self.name) is not str or not 1 <= len(self.name.strip()) <= 64:
            raise ValueError('creator name required')
        if type(self.voice) is not str or len(self.voice) > 400 or type(self.role_statement) is not str or len(self.role_statement) > 400:
            raise ValueError('invalid creator text')
        if (type(self.facts) is not tuple or len(self.facts) > 40
                or any(type(p) is not tuple or len(p) != 2 or type(p[0]) is not str or type(p[1]) is not str
                       or len(p[0]) > 40 or len(p[1]) > 300 for p in self.facts)):
            raise ValueError('invalid creator facts')
        strs(self.limits, 200, 30); strs(self.limit_terms, 60, 80)
        strs(self.avoid_phrases, 80, 60); strs(self.private_terms, 60, 60); strs(self.approved_item_ids, 20, 20)
        if type(self.offers_permitted) is not bool:
            raise ValueError('invalid commercial permission')
        if self.explicit_chat not in EXPLICIT_LEVELS:
            raise ValueError('invalid creator chat permission')

    def fact(self, key):
        return next((v for k, v in self.facts if k == key), None)

    def missing_inputs(self):
        """Names of required inputs this profile does not supply (boundaries block every turn)."""
        return tuple(name for name, present in (('boundaries', any(x.strip() for x in self.limits)),) if not present)

    def may_offer(self, item_id):
        return self.offers_permitted and str(item_id) in self.approved_item_ids

    @classmethod
    def from_facts(cls, name, facts, **extra):
        """Build from the shared creator-fact mapping (creator_facts.map_facts / effective output)."""
        if type(facts) is not dict:
            raise ValueError('facts must be an object')
        limits = tuple(x.strip() for x in str(facts.get('limits') or '').split(';') if x.strip())
        shown = tuple((k, str(v)) for k, v in sorted(facts.items()) if k not in ('limits', 'explicit_chat') and v not in (None, ''))
        if 'explicit_chat' in facts and 'explicit_chat' not in extra:
            extra['explicit_chat'] = str(facts['explicit_chat']).strip().lower()
        if extra.get('explicit_chat', 'explicit') not in EXPLICIT_LEVELS:
            raise ValueError('explicit_chat must be one of ' + ', '.join(EXPLICIT_LEVELS))
        return cls(name=name, facts=shown, limits=limits, **extra)

    @classmethod
    def from_dict(cls, raw):
        if type(raw) is not dict:
            raise ValueError('profile must be an object')
        facts = raw.get('facts') or {}
        if type(facts) is not dict:
            raise ValueError('profile facts must be an object')
        levels = [source['explicit_chat'] for source in (raw, facts) if 'explicit_chat' in source]
        if any(type(level) is not str for level in levels):
            raise ValueError('invalid creator chat permission')
        levels = [level.strip().lower() for level in levels]
        if any(level not in EXPLICIT_LEVELS for level in levels) or len(set(levels)) > 1:
            raise ValueError('invalid or conflicting creator chat permissions')
        return cls(name=raw['name'], voice=raw.get('voice') or '',
                   facts=tuple((k, str(v)) for k, v in facts.items() if k != 'explicit_chat'),
                   limits=tuple(raw.get('limits') or ()), limit_terms=tuple(raw.get('limit_terms') or ()),
                   role_statement=raw.get('role_statement') or '', avoid_phrases=tuple(raw.get('avoid_phrases') or ()),
                   private_terms=tuple(raw.get('private_terms') or ()), explicit_chat=levels[0] if levels else 'explicit',
                   offers_permitted=raw.get('offers_permitted') is True,
                   approved_item_ids=tuple(str(x) for x in (raw.get('approved_item_ids') or ())))


class CreatorProfiles(Protocol):
    def get(self, account_id: str) -> 'CreatorProfile | None': ...


class StaticCreatorProfiles:
    def __init__(self, profiles):
        self._profiles = dict(profiles)

    def get(self, account_id):
        return self._profiles.get(account_id)


class JsonFileCreatorProfiles:
    """{account_id: {name, voice, facts:{}, limits:[], ...}} read on each call (small, operator-owned file)."""
    def __init__(self, path):
        self._path = path

    def get(self, account_id):
        # An unreadable, malformed or invalid entry is a MISSING input (the request goes to a human), never a default.
        try:
            with open(self._path, encoding='utf-8') as handle:
                raw = json.load(handle)
            item = raw.get(account_id) if type(raw) is dict else None
            return CreatorProfile.from_dict(item) if item is not None else None
        except (OSError, ValueError, KeyError, TypeError):
            return None


_ACTIVE_PROFILES = None


def set_active_profiles(profiles):
    """The profile source the running application uses (set once by model_transport.wiring). None clears it."""
    global _ACTIVE_PROFILES
    _ACTIVE_PROFILES = profiles


def profile_data(profile):
    """Canonical, complete creator input for durable scope binding."""
    from dataclasses import asdict
    if type(profile) is not CreatorProfile:
        raise Refused('creator profile is unavailable')
    data = asdict(profile)
    data['facts'] = dict(profile.facts)
    return json.loads(json.dumps(data))


def active_profile(account_id, *, session=None):
    """The approved creator profile for an account from the active source, or None (missing input)."""
    if session is not None:
        from db.onlyfans_engine_models import OfEnginePilotScope
        from sqlalchemy import select
        from . import scope_contract as contracts
        row = session.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.account_id == account_id))
        # Only this account's legacy pilot or commercial scope binds a profile; a preparation row binds nothing.
        kind = contracts.claim(row) if row is not None else None
        if kind in (contracts.LEGACY, contracts.COMMERCIAL):
            config = contracts.validate(kind, json.loads(row.config_json))
            if contracts.fingerprint(config) != row.fingerprint or contracts.encoded(config) != row.config_json:
                raise Refused('pilot creator profile binding changed')
            return CreatorProfile.from_dict(config['creator_profile'])
    if _ACTIVE_PROFILES is not None:
        return _ACTIVE_PROFILES.get(account_id)
    path = os.environ.get('ENGINE_MODEL_PROFILES_FILE', '')
    return JsonFileCreatorProfiles(path).get(account_id) if path else None


# ---- context / budget / provider interface ------------------------------------------
@dataclass(frozen=True)
class PromptBudget:
    system_chars: int = 9000
    user_chars: int = 10000
    history_messages: int = 16
    history_message_chars: int = 400
    latest_chars: int = 1200
    latest_review_chars: int = 6000   # a latest message longer than this is not written for: it goes to a human (no model call)
    memory_chars: int = 900
    memory_items: int = 10             # eligible learned memories rendered (eligibility is decided before this bound)
    memory_excerpts: int = 3
    request_chars: int = 1800
    requests: int = 8
    unanswered: int = 5                # earlier fan messages since her last reply shown as UNANSWERED (most recent first kept)
    purchases: int = 20
    examples: int = 12
    example_chars: int = 140


@dataclass(frozen=True)
class PromptContext:
    profile: CreatorProfile
    turn_kind: str
    fan_text: str
    history: tuple = ()               # ((role, text), ...) oldest first; delivered messages only; excludes the latest
    memory_lines: tuple = ()
    memory_excerpts: tuple = ()
    memory_available: bool = True
    request_context: tuple = ()       # scoped work and existing-card facts; never fresh action authority
    sales_mode: str = 'open'          # open | paused | reopened_scoped
    sales_scope: tuple = ()           # item ids when reopened_scoped
    scope_items: tuple = ()           # ({'itemId','label'}) for those ids when known
    offer: dict | None = None         # {grantId, itemId, label, description}: the ONE approved offer for this turn
    price_information: tuple = ()     # scoped current listing facts; no grant or availability
    outstanding_card: dict | None = None   # a card already sent that his message refers to: {label, priceCents}
    purchases: tuple | None = None    # verified purchases (labels); None = records unavailable
    identity_line: str = ''           # the APPROVED identity wording; '' = none (identity questions go to a human)
    recent_questions: tuple = ()      # questions she already asked lately (never ask them again)
    examples: tuple = ()              # ExampleLine (style only)
    fix: tuple = ()                   # reasons the last draft was rejected
    prefixed: bool = False            # in-character retry
    max_texts: int = 4
    commercial: bool = True           # False in text-only delivery mode: no offer, contract or sales-state instructions


@dataclass(frozen=True)
class RenderedPrompt:
    messages: tuple
    block_names: tuple
    stats: dict
    sha256: str


class PromptProvider(Protocol):
    """Swappable writer prompt. Register with prompt_registry.register_prompt_provider(name)."""
    name: str

    def render(self, ctx: PromptContext) -> RenderedPrompt: ...


def bounded_latest(text, limit):
    """(shown_text, omitted_chars). A long latest message keeps its beginning AND its end (corrections and questions
    usually come last) with an explicit omission marker, never a silent prefix cut. Whitespace is normalised first."""
    flat = ' '.join(str(text or '').split())
    if len(flat) <= limit:
        return flat, 0
    marker_for = lambda n: f' [... {n} characters omitted from the middle of this long message; the beginning and the end are shown, do not guess the omitted part ...] '
    omitted = len(flat) - limit
    for _ in range(3):   # the marker itself uses room, so settle the omitted count
        marker = marker_for(omitted)
        room = max(0, limit - len(marker))
        omitted = len(flat) - room
    marker = marker_for(omitted)
    room = max(0, limit - len(marker))
    head = room // 3
    tail = room - head
    return flat[:head].rstrip() + marker + flat[len(flat) - tail:].lstrip(), len(flat) - head - tail


def digest_messages(messages):
    return sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---- context building (code decides; no model involved) -----------------------------
_REAL_END = r"(?=\s*(?:[?.!,]|$|\s(?:or|person|human|girl|woman|lady|babe|baby|here|right now|lol|haha|though|actually|really)\b))"
_ROLE_RX = re.compile(
    r"\b(?:(?:are|r) (?:you|u|ya) (?:even |actually |really |an? )*(?:(?:bot|ai|robot|chatbot|person|automated|human)\b"
    r"|(?:real|girl|her|you|fake)" + _REAL_END + r")"
    r"|(?:you|u) (?:a|an) (?:bot|robot|ai|chatbot|real (?:person|girl|human))\b"
    r"|is this (?:an? )?(?:(?:bot|ai|robot|chatbot|automated)\b|(?:really |actually )?(?:real|fake)" + _REAL_END + r"|really (?:her|you)\b)"
    r"|am i (?:really |actually |still )?(?:talking|chatting|speaking|texting|messaging) (?:to|with) (?:an? |the )?"
    r"(?:real|bot|ai|human|person|someone|agency|chatter|team|staff|employee)\b"
    r"|who am i (?:really |actually )?(?:talking|chatting|speaking|texting) (?:to|with)"
    r"|is (?:someone|somebody|anyone) else (?:typing|texting|writing|answering|replying|reading|there)"
    r"|who runs this account|do you work for an? agency"
    r"|are you (?:with|using|working for) (?:an? )?agency"
    r"|(?:are you|you are|you're|is this) (?:an? )?chatter"
    r"|is (?:an? )?(?:agency|chatter|team) (?:running|answering|writing)"
    r"|who(?:'s| is) (?:this|that)(?= ?(?:exactly|really|actually|though|then|now|again)?\s*(?:\?|$))"
    r"|who(?:'s| is) (?:really )?(?:typing|texting|writing|answering|behind (?:this|the account))|this isn'?t (?:really )?(?:her|you))\b", re.I)
_CONTENT_RX = re.compile(r"\b(send|show|got|have|any|sell|buy|want|see)\b.{0,40}\b(pics?|pictures?|photos?|vids?|videos?|nudes?|content|ppv|set|bundle|more of you|clip)\b|"
                         r"\b(pics?|photos?|vids?|videos?|nudes?|ppv)\b.{0,20}\b(please|pls|of you|for me)\b", re.I)


def identity_question(text):
    """Shared interpretation for writing, validation and journey decisions, not a send proof."""
    return bool(_ROLE_RX.search(str(text or '').replace('’', "'")))


def classify_turn(fan_text):
    if identity_question(fan_text):
        return 'role_question'
    if _CONTENT_RX.search(fan_text):
        return 'content_request'
    return 'chat'


def _sales_permits(snapshot, item_id):
    mode = snapshot.get('sales_mode')
    if mode == 'open':
        return True
    return mode == 'reopened_scoped' and str(item_id) in {str(x) for x in snapshot.get('sales_scope', [])}


def pick_offer(snapshot, fan_text):
    """At most one approved grant for this turn: sales state first, then best word overlap with his message."""
    grants = [g for g in snapshot.get('offer_grants', []) if _sales_permits(snapshot, g['itemId'])]
    if not grants:
        return None
    words = set(re.findall(r'[a-z0-9]+', fan_text.lower()))
    video = bool(words & {'video', 'videos', 'vid', 'vids', 'clip', 'clips'})
    picture = bool(words & {'photo', 'photos', 'picture', 'pictures', 'pic', 'pics', 'image', 'images'})
    if video != picture:
        key = 'videos' if video else 'pictures'
        grants = [g for g in grants if isinstance(g.get('media'), dict)
                  and type(g['media'].get(key)) is int and g['media'][key] > 0]
        if not grants:
            return None
    def score(grant):
        text = set(re.findall(r'[a-z0-9]+', f"{grant.get('label', '')} {grant.get('description', '')}".lower()))
        return (-len(words & text), str(grant['itemId']))
    return sorted(grants, key=score)[0]


def offer_for(snapshot, profile, fan_text):
    """The ONE approved offer this turn may use, or None.

    Layers, all of which must allow it: the engine's own approved grant and sales state (pick_offer), the journey
    facts (refusal pause, compliment after a purchase, incomplete history), and the creator profile's commercial
    permission for that catalogue item. A missing permission is "no offer", never "any offer".
    """
    journey = snapshot.get('journey') or {}
    if journey.get('offers_allowed') is False or snapshot.get('delivery_mode') == 'text_only':
        return None   # text-only delivery has no commercial action at all
    offer = pick_offer(snapshot, fan_text)
    if offer is None or profile is None or not profile.may_offer(offer['itemId']):
        return None
    return offer


def recent_her_questions(snapshot, limit=3):
    """Questions she asked in her last few delivered messages, oldest first (so none is asked a second time)."""
    out = []
    for row in snapshot.get('history', []):
        if row.get('direction') != 'out':
            continue
        for sentence in re.findall(r'[^.!?\n]*\?', row.get('body') or ''):
            sentence = ' '.join(sentence.split())
            if len(sentence) > 4:
                out.append(sentence[:140])
    return tuple(out[-limit:])


_FACT_LABELS = {'real_name': 'his name', 'his_age': 'his age', 'home_city': 'his city', 'home_country': 'his country',
                'hobbies': 'his hobbies', 'occupation': 'his job', 'fetishes': 'turn-ons he told her'}


def memory_lines_from(view, budget):
    if not isinstance(view, dict) or view.get('state') != 'current':
        return (), (), False
    memory = view.get('memory') or {}
    lines, essential = [], {}
    clipped = False
    def excerpt(value, maximum):
        nonlocal clipped
        text = str(value)
        if len(text) > maximum:
            clipped = True
            return text[:max(0,maximum-3)] + '...'
        return text
    clears = {key for key, item in (memory.get('overrides') or {}).items()
              if isinstance(item, dict) and item.get('operation') == 'clear'}
    if clears:
        lines.append('Operator-cleared fields (do not reuse older statements): ' +
                     ', '.join(_FACT_LABELS.get(key, key.replace('_',' ')) for key in sorted(clears)))
    for key, item in sorted((memory.get('facts') or {}).items()):
        if key not in clears and isinstance(item, dict) and item.get('value'):
            lines.append(f"{_FACT_LABELS.get(key, key)}: {excerpt(item['value'],80)}")
    for key, value in sorted((memory.get('summaries') or {}).items()):
        if value:
            lines.append(f"{key.replace('_', ' ')}: {excerpt(value,200)}")
    learned = memory.get('learnedProfile')
    if isinstance(learned, dict):
        overridden = set(memory.get('overrides') or {})
        overridden.update(k for k, v in (memory.get('facts') or {}).items()
                          if isinstance(v, dict) and v.get('source') == 'operator')
        aliases = {'location':'home_city', 'current_location':'home_city', 'name':'real_name',
                   'age':'his_age', 'job':'occupation', 'interests':'hobbies'}
        # Eligibility (fan subject, valid shape, not overridden by an operator correction, has source evidence) is decided
        # on the WHOLE list first; only then is the eligible list bounded, and any eligible item left out is reported.
        eligible = []
        for item in (learned.get('memories') or []):
            if not isinstance(item, dict) or item.get('subject') != 'fan':
                continue
            key, value, status = item.get('field'), item.get('value'), item.get('status')
            if (not isinstance(key, str) or not isinstance(value, str) or not value.strip()
                    or status not in {'current', 'upcoming', 'uncertain', 'past', 'cancelled', 'expired', 'superseded'}
                    or aliases.get(key, key) in overridden):
                continue
            proof = next((e for e in reversed(item.get('evidence') or [])
                          if isinstance(e, dict) and isinstance(e.get('source'), str)), None)
            if proof is None:
                continue
            eligible.append((item, key, value, status, proof))
        if len(eligible) > budget.memory_items:
            clipped = True   # reported through the 'omitted by budget' notice below
        for item, key, value, status, proof in eligible[:budget.memory_items]:
            state = {'cancelled': 'cancelled, historical; not active or action authority',
                     'expired': 'expired, historical; outcome unconfirmed',
                     'superseded': 'superseded, historical; not current fact'}.get(status, status)
            qualifier = state + (', potentially stale' if item.get('potentially_stale') else '')
            for name in ('last_confirmed','event_start','event_end','review_at','active_until'):
                if item.get(name):
                    qualifier += ', ' + name + ' ' + excerpt(item[name],25)
            if proof.get('at'):
                qualifier += ', source_at ' + excerpt(proof['at'],25)
            if proof.get('quote'):
                qualifier += ', source_quote ' + excerpt(proof['quote'],80)
            alternatives = item.get('alternatives') or []
            if len(alternatives)>3: clipped=True
            if alternatives:
                qualifier += ', conflicting evidence: ' + ' / '.join(excerpt(v,60) for v in alternatives[:3])
            if item.get('event_outcome') == 'unconfirmed':
                qualifier += ', outcome unconfirmed'
            context = item.get('contextEvidence') or []
            if context and isinstance(context[-1], dict) and context[-1].get('quote'):
                qualifier += ', answer to: ' + excerpt(context[-1]['quote'],120)
            line = f"{key.replace('_', ' ')}: {excerpt(value,200)} ({qualifier}; source {excerpt(proof['source'],80)})"
            core = state + (', conflicting evidence' if alternatives else '')
            if item.get('potentially_stale'): core += ', potentially stale'
            for name in ('last_confirmed','event_start','event_end','review_at','active_until','event_outcome'):
                if item.get(name): core += ', ' + name + ' ' + excerpt(item[name],25)
            essential[line] = f"{key.replace('_', ' ')}: {excerpt(value,200)} ({core}; source {excerpt(proof['source'],80)})"
            lines.append(line)
    notice = 'Some memory details omitted by budget; retain uncertainty and do not infer absence.'[:max(0,budget.memory_chars)]
    limit = max(0,budget.memory_chars-len(notice)-2)
    bounded, size, omitted = [], 0, clipped
    for line in lines:
        cost = len(line) + (2 if bounded else 0)
        if size + cost > limit and line in essential:
            line = essential[line]
            cost = len(line) + (2 if bounded else 0)
            omitted = True
        if size + cost > limit:
            omitted = True
            continue
        bounded.append(line)
        size += cost
    if omitted and notice:
        bounded.append(notice)
    excerpts = tuple(str(i.get('excerpt', ''))[:160] for i in (memory.get('interactions') or [])[:budget.memory_excerpts] if i.get('excerpt'))
    return tuple(bounded), excerpts, True


def _attachment_fact(count, with_text=False):
    """An inbound attachment is a fact about the message; what it shows is never known to the writer."""
    n = f'{count} file' if count == 1 else f'{count} files'
    return (f'[+{n} attached; contents not visible to you]' if with_text
            else f'[attachment only: {n}; contents not visible to you, do not guess what it shows]')


def _history_rows(snapshot, budget):
    current_id = snapshot['current_message']['id']
    rows = []
    for row in snapshot.get('history', []):
        if row.get('id') == current_id:
            continue
        who = 'FAN' if row['direction'] == 'in' else 'HER'
        text = (row.get('body') or '').strip()
        if row['direction'] == 'in' and (row.get('media_count') or 0) > 0:
            text = (text + ' ' if text else '') + _attachment_fact(row['media_count'], bool(text))
        if row['direction'] == 'out' and (row.get('media_count') or row.get('price_cents')):
            if (row.get('price_cents') or 0) > 0:
                cards = [card for card in (snapshot.get('offer_reference') or {}).get('cards', [])
                         if str(card.get('messageId')) == str(row.get('id')) and card.get('sendState') == 'accepted']
                card = cards[0] if len(cards) == 1 else None
                if card is not None and card.get('financialReview'):
                    label = '[paid card; access/unlock requires review; financial settlement not established here]'
                elif card is not None and card.get('accessState') in ('confirmed', 'revoked'):
                    label = '[paid card; access/unlock ' + card['accessState'] + '; financial settlement not established here]'
                else:
                    label = '[paid card; access/unlock recorded; financial settlement not established here]' if row.get('paid') else '[paid card; access/unlock unconfirmed]'
            elif row.get('price_cents') == 0:
                label = '[free media; no paid unlock required]'
            else:
                label = '[media shared; pricing and access status unavailable]'
            text = (text + ' ' if text else '') + label
        if text:
            rows.append((who, text[:budget.history_message_chars]))
    return tuple(rows)


def request_context_from(snapshot, budget):
    """Reserve prompt space for uncertain actions and the active request."""
    requests = [r for r in snapshot.get('open_requests', []) if isinstance(r, dict)]
    actions = [r for r in snapshot.get('pending_actions', []) if isinstance(r, dict)]
    unknown = [r for r in actions if r.get('outcome') in ('unknown','dispatching')]
    active = [r for r in requests if str(r.get('source_message_id')) == str(snapshot['current_message']['id'])]
    references = snapshot.get('offer_reference') or {}
    cards = [c for c in references.get('cards', []) if isinstance(c, dict)]
    groups = [('ACTION', unknown, budget.request_chars // (3 if cards else 2)),
              ('REQUEST', active, budget.request_chars // 4),
              ('CARD', cards, budget.request_chars // 3),
              ('REQUEST', [r for r in requests if r not in active], budget.request_chars),
              ('ACTION', [r for r in actions if r not in unknown], budget.request_chars)]
    lines, size, omitted, shown_sources = [], 0, False, set()
    total = max(0, budget.request_chars - (150 if cards or references.get('cardsCoverage') == 'incomplete' else 90))
    for kind, records, quota in groups:
        used = 0
        for row in records[:budget.requests]:
            fields = (('request_id', 'source_message_id', 'status', 'owner', 'next_step') if kind == 'REQUEST' else
                      ('offerId', 'itemId', 'intentId', 'messageId', 'label', 'priceCents', 'currency',
                       'financialReview') if kind == 'CARD' else
                      ('decision_id', 'request_id', 'generation', 'action', 'outcome'))
            data = {k: row[k] for k in fields if k in row}
            if kind == 'CARD' and isinstance(data.get('label'), str) and len(data['label']) > 120:
                data['label'] = data['label'][:117] + '...'
                omitted = True
            body = row.get('source_body' if kind == 'REQUEST' else 'body')
            if isinstance(body, str):
                data['source_text' if kind == 'REQUEST' else 'proposed_text'] = body[:160]
            prefix = ('CARD facts (reference ' + str(references.get('state', 'unavailable'))
                      + '; send ' + str(row.get('sendState', 'unavailable'))
                      + ('; message deleted; current availability unknown' if row.get('messageDeleted') else '')
                      + '; access ' + str(row.get('accessState', 'unavailable'))
                      + '; financial settlement not established; not action authority): '
                      if kind == 'CARD' else kind + ' ')
            line = prefix + json.dumps(data, ensure_ascii=False, sort_keys=True)
            if used + len(line) > quota or size + len(line) > total:
                omitted = True
                continue
            lines.append(line);size += len(line);used += len(line)
            if kind == 'REQUEST':
                shown_sources.add(str(row.get('source_message_id')))
        omitted = omitted or len(records) > budget.requests
    # Earlier messages of his since her last delivered reply that no REQUEST line above covers (a burst: the newest message
    # became the current request, the older ones were superseded or never planned). Derived from the same message and request
    # records, not a new ledger; whatever does not fit is reported as omitted, never silently dropped.
    history = [r for r in snapshot.get('history', []) if isinstance(r, dict)]
    last_out = max((i for i, r in enumerate(history) if r.get('direction') == 'out'), default=-1)
    pending = [r for r in history[last_out + 1:] if r.get('direction') == 'in' and r.get('id') != snapshot['current_message']['id']
               and str(r.get('id')) not in shown_sources and ((r.get('body') or '').strip() or r.get('media_count'))]
    if len(pending) > budget.unanswered:
        omitted = True
        pending = pending[-budget.unanswered:]
    used = 0
    for row in pending:
        text = (row.get('body') or '').strip()[:160]
        if (row.get('media_count') or 0) > 0:
            text = (text + ' ' if text else '') + _attachment_fact(row['media_count'], bool(text))
        line = 'UNANSWERED ' + json.dumps({'message_id': row.get('id'), 'text': text}, ensure_ascii=False, sort_keys=True)
        if used + len(line) > budget.request_chars // 3 or size + len(line) > total:
            omitted = True
            continue
        lines.append(line); size += len(line); used += len(line)
    incomplete = references.get('cardsCoverage') == 'incomplete' or references.get('cardsTruncated')
    if omitted or incomplete or snapshot.get('open_requests_truncated') or snapshot.get('pending_actions_truncated'):
        lines.append('Additional unresolved/card records omitted or coverage incomplete; do not infer completion or a known outcome.'
                     if cards or incomplete else 'Additional unresolved records omitted; do not infer completion or a known outcome.')
    return tuple(lines)


def build_context(snapshot, profile, *, examples=(), fix=(), prefixed=False, budget=PromptBudget()):
    fan_text = snapshot['current_message']['body']
    offer = offer_for(snapshot, profile, fan_text)
    if not (fan_text or '').strip():
        # A media-only message is an attachment fact, not an empty message and not a description of what it shows.
        current = next((r for r in snapshot.get('history', []) if isinstance(r, dict)
                        and r.get('id') == snapshot['current_message']['id']), {})
        if (current.get('media_count') or 0) > 0:
            fan_text = _attachment_fact(current['media_count'])
    journey = snapshot.get('journey') or {}
    identity_line = (getattr(profile, 'role_statement', '') or journey.get('approved_identity_line') or '').strip()
    lines, excerpts, available = memory_lines_from(snapshot.get('memory'), budget)
    matched = (snapshot.get('offer_reference') or {}).get('offers') or []
    purchases = snapshot.get('verified_purchases')
    catalogue = {str(i['itemId']): i for i in (snapshot.get('catalogue_search') or {}).get('items', [])}
    scope = tuple(str(x) for x in snapshot.get('sales_scope', []))
    scope_items = tuple({'itemId': i, 'label': catalogue[i].get('label', '')} for i in scope if i in catalogue)
    return PromptContext(profile=profile, turn_kind=classify_turn(fan_text), fan_text=fan_text,
        history=_history_rows(snapshot, budget), memory_lines=lines, memory_excerpts=excerpts, memory_available=available,
        request_context=request_context_from(snapshot, budget),
        sales_mode=snapshot.get('sales_mode', 'open'), sales_scope=scope, scope_items=scope_items, offer=offer,
        price_information=(() if snapshot.get('delivery_mode') == 'text_only' else
                           tuple(i for i in snapshot.get('price_information', []) if profile.may_offer(i['itemId']))),
        outstanding_card=matched[0] if matched else None,
        purchases=tuple(p['label'] for p in purchases[:budget.purchases]) if isinstance(purchases, list) else None,
        identity_line=identity_line, recent_questions=recent_her_questions(snapshot),
        examples=tuple(examples), fix=tuple(fix), prefixed=prefixed,
        commercial=snapshot.get('delivery_mode') != 'text_only')
