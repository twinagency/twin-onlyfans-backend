"""Shared creator-fact field contract and parser; no model or automation imports."""
from jsonsafe import load_dict


PERSONA_FACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "Age"),                                  # [173]
    ("job", "Work"),                                 # [81]
    ("home_city", "Lives in"),                       # [92]
    ("relationship", "Relationship"),                # [325] the most-asked of all
    ("kids", "Kids"),                                # [163] binary — catastrophic to flip
    ("family", "Family"),                            # [82]
    # Fans read tattoos off her photos — body art is VISIBLE EVIDENCE, so there
    # is a picture to check her answer against. Operator-only, like the two
    # below. [179]
    ("tattoos", "Tattoos"),
    # ── Operator-only (see PERSONA_FACTS_OPERATOR_ONLY) ──
    ("kinks", "What she's into"),                    # [65] and it saturates the sample
    ("limits", "What she won't do"),                 # fans don't ask, they PUSH
)


PERSONA_FACTS_OPERATOR_ONLY: frozenset[str] = frozenset({"kinks", "limits", "tattoos"})


def _parse_persona_facts(raw) -> dict:
    """`persona_facts_json` → dict, keys narrowed to the known field contract.

    `load_dict` owns the defensive half (NULL / already-parsed / corrupt / a
    stored list all collapse to {}), so this only has to say what is SPECIFIC to
    persona facts: unknown keys are dropped, because the block that renders them
    reads labels from PERSONA_FACT_FIELDS and a stray key would render unlabelled."""
    return {k: v for k, v in load_dict(raw).items() if k in dict(PERSONA_FACT_FIELDS)}
