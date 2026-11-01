"""R7: per-message promotional-intent classification. Pure: no DB, no I/O, no model.

This extends the one existing promo filter (``_common.load_promo_spam_ids``,
fan level and deliberately conservative) with a classification of the SPECIFIC
incoming message plus its conversation context. ``_common`` re-exports the marker
list from here, so there is a single list and no second authority.

No single signal is sufficient. A creator account, a link, a discount question,
a repeated message or an attachment each only ever contributes; a confirmed
promotional label needs at least two different signal kinds, at least one of
which is content or behaviour (never account status alone). Anything doubtful is
AMBIGUOUS, which stays eligible for a customer reply. A fan who has paid is never
auto-classified as promotional. Nothing here deletes or rewrites a record: the
label is recomputed from the raw message, so it is reviewable and reversible.

Labels
  customer   an ordinary customer message (default)
  ambiguous  some promotional signal but not enough, or mixed with customer intent
  business   a legitimate business/collaboration enquiry (not spam, not buying intent)
  promo      confirmed promotional traffic

``promo`` and ``business`` get no automated customer-flirt reply and no unsolicited
rating, and write no personal memory; ``business`` additionally needs a person.
No outreach line is ever invented here.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

CUSTOMER, AMBIGUOUS, BUSINESS, PROMO = "customer", "ambiguous", "business", "promo"
LABELS = (CUSTOMER, AMBIGUOUS, BUSINESS, PROMO)

# Reporting cohorts (R7 item 3).
COHORT_KNOWN_TEST, COHORT_SUSPECTED_TEST = "known_test", "suspected_test"
COHORT_PROMO_BUSINESS, COHORT_AMBIGUOUS, COHORT_GENUINE = "promo_business", "ambiguous", "genuine"
COHORTS = (COHORT_KNOWN_TEST, COHORT_SUSPECTED_TEST, COHORT_PROMO_BUSINESS, COHORT_AMBIGUOUS, COHORT_GENUINE)

# What the rich-text mass-message editor leaves on the wire (the original list).
EDITOR_MARKERS = ("m-editor-", "<strong>", "<em>")
LEGACY_PROMO_MARKERS = ("m-editor-", "onlyfans.com", "<strong>", "<em>")

_AD_TAG = tuple(re.compile(p, re.I) for p in (
    r"#\s*ads?\b", r"\bsfs\b", r"\bs4s\b", r"\bshout\s*-?\s*out\s+for\s+shout\s*-?\s*out\b",
    r"\bfriend\s*-?\s*list\b", r"\bfree\s+(?:page|trial|sub(?:scription)?)\b",
    r"\blink\s+in\s+(?:my\s+)?bio\b", r"\bsubscribe\s+to\s+my\b", r"\bcheck\s+out\s+my\s+(?:page|profile|of|onlyfans)\b",
    r"\bpromo(?:tion)?\s*(?:code|page)?\b", r"\bdm\s+me\s+for\s+(?:promo|collab|sfs)\b",
    r"\bgo\s+show\s+(?:her|him|me)\s+some\s+love\b"))
_FOLLOWER_CLAIM = re.compile(r"\b\d+(?:[.,]\d+)?\s*k\s+(?:fans|followers|subs|subscribers)\b|\bi\s+have\s+\d[\d.,]*\s*(?:k\s+)?(?:fans|followers|subs)\b", re.I)
_LINK = tuple(re.compile(p, re.I) for p in (
    r"https?://", r"\bwww\.\S+", r"\bonlyfans\.com/\S+", r"\bt\.me/\S+", r"\blinktr\.ee\S*",
    r"\bfansly\.com\S*", r"\b(?:bit\.ly|tinyurl\.com)/\S+"))
_BUSINESS = re.compile(
    r"\b(?:agency|management\s+(?:company|team)|manage\s+(?:you|your\s+(?:page|account))|managing\s+creators|collab(?:oration|orate)?|"
    r"brand\s+deal|partnership|sponsor(?:ship)?|photographer|videographer|booking|business\s+(?:enquiry|inquiry|proposal))\b", re.I)
# Buying / content / service interest. Presence turns a promo-looking message into
# AMBIGUOUS: the customer-intent half is never discarded with the promo half.
_CUSTOMER_INTENT = re.compile(
    r"\b(?:preview|how\s+much|price|prices|cost|custom|customs|buy|purchase|unlock|send\s+me|do\s+you\s+(?:have|do|sell|make)|"
    r"got\s+any|tapes?|videos?|vids?|pics?|photos?|nudes?|content|tip|pay|paying|cheaper|discount|bundle|"
    r"can\s+i\s+(?:get|see|have|buy)|i\s+(?:want|wanna|would\s+like)|i'?d\s+like)\b", re.I)


def normalize_text(text) -> str:
    """Stable key for 'the same message': lowercase, tags and links reduced, whitespace collapsed."""
    value = re.sub(r"<[^>]+>", " ", text or "")
    value = re.sub(r"https?://\S+", "<link>", value, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"[^\w<>#@]+", " ", value.lower())).strip()


@dataclass(frozen=True)
class IntentContext:
    """Facts about the conversation, supplied by the caller (no I/O here)."""
    sender_is_creator: bool = False       # OnlyFans relationship flag only; never sufficient alone
    lifetime_spend_cents: int = 0
    prior_purchases: int = 0
    our_replies: int = 0                  # informational: a reply from us never converts a promo into a customer
    same_text_other_fans: int = 0         # distinct OTHER fans who sent the same normalised text in the window
    same_text_self_count: int = 1         # how many times THIS fan sent it (including this message)
    equals_our_recent_outbound: bool = False   # the text equals a line our own account recently sent (mass message echo)
    attachment_kind: str | None = None    # photo/video/audio/gif/None; an attachment is never a promo signal on its own
    known_test: bool = False

    @classmethod
    def coerce(cls, value) -> "IntentContext":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        names = cls.__dataclass_fields__
        return cls(**{k: v for k, v in dict(value).items() if k in names})

    @property
    def is_buyer(self) -> bool:
        return int(self.lifetime_spend_cents or 0) > 0 or int(self.prior_purchases or 0) > 0


@dataclass(frozen=True)
class IntentResult:
    label: str
    signals: tuple = ()
    points: int = 0
    reason: str = ""
    customer_intent: bool = False

    @property
    def suppress_reply(self) -> bool:
        """No automated customer-flirt reply (promo and business only; ambiguous stays eligible)."""
        return self.label in (PROMO, BUSINESS)

    @property
    def counts_as_customer(self) -> bool:
        return self.label in (CUSTOMER, AMBIGUOUS)

    @property
    def allows_memory_write(self) -> bool:
        return self.label in (CUSTOMER, AMBIGUOUS)

    @property
    def allows_rating(self) -> bool:
        return self.label == CUSTOMER

    def as_json(self) -> dict:
        return {"label": self.label, "signals": list(self.signals), "points": self.points, "reason": self.reason}


_WEIGHT = {"ad_tag": 2, "editor_markup": 2, "cross_fan_repeat": 2, "echo_our_outbound": 3,
           "follower_claim": 1, "link": 1, "self_repeat": 1, "creator_status": 1}
_CONTENT = frozenset(_WEIGHT) - {"creator_status"}


def signals_of(message, ctx: IntentContext) -> set[str]:
    text = message or ""
    found = set()
    if any(rx.search(text) for rx in _AD_TAG):
        found.add("ad_tag")
    if _FOLLOWER_CLAIM.search(text):
        found.add("follower_claim")
    if any(rx.search(text) for rx in _LINK):
        found.add("link")
    if any(marker in text for marker in EDITOR_MARKERS):
        found.add("editor_markup")
    if ctx.same_text_other_fans >= 2 and normalize_text(text):
        found.add("cross_fan_repeat")
    if ctx.same_text_self_count >= 3 and normalize_text(text):
        found.add("self_repeat")
    if ctx.equals_our_recent_outbound and normalize_text(text):
        found.add("echo_our_outbound")
    if ctx.sender_is_creator:
        found.add("creator_status")
    return found


def classify_inbound_intent(message, context=None) -> IntentResult:
    """Classify ONE incoming message in its conversation context."""
    ctx = IntentContext.coerce(context)
    text = message or ""
    found = signals_of(text, ctx)
    points = sum(_WEIGHT[s] for s in found)
    customer_intent = bool(_CUSTOMER_INTENT.search(re.sub(r"<[^>]+>", " ", text)))
    content = found & _CONTENT
    ordered = tuple(sorted(found))
    # Test accounts are cohort-separated for reporting; they are never silenced by this classifier.
    if ctx.is_buyer:
        # A spender or prior buyer is never auto-excluded, whatever the message looks like.
        return IntentResult(AMBIGUOUS if content else CUSTOMER, ordered, points,
                            "buyer_never_excluded" if content else "", customer_intent)
    business = _BUSINESS.search(text) is not None
    if business and not customer_intent and not (found & {"ad_tag", "editor_markup", "cross_fan_repeat", "echo_our_outbound"}):
        return IntentResult(BUSINESS, ordered, points, "business_enquiry", customer_intent)
    content_points = sum(_WEIGHT[x] for x in content)
    # Account status counts as a supporting kind only once content evidence already reaches the threshold.
    kinds = len(content) + (1 if "creator_status" in found and content_points >= 3 else 0)
    if content_points >= 3 and kinds >= 2 and not customer_intent:
        return IntentResult(PROMO, ordered, points, "multiple_independent_promo_signals", customer_intent)
    if content and customer_intent:
        return IntentResult(AMBIGUOUS, ordered, points, "promo_signals_mixed_with_customer_intent", customer_intent)
    if content or (found and points >= 2):
        return IntentResult(AMBIGUOUS, ordered, points, "insufficient_independent_signals", customer_intent)
    return IntentResult(CUSTOMER, ordered, points, "", customer_intent)


def burst_decision(messages, context=None) -> tuple[bool, list[IntentResult]]:
    """Whether an unanswered inbound burst may be given an automated customer reply.

    ``messages`` is a list of str or (str, context) pairs. The burst is withheld only when EVERY
    message is confirmed promo/business; one genuine or ambiguous message keeps the customer eligible,
    so promo traffic inside the same conversation never taints a genuine request (p92/p97 style).
    """
    results = []
    for item in messages:
        text, ctx = item if isinstance(item, tuple) else (item, context)
        results.append(classify_inbound_intent(text, ctx))
    withheld = bool(results) and all(r.suppress_reply for r in results)
    return (not withheld), results


# ── reporting cohorts ────────────────────────────────────────────────────
def _ids_from(value: str) -> set[int]:
    return {int(part) for part in re.split(r"[,\s]+", value or "") if part.strip().isdigit()}


def configured_id_set(env_ids: str, env_file: str) -> frozenset[int]:
    """Fan ids from a deployment-supplied list (env or a file path in env). Never from the repository."""
    found = _ids_from(os.environ.get(env_ids, ""))
    path = os.environ.get(env_file)
    if path:
        try:
            found |= _ids_from(Path(path).read_text())
        except OSError:
            pass
    return frozenset(found)


def known_test_fan_ids() -> frozenset[int]:
    return configured_id_set("ONLYFANS_KNOWN_TEST_FAN_IDS", "ONLYFANS_KNOWN_TEST_FAN_IDS_FILE")


def suspected_test_fan_ids() -> frozenset[int]:
    return configured_id_set("ONLYFANS_SUSPECTED_TEST_FAN_IDS", "ONLYFANS_SUSPECTED_TEST_FAN_IDS_FILE")


def reporting_cohort(fan_id, results, *, known_test=frozenset(), suspected_test=frozenset(), is_buyer=False) -> str:
    """Cohort for one fan from the labels of his messages (never from account status alone)."""
    fan_id = int(fan_id)
    if fan_id in known_test:
        return COHORT_KNOWN_TEST
    if fan_id in suspected_test:
        return COHORT_SUSPECTED_TEST
    labels = {r.label if isinstance(r, IntentResult) else r for r in results}
    if is_buyer:
        return COHORT_AMBIGUOUS if labels & {PROMO, BUSINESS, AMBIGUOUS} else COHORT_GENUINE
    if labels & {CUSTOMER}:
        # at least one genuine customer message: the fan stays a customer even if he also sent promo
        return COHORT_GENUINE
    if labels & {PROMO, BUSINESS} and not labels & {AMBIGUOUS}:
        return COHORT_PROMO_BUSINESS
    if labels & {AMBIGUOUS, PROMO, BUSINESS}:
        return COHORT_AMBIGUOUS
    return COHORT_GENUINE


def counts_in_customer_metrics(cohort: str) -> bool:
    """Commercial customer metrics include genuine and ambiguous traffic only."""
    return cohort in (COHORT_GENUINE, COHORT_AMBIGUOUS)
