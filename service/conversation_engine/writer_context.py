"""Provider-neutral writer context: creator facts, prompt context, budgets and the PromptProvider interface.

A PromptProvider turns a PromptContext into chat messages. The engine builds the context from its own records
(corrected memory, approved offer, sales state, verified purchases, delivered history); a provider only words it.
Providers are looked up by name through prompt_registry.py and selected with ModelConfig.prompt_provider.
The model never gets send or charge authority from any provider.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from typing import Protocol

from onlyfans_runtime.store import Refused

TURN_KINDS = ('chat', 'content_request', 'role_question')


# ---- creator facts ---------------------------------------------------------------
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
        shown = tuple((k, str(v)) for k, v in sorted(facts.items()) if k != 'limits' and v not in (None, ''))
        return cls(name=name, facts=shown, limits=limits, **extra)

    @classmethod
    def from_dict(cls, raw):
        if type(raw) is not dict:
            raise ValueError('profile must be an object')
        return cls(name=raw['name'], voice=raw.get('voice') or '',
                   facts=tuple((k, str(v)) for k, v in (raw.get('facts') or {}).items()),
                   limits=tuple(raw.get('limits') or ()), limit_terms=tuple(raw.get('limit_terms') or ()),
                   role_statement=raw.get('role_statement') or '', avoid_phrases=tuple(raw.get('avoid_phrases') or ()),
                   private_terms=tuple(raw.get('private_terms') or ()),
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


def active_profile(account_id):
    """The approved creator profile for an account from the active source, or None (missing input)."""
    return _ACTIVE_PROFILES.get(account_id) if _ACTIVE_PROFILES is not None else None


# ---- context / budget / provider interface ------------------------------------------
@dataclass(frozen=True)
class PromptBudget:
    system_chars: int = 9000
    user_chars: int = 10000
    history_messages: int = 16
    history_message_chars: int = 400
    latest_chars: int = 1200
    memory_chars: int = 900
    memory_excerpts: int = 3
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
    sales_mode: str = 'open'          # open | paused | reopened_scoped
    sales_scope: tuple = ()           # item ids when reopened_scoped
    scope_items: tuple = ()           # ({'itemId','label'}) for those ids when known
    offer: dict | None = None         # {grantId, itemId, label, description}: the ONE approved offer for this turn
    outstanding_card: dict | None = None   # a card already sent that his message refers to: {label, priceCents}
    purchases: tuple | None = None    # verified purchases (labels); None = records unavailable
    identity_line: str = ''           # the APPROVED identity wording; '' = none (identity questions go to a human)
    recent_questions: tuple = ()      # questions she already asked lately (never ask them again)
    examples: tuple = ()              # ExampleLine (style only)
    fix: tuple = ()                   # reasons the last draft was rejected
    prefixed: bool = False            # in-character retry
    max_texts: int = 4


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


def digest_messages(messages):
    return sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---- context building (code decides; no model involved) -----------------------------
_ROLE_RX = re.compile(r"\b(are you (a )?(real|bot|ai|robot|human|fake|an? agency|chatbot)|is this (a )?(bot|ai|real|fake|agency)|"
                      r"(real|actual) (person|girl|woman)|who(?:'?s| is) (this|typing|really)|am i (talking|chatting) to (a )?(bot|ai|real)|"
                      r"this isn'?t (really )?(her|you)|agency)\b", re.I)
_CONTENT_RX = re.compile(r"\b(send|show|got|have|any|sell|buy|want|see)\b.{0,40}\b(pics?|pictures?|photos?|vids?|videos?|nudes?|content|ppv|set|bundle|more of you|clip)\b|"
                         r"\b(pics?|photos?|vids?|videos?|nudes?|ppv)\b.{0,20}\b(please|pls|of you|for me)\b", re.I)


def classify_turn(fan_text):
    if _ROLE_RX.search(fan_text):
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
    if journey.get('offers_allowed') is False:
        return None
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
    lines = []
    for key, item in sorted((memory.get('facts') or {}).items()):
        if isinstance(item, dict) and item.get('value'):
            lines.append(f"{_FACT_LABELS.get(key, key)}: {str(item['value'])[:80]}")
    for key, value in sorted((memory.get('summaries') or {}).items()):
        if value:
            lines.append(f"{key.replace('_', ' ')}: {str(value)[:200]}")
    learned = memory.get('learnedProfile')
    if isinstance(learned, dict):
        for key, value in sorted(learned.items()):
            if isinstance(value, str) and value:
                lines.append(f"{key.replace('_', ' ')}: {value[:200]}")
    excerpts = tuple(str(i.get('excerpt', ''))[:160] for i in (memory.get('interactions') or [])[:budget.memory_excerpts] if i.get('excerpt'))
    return tuple(lines), excerpts, True


def _history_rows(snapshot, budget):
    current_id = snapshot['current_message']['id']
    rows = []
    for row in snapshot.get('history', []):
        if row.get('id') == current_id:
            continue
        who = 'FAN' if row['direction'] == 'in' else 'HER'
        text = (row.get('body') or '').strip()
        if row['direction'] == 'out' and (row.get('media_count') or row.get('price_cents')):
            text = (text + ' ' if text else '') + ('[a locked card he opened]' if row.get('paid') else '[a locked card, not opened]')
        if text:
            rows.append((who, text[:budget.history_message_chars]))
    return tuple(rows)


def build_context(snapshot, profile, *, examples=(), fix=(), prefixed=False, budget=PromptBudget()):
    fan_text = snapshot['current_message']['body']
    offer = offer_for(snapshot, profile, fan_text)
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
        sales_mode=snapshot.get('sales_mode', 'open'), sales_scope=scope, scope_items=scope_items, offer=offer,
        outstanding_card=matched[0] if matched else None,
        purchases=tuple(p['label'] for p in purchases[:budget.purchases]) if isinstance(purchases, list) else None,
        identity_line=identity_line, recent_questions=recent_her_questions(snapshot),
        examples=tuple(examples), fix=tuple(fix), prefixed=prefixed)
