"""Conservative offline extraction of explicit first-person declarations.

No model calls, guessed purchase/ownership facts or provider writes. This is
deliberately English-only and abstains on compound, ambiguous or long values.
The canonical text remains available for bounded interaction retrieval even
when no typed fact can be extracted. Changing these rules requires a new version.
"""
from dataclasses import dataclass
import hashlib
import re

from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.native_memory import FACT_FIELDS
from onlyfans_runtime.store import Refused

ALGORITHM = "explicit-en.v1"
PROFILE_FIELDS = {"short_bio": 400, "bullet_points": 600}


@dataclass(frozen=True)
class Fact:
    field: str
    value: str
    quote: str


def validate_value(field, value):
    if type(field) is not str:
        raise Refused("invalid memory field")
    limit = PROFILE_FIELDS.get(field, 40 if field == "real_name" else 80)
    if (field not in FACT_FIELDS | PROFILE_FIELDS.keys() or type(value) is not str
            or not value.strip() or value != value.strip() or len(value) > limit):
        raise Refused("invalid memory value")
    if field == "his_age" and (not value.isascii() or not value.isdigit()
                               or not 18 <= int(value) <= 120):
        raise Refused("invalid adult age")
    return value


def source_digest(message):
    return hashlib.sha256(canonical({
        "account": message.account_id, "fan": message.fan_id,
        "id": message.message_id, "body": message.body,
        "direction": message.direction, "at": message.created_at,
        "unsent": message.is_unsent,
    }).encode()).hexdigest()


PATTERNS = (
    ("real_name", r"my name is ([A-Za-z][A-Za-z '-]{0,39})"),
    ("his_age", r"i am ([0-9]{2,3}) years old"),
    ("home_city", r"i live in ([A-Za-z][A-Za-z '-]{0,79}?)(?: now)?"),
    ("home_country", r"my home country is ([A-Za-z][A-Za-z '-]{0,79})"),
    ("occupation", r"i work as (?:an? )?([A-Za-z][A-Za-z '-]{0,79})"),
    ("hobbies", r"my hobbies are ([A-Za-z][A-Za-z ,'-]{0,79})"),
    ("fetishes", r"my preferences are ([A-Za-z][A-Za-z ,'-]{0,79})"),
)


def extract(message):
    if message.is_unsent or message.direction != "in":
        return []
    facts = {}
    # Whole declarations only: quotes, hypotheticals and embedded instructions
    # cannot acquire typed authority by merely containing a matching substring.
    for sentence in re.findall(r"[^.!?\n]+[.!?\n]?", message.body or ""):
        if sentence.rstrip().endswith("?"):
            continue
        declaration = sentence.rstrip(".!\n").strip()
        declaration = re.sub(r"^actually[, ]+", "", declaration, flags=re.I)
        for field, pattern in PATTERNS:
            match = re.fullmatch(pattern, declaration, re.I)
            if not match:
                continue
            value = match.group(1).strip()
            if re.search(r"\b(and|but|not|if|unless|maybe|previously|formerly)\b", value, re.I):
                continue
            try:
                validate_value(field, value)
            except Refused:
                continue
            if declaration not in message.body:
                continue
            facts[field] = Fact(field, value, declaration)
    return list(facts.values())
