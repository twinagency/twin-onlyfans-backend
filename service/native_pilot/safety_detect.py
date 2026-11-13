"""Context-scoped inbound safety detectors and the output-side check (R1).

Pure (no DB, no I/O), beside the identity detector (``identity.py``). ONE shared entry point,
``assess(texts)``, is called by both sender planes, so a fan message is judged the same way wherever it lands.

A verdict is *scoped*, never a bare keyword hit:

* ``stop_contact``    the fan wants the conversation to end ("we are done stop talking", "stop messaging me", "block me").
* ``stop_offer``      the fan declines sales / pictures; chat may go on. Recorded, never a contact hold.
* ``stop_nickname``   the fan dislikes a nickname ("stop calling me X"). Recorded, never a hold.
* ``distress``        the fan describes harming or ending his own life. Ordinary commercial activity stops.
* ``consent``         the fan withdraws consent or voices an unsafe / uncomfortable feeling. ``ambiguous`` marks a
                      bare "stop" that may be play or may be real: the affected activity stops and a human clarifies.
* ``hardship``        spending money on ESSENTIALS is at risk (food, rent, bills, medicine, children).
* ``complaint``       a material complaint or dispute about something he paid for.
* ``identity``        the fan asks whether he is talking to a bot / asks for a human (existing detector).
* ``minor``           minor-implying roleplay (school / teen / step-family terms), a minor self-assertion or a stated
                      age under 18 (shared with the engine: ``conversation_engine.minor_signal``). Hard stop.

Deliberately NOT matched (these are the legitimate neighbours the tests pin): "stop teasing me", "stop it" in play,
"I don't want you to stop", the isolated word "rent", routine budget haggling ("out of my budget, can you do 20"),
"broke" with no essentials, physical pain from arousal, "kill me" as banter, a stop request that is only QUOTED
("you said 'stop talking' yesterday"), and a question ("we're done?").
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from conversation_engine.minor_signal import _MINOR, _STEP, is_minor  # noqa: F401  (_MINOR/_STEP: linearity test)

from .identity import denies_automation, discloses_ai, sincere_identity_question, wants_human

STOP_CONTACT = "stop_contact"
STOP_OFFER = "stop_offer"
STOP_NICKNAME = "stop_nickname"
DISTRESS = "distress"
CONSENT = "consent"
HARDSHIP = "hardship"
COMPLAINT = "complaint"
IDENTITY = "identity"
MINOR = "minor"

# Verdicts that create an operator case when the detectors are live. The rest are recorded only.
HOLD_CATEGORIES = (MINOR, IDENTITY, DISTRESS, CONSENT, STOP_CONTACT, COMPLAINT, HARDSHIP)
_PRIORITY = {c: i for i, c in enumerate(HOLD_CATEGORIES)}


@dataclass(frozen=True)
class Verdict:
    category: str
    code: str                 # fixed rule code, safe to log and store
    scope: str                # all | commercial | offer | nickname
    ambiguous: bool = False
    message_index: int = 0    # index into the texts given to assess()

    @property
    def holds(self) -> bool:
        return self.category in HOLD_CATEGORIES


def _norm(text):
    t = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t).strip()


# A phrase the fan merely quotes or reports is not a request: drop quoted spans and "you said ..." reports.
# A single-quoted span must open and close on a word boundary: the apostrophes in "i've ... i'm" are not quotes.
_QUOTED = re.compile(r'"[^"]{1,200}"|(?<!\w)\'[^\']{3,200}\'(?!\w)')
_REPORTED = re.compile(r"\b(?:you|u|she|he|they|someone|everyone|my ex|my wife|my gf)\s+(?:said|says|told me|texted|wrote)\b,?\s*(?:(?!,|\bbut\b|\band\b)[^.!?\n]){0,120}", re.I)
_I_SAID_EARLIER = re.compile(r"\b(?:when|if|every time|whenever)\s+(?:i|you|u)\s+(?:said|say|tell)\b[^.!?\n]{0,80}", re.I)


def _strip_quotes(t):
    t = _QUOTED.sub(" ", t)
    t = _REPORTED.sub(" ", t)
    return _I_SAID_EARLIER.sub(" ", t)


_LAUGH = re.compile(r"(?:\bha(?:ha)+h?\b|\blol+\b|\blmao\b|\bhehe\b|😂|🤣|😜|😉|😈|😏|😘)", re.I)
# "don't stop", "never stop", "don't want you to stop", "til u beg me to stop": play, the opposite of a stop.
_NEGATED_STOP = re.compile(r"\b(?:do(?:n'?t| not)|never|won'?t|can'?t|cant|dont)\s+(?:ever\s+)?(?:want\s+(?:you\s+|u\s+)?to\s+)?stop\b"
                           r"|\b(?:beg(?:ging)?|make|makes)\s+(?:me|you|u)\s+(?:to\s+)?stop\b|\bwant\s+(?:you|u)\s+to\s+stop\b.{0,20}\b(?:don'?t|not)\b", re.I)

_STOP_CONTACT = (
    re.compile(r"\bstop\s+(?:talking|texting|messaging|replying|contacting|writing|dming|responding|chatting)(?:\s+(?:to|with)\s+me)?\b", re.I),
    re.compile(r"\b(?:quit|cease)\s+(?:talking|texting|messaging|replying|contacting|dming)\b", re.I),
    re.compile(r"\b(?:do\s*n'?t|do not|dont|never)\s+(?:ever\s+)?(?:text|message|msg|contact|dm|write to|talk to|reply to)\s+me\b", re.I),
    re.compile(r"\bleave\s+me\s+(?:the\s+fuck\s+)?alone\b", re.I),
    re.compile(r"\bwe(?:'re| are| r)\s+(?:done|finished|through)\b", re.I),
    re.compile(r"\b(?:i(?:'m| am|m)|im)\s+(?:done|finished|through)\s+(?:talking|texting|with\s+(?:you|this|u))\b", re.I),
    re.compile(r"\bi(?:'m| am|m)\s+(?:blocking|gonna block|going to block|about to block)\s+(?:you|u)\b|\bi(?:'ll| will)\s+block\s+(?:you|u)\b", re.I),
    re.compile(r"\b(?:why\s+don'?t|how\s+about|just|go)\s+(?:you\s+|u\s+)?block\s+me\b|\b(?:still\s+)?haven'?t\s+blocked\s+me\b|\bblock\s+me\b", re.I),
    re.compile(r"\bunsubscribe\b|\bremove\s+me\s+from\b|\btake\s+me\s+off\b", re.I),
)
# A bare "stop" ("So yeah stop", "ok stop", "please stop") is only a contact stop with company; alone it is ambiguous.
_BARE_STOP = re.compile(r"^\W*(?:(?:ok(?:ay)?|so|yeah|yes|well|no|just|please|pls|plz|i said|i meant)\W+)*stop\W*$", re.I)
_BLOCK_SIGNAL = re.compile(r"\b(?:block(?:ed)?\s+me|fuck\s+off|piss\s+off|go\s+away|leave\s+me|we(?:'re| are)\s+done|i(?:'m| am)\s+done|done\s+with\s+(?:you|this|u))\b", re.I)
# "we are done for the night, goodnight" is a friendly sign-off, not a request to stop.
_FRIENDLY_END = re.compile(r"\b(?:for\s+(?:the\s+)?(?:night|today|now|tonight|this\s+week)|good\s*night|gn\b|talk\s+(?:later|tomorrow|soon)|ttyl|see\s+(?:you|u)|sweet\s+dreams)\b", re.I)
_QUESTION_DONE = re.compile(r"\b(?:we(?:'re| are)|are\s+we)\s+done\s*\?|\bdone\s*\?\s*$", re.I)

_STOP_OFFER = (
    re.compile(r"\b(?:stop|quit)\s+(?:sending|trying to sell|pitching|spamming)\b[^.!?]{0,40}\b(?:offers?|ppv|promos?|ads?|spam|locked|links?|pics?|pictures?|videos?|content|prices?|vids?)\b", re.I),
    re.compile(r"\bno\s+more\s+(?:offers?|ppv|promos?|pics?|pictures?|videos?|locked|spam|sales)\b", re.I),
    re.compile(r"\b(?:do\s*n'?t|dont)\s+(?:send|offer)\s+me\s+(?:any\s+more\s+|anymore\s+)?(?:offers?|ppv|promos?|pics?|pictures?|videos?|content|links?)\b", re.I),
    re.compile(r"\bi\s+(?:do\s*n'?t|dont)\s+want\s+(?:to\s+buy|any\s+(?:more\s+)?(?:offers?|ppv|content|pics?|videos?))\b", re.I),
)
_STOP_NICKNAME = re.compile(r"\b(?:stop|quit)\s+calling\s+me\b|\b(?:do\s*n'?t|dont)\s+call\s+me\b|\bmy\s+name\s+is\s+not\b|\bcall\s+me\s+by\s+my\s+name\b", re.I)

_DISTRESS = (
    re.compile(r"\b(?:kill(?:ing)?|hurt(?:ing)?|harm(?:ing)?|cut(?:ting)?|hang(?:ing)?|drown(?:ing)?|shoot(?:ing)?|starv(?:e|ing)|poison(?:ing)?|overdos(?:e|ing)\s+on)\s+(?:my\s*self|myself)\b", re.I),
    re.compile(r"\b(?:end(?:ing)?\s+(?:my\s+own\s+life|my\s+life|it\s+all)|tak(?:e|ing)\s+my\s+(?:own\s+)?life)\b", re.I),
    re.compile(r"\bsuicid(?:e|al)\b|\bself[\s-]?harm\b", re.I),
    re.compile(r"\b(?:want|wanna|wish|trying|tryna)\s+(?:to\s+)?(?:die|be\s+dead|disappear\s+forever)\b|\bwish\s+i\s+(?:was|were)\s+dead\b|\bbetter\s+off\s+dead\b", re.I),
    re.compile(r"\b(?:no\s+reason\s+to\s+live|not\s+worth\s+living|don'?t\s+want\s+to\s+live|dont\s+want\s+to\s+live)\b", re.I),
    re.compile(r"\b(?:nobody|no\s*one)\s+would\s+(?:even\s+)?(?:miss|care\s+if)\s+(?:me|i)\b", re.I),
    re.compile(r"\b(?:trying|going|gonna|about)\s+to\s+(?:jump|hang|overdose|od)\b", re.I),
    re.compile(r"\b(?:do\s*n'?t|dont|do not)\s+(?:want|wanna)\s+(?:to\s+)?be\s+(?:here|alive|around)\s+(?:any\s*more|anymore|any\s+longer)\b", re.I),
    re.compile(r"\b(?:do\s*n'?t|dont|can'?t|cant|do not)\s+see\s+(?:the|a|any)\s+point\s+(?:in|of)\s+(?:living|life|being\s+alive|going\s+on)\b|\bno\s+point\s+(?:in\s+)?(?:living|being\s+alive)\b", re.I),
    re.compile(r"\b(?:took|taken|swallowed|downed)\s+(?:a\s+lot\s+of|lots\s+of|a\s+bunch\s+of|a\s+handful\s+of|too\s+many|a\s+(?:whole\s+)?bottle\s+of|all\s+(?:of\s+)?(?:my|the))\s+(?:pills|tablets|meds|medication|sleeping\s+pills|painkillers)\b", re.I),
    re.compile(r"\b(?:i\s+(?:think\s+)?(?:i\s+)?|i'?ve\s+|i\s+have\s+|i\s+just\s+)(?:overdosed|od'?d|od'?ed)\b", re.I),
    re.compile(r"\b(?:can'?t|cant|cannot)\s+go\s+on\s+(?:any\s*more|anymore|any\s+longer|like\s+this)\b", re.I),
)
_CONSENT_STRONG = (
    re.compile(r"\b(?:i\s+)?(?:do\s*n'?t|dont|did\s*n'?t|didn'?t|did not|do not|never)\s+consent(?:ed)?\b|\bwithdraw(?:ing)?\s+(?:my\s+)?consent\b|\bwithout\s+my\s+consent\b", re.I),
    re.compile(r"\b(?:i(?:'m| am|m)|im|this\s+is|that\s+is|that'?s|it'?s)\s+(?:(?:actually|honestly|just|really|very|so|feeling|kinda|getting)\s+){0,2}(?:uncomfortable|not\s+comfortable|not\s+ok(?:ay)?\s+with)\b|\bmaking\s+me\s+(?:feel\s+)?(?:uncomfortable|unsafe|scared)\b", re.I),
    re.compile(r"\byou(?:'re| are)\s+(?:scaring|pressuring|forcing)\s+me\b|\bi\s+feel\s+(?:pressured|forced|unsafe|scared|used)\b", re.I),
    re.compile(r"\bi\s+(?:do\s*n'?t|dont)\s+want\s+(?:to\s+do\s+)?this(?!\s+(?:to\s+)?stop)(?:\s+(?:any\s*more|anymore))?\b", re.I),
)
_SAFEWORD_FIRST_PERSON = re.compile(r"\b(?:i(?:'m| am)?\s+(?:use|using|say|saying|call|calling|invoke)\w*\s+(?:my\s+)?safe\s*word|^\W*safe\s*word\W*$)", re.I)
_AMBIGUOUS_STOP = re.compile(r"\bi\s+said\s+(?:no|stop)\b|\bno\s*[,.!]*\s*stop\b|\bplease\s+(?:just\s+)?stop\b|\bstop\s*[,.!]*\s*(?:please|pls|plz)\b", re.I)

# Essentials: money that pays for living. "rent" ALONE is deliberately not here.
_ESSENTIAL = (r"(?:rent|mortgage|bills?|groceries|grocery|food|meals?|feeding\s+(?:my\s+)?(?:kids?|children|family|baby)|feed\s+(?:my\s+)?(?:kids?|children|family|baby)|"
              r"formula|diapers?|nappies|medic(?:ine|ation)s?|prescriptions?|insulin|electric(?:ity)?|power\s+bill|utilities|heating|gas\s+bill|school\s+fees|childcare|child\s+care|"
              r"(?:my\s+)?(?:kids?|children|family)(?:'s)?\s+(?:food|dinner|lunch|meals?))")
_HARDSHIP_SIGNAL = (r"(?:instead\s+of|rather\s+than|in\s+place\s+of|behind\s+on|late\s+on|overdue|skip(?:ping)?|going\s+without|"
                    r"choose\s+between|have\s+to\s+choose|had\s+to\s+choose|eviction|evicted|kicked\s+out|cut\s+off|shut\s+off|"
                    r"(?:can'?t|cant|unable\s+to)\s+pay|spend(?:ing)?\s+(?:my|the)\s+(?:rent|food|grocery|bill|medic)\w*)")
_HARDSHIP = (
    re.compile(rf"\b{_HARDSHIP_SIGNAL}\b[^.!?\n]{{0,60}}\b{_ESSENTIAL}\b", re.I),
    re.compile(rf"\b{_ESSENTIAL}\b[^.!?\n]{{0,60}}\b{_HARDSHIP_SIGNAL}\b", re.I),
    # "can't afford" counts only when the thing he cannot afford IS an essential, not the price he is haggling over.
    re.compile(rf"\b(?:can'?t|cant)\s+afford\s+(?:to\s+(?:pay|buy|eat|feed)\s+)?(?:my\s+|the\s+|our\s+)?{_ESSENTIAL}\b", re.I),
    re.compile(r"\b(?:homeless|can'?t\s+eat|cant\s+eat|no\s+food|going\s+hungry|go\s+hungry|starving|can'?t\s+feed|cant\s+feed)\b", re.I),
    re.compile(r"\b(?:my|the)\s+(?:weekly\s+|monthly\s+|whole\s+)?(?:wages|pay(?:check)?|salary|paycheck|savings)\b[^.!?\n]{0,40}\b(?:instead\s+of|rather\s+than)\b", re.I),
)

# Minor / age: ONE shared signal with the engine (conversation_engine.minor_signal, G05-PR2), matched on the full text
# (before quote stripping); explicit disclosures are never exempt.
_minor = is_minor

_COMPLAINT = (
    re.compile(r"\b(?:refund|charge\s*back|chargeback|scam(?:med|mer|ming)?|rip(?:ped)?[\s-]?off|money\s+back|report(?:ed|ing)?\s+you|dispute|disputing|fraud)\b", re.I),
    re.compile(r"\b(?:i\s+)?(?:paid|bought|purchased|unlocked)\b[^.!?\n]{0,60}\b(?:didn'?t\s+(?:see|get|receive|open|work|show|load|include)|did\s+not\s+(?:get|receive|open|show)|never\s+(?:got|received|came|arrived|opened|showed|saw|loaded)|nothing\s+(?:in|there|inside|came|showed)|not\s+what|wasn'?t\s+(?:what|as|there)|empty|blank|blurry|can'?t\s+(?:see|open|play)|cant\s+(?:see|open|play)|only\s+\d+|no\s+(?:face|video|pic))\b", re.I),
    re.compile(r"\$\s?\d+[^.!?\n]{0,60}\bdidn'?t\s+(?:see|get|receive|show)\b", re.I),
    re.compile(r"\bdidn'?t\s+(?:get|receive)\s+(?:what\s+i\s+paid|anything|the\s+(?:video|pic|photo|content|set|bundle))\b", re.I),
    re.compile(r"\byou\s+owe\s+me\s+(?:\d+|another|more|the\s+rest|a\s+refund)\b|\b(?:did|done)\s+me\s+dirty\b|\b(?:cheated|screwed|ripped)\s+me\b", re.I),
)


def _first(rules, text):
    for rule in rules:
        if rule.search(text):
            return True
    return False


def _classify_one(raw, index):
    """All verdicts for one message in isolation (bundle context is added by ``assess``)."""
    out = []
    full = _norm(raw)
    if not full:
        return out
    text = _strip_quotes(full)
    if _minor(raw):
        out.append(Verdict(MINOR, "minor-theme", "all", message_index=index))
    if sincere_identity_question(text):
        out.append(Verdict(IDENTITY, "identity-question", "all", message_index=index))
    # Distress and consent are NEVER downgraded by laughter, by roleplay, or by quoting context: a statement about
    # ending one's own life is a hold whatever came before it.
    if _first(_DISTRESS, text):
        out.append(Verdict(DISTRESS, "distress-phrase", "all", message_index=index))
    if _first(_CONSENT_STRONG, text) or _SAFEWORD_FIRST_PERSON.search(text):
        out.append(Verdict(CONSENT, "consent-withdrawn", "all", message_index=index))
    elif _AMBIGUOUS_STOP.search(text) and not _NEGATED_STOP.search(text) and not _LAUGH.search(text):
        out.append(Verdict(CONSENT, "consent-ambiguous-stop", "all", ambiguous=True, message_index=index))
    contact_text = _NEGATED_STOP.sub(" ", text)
    if (_first(_STOP_CONTACT, contact_text) and not _QUESTION_DONE.search(contact_text)
            and not (_FRIENDLY_END.search(contact_text) and not re.search(r"stop|block|alone|leave|unsub|contact|message|text", contact_text, re.I))):
        out.append(Verdict(STOP_CONTACT, "stop-contact-phrase", "all", message_index=index))
    if _first(_STOP_OFFER, text):
        out.append(Verdict(STOP_OFFER, "stop-offer-phrase", "offer", message_index=index))
    if _STOP_NICKNAME.search(text):
        out.append(Verdict(STOP_NICKNAME, "stop-nickname-phrase", "nickname", message_index=index))
    if _first(_COMPLAINT, text):
        out.append(Verdict(COMPLAINT, "complaint-phrase", "commercial", message_index=index))
    if _first(_HARDSHIP, text):
        out.append(Verdict(HARDSHIP, "hardship-essentials", "commercial", message_index=index))
    return out


def assess(texts) -> list[Verdict]:
    """Verdicts for a short run of consecutive fan messages (oldest first), strongest first.

    The bundle matters for exactly one case: a bare "stop" next to a clear exit signal ("So yeah stop" together with
    "still haven't blocked me") is a stop-contact request, while a bare "stop" alone is only an ambiguous consent
    concern (it may be play) and stops the affected activity pending clarification.
    """
    texts = [t for t in texts if isinstance(t, str)]
    verdicts = []
    for i, text in enumerate(texts):
        verdicts.extend(_classify_one(text, i))
    normed = [_strip_quotes(_norm(t)) for t in texts]
    signal = any(_BLOCK_SIGNAL.search(t) for t in normed)
    for i, t in enumerate(normed):
        if _BARE_STOP.match(t) and not _LAUGH.search(t):
            if signal:
                verdicts.append(Verdict(STOP_CONTACT, "stop-contact-bundle", "all", message_index=i))
            elif not any(v.message_index == i and v.category == CONSENT for v in verdicts):
                verdicts.append(Verdict(CONSENT, "consent-ambiguous-stop", "all", ambiguous=True, message_index=i))
    if any(v.category == STOP_CONTACT and v.code == "stop-contact-bundle" for v in verdicts):
        # The bundle explained the bare "stop": it is a contact stop, not merely an ambiguous consent concern.
        verdicts = [v for v in verdicts if not (v.category == CONSENT and v.ambiguous and v.code == "consent-ambiguous-stop"
                    and _BARE_STOP.match(normed[v.message_index]))]
    seen, unique = set(), []
    for v in verdicts:
        key = (v.category, v.message_index)
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return sorted(unique, key=lambda v: (_PRIORITY.get(v.category, 99), v.message_index))


# ── output side ────────────────────────────────────────────────────────────────────────────────────────────────────
# Generated text must not itself introduce a consent or trust violation. ``output_violation`` is NOT wired to any send
# path yet: wiring it into the outbound validator is a gate before send re-enable (G14). The rules live beside inbound.
_OUT_UNCONSCIOUS = re.compile(r"\b(?:pa+ss?ed|passed|knocked|blacked|zonked)\s*(?:the\s+f\w+\s+)?out\b|\bunconscious\b|\bwhile\s+(?:you(?:'re| are)|u(?:'re| r| are)?)\s+(?:asleep|sleeping|passed|out cold)\b|\bdrugged\b|\bin\s+(?:your|ur)\s+sleep\b|\bout\s+cold\b", re.I)
_OUT_IGNORE_STOP = re.compile(r"\b(?:keep|kept|keeps)\s+go(?:ing|in)\s+(?:anyway|regardless|even\s+(?:if|when|though))\b|\b(?:even\s+if|no\s+matter\s+(?:how\s+much|what))\s+(?:you|u)\s+(?:beg|say\s+(?:stop|no)|tap|cry|scream)\b|\bignor(?:e|ing)\s+(?:your|ur)\s+(?:no|stop|safe\s*word|safeword)\b|\b(?:won'?t|wont|not\s+gonna|never)\s+stop\s+(?:even\s+)?(?:if|when|until)\b|\bwhether\s+(?:you|u)\s+(?:like|want)\s+it\s+or\s+not\b|\b(?:you|u)\s+(?:can'?t|cant)\s+say\s+no\b", re.I)
_OUT_ESSENTIALS = re.compile(r"\b(?:(?:the\s+|your\s+|ur\s+)?(?:kids?|children|rent|bills?|food|groceries|family|mortgage|meds|medicine)\s+(?:can|could|will|gonna)\s+wait)\b|\b(?:forget|skip|screw|ditch)\s+(?:about\s+)?(?:the\s+|your\s+|ur\s+)?(?:rent|bills?|groceries|food|meals?|mortgage|medic\w*)\b|\b(?:spend|use|blow)\s+(?:your|ur)\s+(?:rent|grocery|food|bill|mortgage|medic)\w*\b|\binstead\s+of\s+(?:paying|feeding|buying\s+food)\b", re.I)


def output_violation(text) -> str | None:
    """A fixed reason code when generated text introduces a consent or trust violation, else None.

    ``minor-theme``          minor-implying roleplay or a stated age under 18 (same rules as inbound);
    ``unconscious-partner``  depicts a passed-out / drugged / sleeping partner;
    ``ignores-stop``         continues past a stop, a safeword or begging;
    ``endorses-essentials``  tells a fan that rent, food, bills, medicine or his children can wait for spending;
    ``denies-automation``    claims to be a real person or the creator typing (trust);
    ``discloses-ai``         says it is an AI / bot (the existing identity rule, one check for the validator).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    t = _norm(text)
    if _minor(text):
        return "minor-theme"
    if _OUT_UNCONSCIOUS.search(t):
        return "unconscious-partner"
    if _OUT_IGNORE_STOP.search(t):
        return "ignores-stop"
    if _OUT_ESSENTIALS.search(t):
        return "endorses-essentials"
    if denies_automation(t):
        return "denies-automation"
    if discloses_ai(t):
        return "discloses-ai"
    return None


__all__ = ["Verdict", "assess", "output_violation", "HOLD_CATEGORIES", "STOP_CONTACT", "STOP_OFFER", "STOP_NICKNAME",
           "DISTRESS", "CONSENT", "MINOR", "HARDSHIP", "COMPLAINT", "IDENTITY", "wants_human"]
