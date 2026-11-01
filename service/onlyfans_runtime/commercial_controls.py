"""Per-creator commercial controls (R5): validated, persisted in the existing saved sales JSON, read-only helpers.

Lives beside the other off/shadow/live switches (``contextual_mode`` is the mode; these are its tuning). Three settings:

* initiative       lower | standard | active     how readily an appropriate commercial next step is taken
* first_offer_ref  creator turns before the first paid card at which the planner reassesses (reference, NOT a trigger)
* repeat_offer_ref creator turns after a purchase/offer before the next is reassessed; EXPERIMENTAL starting setting

None of these forces a send. They only weight the choice among actions the evidence already supports; relevance,
creator restrictions, stops, takeover, complaints, budgets, ownership and delivery uncertainty are checked elsewhere
and are never overridden by a setting.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict

log = logging.getLogger(__name__)

INITIATIVE_KEY, INITIATIVE_FIELD = "fastt_commercial_initiative", "fasttCommercialInitiative"
FIRST_KEY, FIRST_FIELD = "fastt_first_offer_reference", "fasttFirstOfferReference"
REPEAT_KEY, REPEAT_FIELD = "fastt_repeat_offer_reference", "fasttRepeatOfferReference"
FIELDS = (INITIATIVE_FIELD, FIRST_FIELD, REPEAT_FIELD)
INITIATIVES = ("lower", "standard", "active")
FIRST_RANGE = (1, 12)
REPEAT_RANGE = (3, 6)
DEFAULTS = {"initiative": "standard", "first_offer_ref": 3, "repeat_offer_ref": 4}
# Pooled median 3 (middle half 2-6) from the approved reference data; descriptive, not a promise.
REPEAT_LABEL = "experimental starting setting, not an observed benchmark and not a mandatory wait"
# Prepared for an approved pilot. NEVER applied by code: an operator chooses to save it through the config interface.
ACTIVE_PILOT_PROFILE = {INITIATIVE_FIELD: "active", FIRST_FIELD: 3, REPEAT_FIELD: 3}


@dataclass(frozen=True)
class Controls:
    initiative: str = "standard"
    first_offer_ref: int = 3
    repeat_offer_ref: int = 4

    def as_dict(self) -> dict:
        return asdict(self)


def _int(value, lo, hi):
    return value if type(value) is int and lo <= value <= hi else None


def controls(settings) -> Controls:
    """Saved value or the default; an invalid saved value falls back to the default for that one setting."""
    raw = settings if type(settings) is dict else {}
    out = dict(DEFAULTS)
    value = raw.get(INITIATIVE_KEY)
    if value is not None:
        if type(value) is str and value in INITIATIVES:
            out["initiative"] = value
        else:
            log.warning("invalid_commercial_initiative: using standard")
    for key, name, rng in ((FIRST_KEY, "first_offer_ref", FIRST_RANGE), (REPEAT_KEY, "repeat_offer_ref", REPEAT_RANGE)):
        value = raw.get(key)
        if value is not None:
            good = _int(value, *rng)
            if good is None:
                log.warning("invalid_%s: using default", name)
            else:
                out[name] = good
    return Controls(**out)


def validate_patch_fields(patch: dict, error) -> dict:
    """Return the validated subset of ``patch`` for these fields; ``error`` is the caller's exception class."""
    result = {}
    if INITIATIVE_FIELD in patch:
        value = patch[INITIATIVE_FIELD]
        if type(value) is not str or value not in INITIATIVES:
            raise error("Commercial initiative must be lower, standard, or active.")
        result[INITIATIVE_FIELD] = value
    for field, rng, label in ((FIRST_FIELD, FIRST_RANGE, "First-offer reference"), (REPEAT_FIELD, REPEAT_RANGE, "Repeat-offer reference")):
        if field in patch:
            value = patch[field]
            if _int(value, *rng) is None:
                raise error(f"{label} must be a whole number from {rng[0]} to {rng[1]}.")
            result[field] = value
    return result


KEY_OF = {INITIATIVE_FIELD: INITIATIVE_KEY, FIRST_FIELD: FIRST_KEY, REPEAT_FIELD: REPEAT_KEY}


def view(settings, valid=True) -> dict:
    c = controls(settings if valid else {})
    return {INITIATIVE_FIELD: c.initiative, FIRST_FIELD: c.first_offer_ref, REPEAT_FIELD: c.repeat_offer_ref,
            "fasttRepeatOfferReferenceLabel": REPEAT_LABEL}
