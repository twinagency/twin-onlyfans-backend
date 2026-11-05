"""Bounded evidence checks for increases in commercial authority.

This is not the conversation classifier. The combined writer still proposes the
meaning and action. These checks are used when that proposal would lift a saved
restriction or replace retained offer terms. Unsupported paraphrases keep their
restriction; ordinary permitted proactive offers do not require this grammar.
"""
import re

from .conversation import clauses, quotes


_SEND_STOP = re.compile(
    r"\b(?:do\s+not|don't|dont|never)\s+send\b|\bstop\s+(?:sending|offering)\b"
    r"|\bhold\s+off\s+on\s+(?:sending|the\s+(?:photo|video|card|offer))\b", re.I)
_CANCEL = re.compile(
    r"\b(?:cancel|never\s?mind|hold\s+off)(?:\s+(?:that|it|my\s+request|the\s+request|"
    r"the\s+order|the\s+offer|the\s+card|the\s+photo|the\s+video))?(?:\s*[.!?]*$)", re.I)
_GLOBAL_STOP = re.compile(
    r"\b(?:do\s+not|don't|dont|never)\s+send\s+(?:(?:me|us)\s+)?(?:anything|any\s+more(?:\s+(?:offers|content))?|more\s+(?:offers|content))\b(?=\s*(?:$|because\b|since\b))"
    r"|\bstop\s+(?:sending|offering)(?:\s+(?:me|us))?(?:\s+(?:anything|content|offers))?\s*[.!?]*$",
    re.I)
_REPORTED = re.compile(r"\b(?:say|says|said|saying|wrote|quoted|asked|told|meant|hypothetically)\b", re.I)
_REQUEST = re.compile(
    r"^\s*(?:(?:actually|instead|now|okay|ok|yes|yeah|yep|please)\b[\s,]*)*"
    r"(?:(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:send|show|offer)\b|(?:i\s+(?:want|would\s+like)|i'd\s+like)\b)", re.I)
_REFUSAL = re.compile(r"^\s*(?:i\s+(?:don't|do\s+not|dont)\s+want|"
                      r"(?:please\s+)?(?:don't|do\s+not)\s+(?:send|offer)|not)\b", re.I)
_NEGATIVE = re.compile(r"\b(?:no|nothing|not|never|don't|dont|do\s+not|can't|cannot|won't|wouldn't|without)\b", re.I)
_META_REQUEST = re.compile(r"\b(?:how|why|whether|price|cost|explain|describe|description|meaning|know|about|refund|delete|remove|cancel|discount)\b", re.I)
_WANT = re.compile(r"\bi(?:\s+(?:want|would\s+like)|'d\s+like)\b", re.I)
_MEDIA = re.compile(r"\b(?:photos?|pictures?|pics?|videos?|clips?|cards?|content|set|bundle)\b", re.I)
_QUALIFIER = re.compile(r"\b(?:if|unless|when|until|before|after|because|while|whether|what|how|why|"
                        r"tomorrow|later|yesterday|tonight|next|eventually|someday)\b", re.I)
_ASSENT = re.compile(r"^\s*(?:yes|yeah|yep|sure|okay|ok|please\s+do|go\s+ahead)"
                     r"(?:\s*,?\s*(?:please|send\s+(?:it|that(?:\s+one)?)))?\s*[.!]*$", re.I)
_REFERENCE = re.compile(r"\b(?:it|that(?:\s+one)?|this(?:\s+one)?)\b", re.I)
_ACK = re.compile(r"\s*(?:ok|okay|yes|yeah|yep|no|sure|please|thanks|thank\s+you|"
                  r"actually|instead|now|alright|hi|hey|hello|"
                  r"that(?:'s|s|\s+is)\s+too\s+much|too\s+expensive)\s*[.!]*", re.I)
_BUDGET_STATEMENT = re.compile(
    r"\s*(?:(?:actually|now|well)\s+)?(?:my\s+)?budget\s+(?:is\s+|of\s+|[:=]\s*)?"
    r"(?:(?:only|just)\s+)?\$(?P<amount>[0-9]{1,5}(?:\.[0-9]{1,2})?)\s*[.!]*", re.I)
_PERSONAL_BUDGET = re.compile(
    r"\s*i\s+(?:only\s+)?(?:have|can\s+spend|could\s+spend)\s+"
    r"(?:(?:only|just|up\s+to)\s+)?\$(?P<amount>[0-9]{1,5}(?:\.[0-9]{1,2})?)\s*[.!]*", re.I)


def _without_courtesy(text):
    # A closing thanks may address any name, not just a fixed list of pet names.
    # Keep conditions, negation and reported speech in scope before stripping it.
    match = re.search(r'\b(?:thanks|thank\s+you)\b', text, re.I)
    if match:
        closing = text[match.start():]
        if (re.fullmatch(r'(?:thanks|thank\s+you)(?:\s+[^\W\d_]+)?[^\w]*', closing, re.I)
                and not any(p.search(closing) for p in (_NEGATIVE, _QUALIFIER, _REPORTED, _META_REQUEST, _MEDIA, _REQUEST))):
            return text[:match.start()].rstrip(' ,.!')
    return text


def request_source(snapshot, quote=None):
    """Select the model's current source, never guess a topic from old history.

    A missing quote selects only the latest input for legacy/proactive plans.
    This binds provenance; affirmative scope and product facts are checked below.
    """
    from .conversation import pending_messages
    from .input_batch import current_requests
    incoming = current_requests([m for m in pending_messages(snapshot) if m.get('direction') == 'in'])
    if quote is None:
        return incoming[-1] if incoming else None
    def normalized(text):
        return ' '.join(text.translate(str.maketrans('‘’“”', '\'\'""')).casefold().split())
    needle = normalized(quote)
    return next((m for m in reversed(incoming) if needle and needle in normalized(m.get('text', ''))), None)


def _unquoted(text):
    text = text.replace('’', "'").replace('“', '"').replace('”', '"')
    for start, end, _ in reversed(quotes(text)):
        text = text[:start] + ' ' * (end - start) + text[end:]
    return text


def send_cancelled(text):
    """A scoped or global send cancellation, excluding unrelated topic changes."""
    return any(_SEND_STOP.search(part) or _CANCEL.search(part) for part in _asserted_clauses(text))


def stops_all_sends(text):
    return any(_GLOBAL_STOP.search(part) for part in _asserted_clauses(text))


def _asserted_clauses(text):
    """Shared source scope for observed preferences and deterministic stops."""
    for sentence in re.split(r'[.!?\n]+', _unquoted(text)):
        parts = list(clauses(sentence))
        for index, part in enumerate(parts):
            # A trailing explanation leaves a direct refusal intact. Carry
            # conditions across commas, not unrelated preceding sentences.
            part = re.split(r'\s+\b(?:because|since)\b', part, maxsplit=1, flags=re.I)[0]
            scope = ' '.join(parts[:index] + [part])
            if not _REPORTED.search(scope) and not _QUALIFIER.search(scope):
                yield part


def _simple_request(part):
    return bool(_REQUEST.search(part) and not _NEGATIVE.search(part) and not _REPORTED.search(part)
                and not _META_REQUEST.search(part) and not _QUALIFIER.search(part)
                and (not _WANT.search(part) or _MEDIA.search(part)))


def _simple_cancellation(part):
    text = _unquoted(part)
    text = re.sub(r'^\s*(?:(?:please|actually|now)\s+)*', '', text, flags=re.I)
    return bool((_SEND_STOP.match(text) or _CANCEL.fullmatch(text))
                and not _QUALIFIER.search(text) and not _REPORTED.search(text))


def affirmative_requests(text):
    """A restricted-action exception requires a wholly understood simple source.

    Unknown prefixes do not disappear when punctuation splits clauses. Every
    clause must be a direct request, simple cancellation, explicit budget or a
    bounded acknowledgement. Conditional/reported/deferred context stays unproven.
    """
    result = []
    for part in clauses(text):
        part = _without_courtesy(part)
        if not part:
            continue
        if _simple_cancellation(part):
            result = []
        elif _simple_request(part):
            result.append(part)
        elif _ACK.fullmatch(part) or _BUDGET_STATEMENT.fullmatch(part):
            continue
        else:
            return []
    return result


def budget_evidence(text, *, allow_personal=False):
    """Return directly asserted budget clauses only when their full scope is clear."""
    from decimal import Decimal
    result = []
    for part in clauses(text):
        found = _BUDGET_STATEMENT.fullmatch(part)
        if found is None and allow_personal:
            found = _PERSONAL_BUDGET.fullmatch(part)
        if found:
            result.append((part, int(Decimal(found.group('amount')) * 100)))
        elif _ACK.fullmatch(part) or _simple_request(part) or _simple_cancellation(part):
            continue
        else:
            return []
    return result


def has_replacement(text):
    return send_cancelled(text) and bool(affirmative_requests(text))


def _object_signature(text):
    """Only lexical distinctions with explicit object identity or media kind.

    Arbitrary descriptors may name the same asset and therefore do not prove
    independence. Unknown pronouns/aliases deliberately produce no identity.
    """
    identities = set(re.findall(r'\b(?:first|second|third|fourth|fifth)\b', text.lower()))
    identities.update('card:' + n for n in re.findall(r'\bcard\s*#?\s*(\d+)\b', text, re.I))
    kinds = set()
    if re.search(r'\b(?:photos?|pictures?|pics?|images?)\b', text, re.I): kinds.add('photo')
    if re.search(r'\b(?:videos?|clips?)\b', text, re.I): kinds.add('video')
    return identities, kinds


def cancellation_leaves_independent_request(prior_text, cancellation_text):
    """Prove a previous request is distinct; ambiguity stays fenced."""
    if stops_all_sends(cancellation_text):
        return False
    cancelled = [part for part in _asserted_clauses(cancellation_text) if send_cancelled(part)]
    requests = affirmative_requests(prior_text)
    if not cancelled or not requests:
        return False
    for stopped in cancelled:
        ids, kinds = _object_signature(stopped)
        for requested in requests:
            old_ids, old_kinds = _object_signature(requested)
            if ids and old_ids and ids.isdisjoint(old_ids):
                continue
            if kinds and old_kinds and kinds.isdisjoint(old_kinds):
                continue
            return False
    return True


def _contains(text, phrase):
    return bool(phrase and re.search(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)", text.lower()))


def _target_is_unique(part, product_id, snapshot):
    products = snapshot.get('products', {})
    inventory = snapshot.get('inventory', {})
    match = _REQUEST.search(part) or _REFUSAL.match(part)
    if not match:
        return False
    # The entire requested object must be accounted for. A matching product ID
    # does not make a conditional, informational or differently qualified
    # request into permission to send that product.
    body = re.sub(r'^\s*to\s+(?:see|get|buy|receive)\s+', '', part[match.end():], flags=re.I)
    object_tokens = set(re.findall(r'[a-z0-9]+', body.lower()))
    grammar = {'a','an','the','this','that','one','it','me','my','your','you','of','to',
               'please','now','instead','again','too','also'}
    kinds = {'photo','photos','picture','pictures','pic','pics','image','images',
             'video','videos','clip','clips','card','cards','content','set','bundle'}
    explicit = set()
    for key in products:
        label = inventory.get(key, {}).get('item', {}).get('label', '')
        if _contains(part, key) or _contains(part, label):
            explicit.add(key)
    if explicit and explicit != {product_id}:
        return False
    invitation = snapshot.get('invitation') or {}
    if (object_tokens <= grammar and _REFERENCE.search(body)
            and invitation.get('product_id') == product_id):
        return True
    candidates = set()
    from .product_grounding import _positive_facts
    for key, product in products.items():
        item = inventory.get(key, {})
        # An excluded feature mentioned in an asset description is evidence
        # of absence, not a selectable subject. Apply the existing positive
        # fact screen to descriptive labels as well as asset facts.
        label = _positive_facts({'assets': [{'facts': [item.get('item', {}).get('label', '')]}]})
        source = ' '.join([key, product.get('category', '').replace('_', ' '),
                           label, _positive_facts(product)])
        known = set(re.findall(r'[a-z0-9]+', source.lower()))
        # Kind words narrow actual files where available; a video request does
        # not become authority for the sole photograph in the catalogue.
        files = item.get('files', [])
        media_kinds = {file.get('kind') for file in files}
        if object_tokens & {'video','videos','clip','clips'} and 'video' not in media_kinds:
            continue
        if object_tokens & {'photo','photos','picture','pictures','pic','pics','image','images'} and 'photo' not in media_kinds:
            continue
        if (object_tokens and object_tokens <= known | grammar | kinds
                and (object_tokens & (known - grammar) or object_tokens & kinds)):
            candidates.add(key)
    return candidates == {product_id}


def authorizes_request(snapshot, product_id, kind, quote=None):
    from .conversation import pending_messages
    if product_id not in snapshot.get('products', {}):
        return False
    invitation = snapshot.get('invitation') or {}
    incoming = [m for m in pending_messages(snapshot) if m.get('direction') == 'in']
    message = request_source(snapshot, quote)
    if message is None:
        return False

    def retained_through_latest(index):
        # An older quotation does not make subsequent withdrawal or changed
        # meaning disappear. Unknown later input cannot increase authority.
        for later in incoming[index + 1:]:
            text = later.get('text', '')
            if _ASSENT.fullmatch(text):
                continue
            requests = affirmative_requests(text)
            if not requests or not all(_target_is_unique(part, product_id, snapshot) for part in requests):
                return False
        return True

    if not retained_through_latest(incoming.index(message)):
        return False
    text = message.get('text', '')
    if kind == 'accept' and invitation.get('product_id') == product_id and _ASSENT.fullmatch(text):
        return True
    if kind == 'accept' and invitation.get('product_id') != product_id:
        return False
    return any(_target_is_unique(part, product_id, snapshot) for part in affirmative_requests(text))


def supports_preference(observation, message, snapshot):
    """Check new sales holds and reversals against what the fan actually stated."""
    text = _unquoted(message.get('text', ''))
    if observation['active'] and observation['kind'] not in {'ordinary_chat_only', 'sales_refused'}:
        return True  # Existing hardship/boundary authority is unchanged.
    kind = observation['kind']
    if observation['active']:
        for part in _asserted_clauses(text):
            if observation.get('product_id') is not None:
                if (kind == 'sales_refused' and _REFUSAL.match(part)
                        and _target_is_unique(part, observation['product_id'], snapshot)):
                    return True
            elif stops_all_sends(part) or re.fullmatch(
                r"(?:please\s+)?(?:no\s+(?:more\s+)?(?:offers|sales|content)"
                r"|(?:just\s+chat|(?:i\s+)?only\s+want\s+to\s+chat)(?:\s+with\s+me)?"
                r"|(?:don't|do\s+not)\s+(?:send|offer|sell)\s+(?:me\s+)?(?:any\s+)?(?:more\s+)?(?:offers|content))"
                r"(?:\s+please)?", part, re.I):
                return True
        return False
    if _REPORTED.search(text) or _QUALIFIER.search(text) or stops_all_sends(text):
        return False
    if observation.get('product_id') is not None:
        return kind == 'sales_refused' and authorizes_request(
            {**snapshot, 'pending_fan_messages': [message]}, observation['product_id'], 'direct', observation['quote'])
    if kind in {'ordinary_chat_only', 'sales_refused'}:
        return bool(re.fullmatch(
            r"\s*(?:(?:you\s+can|feel\s+free\s+to|please|i\s+want\s+you\s+to|start|resume)\s+"
            r"(?:(?:send|sending|show|showing|make|making|suggest|suggesting)\s+(?:me\s+)?)?"
            r"(?:offers|offering|sales\s+pitches)\s*(?:again|now)?\s*[.!]*$"
            r"|i(?:'m|\s+am)\s+(?:okay|ok|fine)\s+with\s+(?:offers|sales\s+pitches)\s+again\s*[.!]*)",
            text, re.I)) and not _NEGATIVE.search(text)
    if kind == 'budget':
        return bool(re.fullmatch(r"\s*(?:(?:please\s+)?(?:remove|forget|ignore)\s+(?:my\s+|the\s+)?budget"
                                 r"|(?:my\s+)?budget\s+(?:no\s+longer\s+applies|is\s+unlimited))\s*[.!]*", text, re.I))
    if kind == 'boundary_hold':
        return bool(re.fullmatch(r"\s*(?:i\s+understand\s+(?:the\s+boundary|we\s+are\s+only\s+chatting\s+here)"
                                 r"|(?:the|that)\s+boundary\s+is\s+clear)\s*[.!]*", text, re.I))
    return False
