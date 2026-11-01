"""Creator persona: everything creator-specific, swappable per account. Loaded from JSON; validated strictly.

A persona never invents facts or prices: a missing price stays None and the engine refuses to quote it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class PersonaError(ValueError):
    pass


@dataclass(frozen=True)
class PriceItem:
    key: str                      # e.g. "boobs_covered"
    label: str                    # fan-facing description, e.g. "4-5 covered boob pics"
    price_cents: int | None       # None = not approved: never quoted
    vip_only: bool = False
    keywords: tuple[str, ...] = ()
    text_only: bool = False       # e.g. a written rating


@dataclass(frozen=True)
class VaultStep:
    ppv_id: str
    label: str
    price_cents: int | None


@dataclass(frozen=True)
class VaultLevel:
    name: str
    steps: tuple[VaultStep, ...]


@dataclass(frozen=True)
class Voice:
    emojis: bool = True
    pet_names: tuple[str, ...] = ("babe",)
    phrases: tuple[str, ...] = ()
    lowercase: bool = True
    elongate: bool = True          # "okayyy", "thoughhh"
    never_say: tuple[str, ...] = ()


@dataclass(frozen=True)
class Persona:
    creator_id: str
    display_name: str
    voice: Voice
    facts: dict                    # fan-facing approved facts only (key -> short text)
    content_level: str = "PG-13"
    sexting: str = "no"            # "yes" | "spenders" | "no"
    customs_min_cents: int | None = None
    customs_vip_only: bool = False
    customs_need_approval: bool = True
    rating: PriceItem | None = None
    prices: tuple[PriceItem, ...] = ()
    hard_no_keywords: tuple[str, ...] = ()
    vault: tuple[VaultLevel, ...] = ()
    vip_spend_cents: int | None = None        # spend that also qualifies a fan as VIP (optional)
    first_offer_after_turns: int = 3          # rapport turns before the first proactive vault offer
    repeat_offer_after_turns: int = 4         # creator turns between proactive offers
    sales_cooldown_turns: int = 6             # after "stop selling to me"
    quiet_labels: tuple[str, ...] = ("⛔",)   # never engage
    shift_skip_labels: dict = field(default_factory=dict)   # label -> list of "HH-HH" local hours to skip
    throttle_labels: dict = field(default_factory=dict)     # label -> minutes between replies when not spending

    def price(self, key: str) -> PriceItem | None:
        return next((p for p in self.prices if p.key == key), None)


def _cents(v, where):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise PersonaError(f"{where}: price must be a positive number of dollars or null")
    return int(round(float(v) * 100))


def _price_item(d, where) -> PriceItem:
    if not isinstance(d, dict) or not d.get("key") or not d.get("label"):
        raise PersonaError(f"{where}: needs key and label")
    return PriceItem(key=str(d["key"]), label=str(d["label"]), price_cents=_cents(d.get("price"), where),
                     vip_only=bool(d.get("vip_only", False)), text_only=bool(d.get("text_only", False)),
                     keywords=tuple(str(k).lower() for k in d.get("keywords", ())))


def persona_from_dict(d: dict) -> Persona:
    if not isinstance(d, dict):
        raise PersonaError("persona must be an object")
    for req in ("creator_id", "display_name"):
        if not d.get(req):
            raise PersonaError(f"persona missing {req}")
    v = d.get("voice") or {}
    voice = Voice(emojis=bool(v.get("emojis", True)), pet_names=tuple(v.get("pet_names") or ("babe",)),
                  phrases=tuple(v.get("phrases") or ()), lowercase=bool(v.get("lowercase", True)),
                  elongate=bool(v.get("elongate", True)),
                  never_say=tuple(str(x).lower() for x in v.get("never_say") or ()))
    vault = []
    seen_ids = set()
    for li, lvl in enumerate(d.get("vault") or ()):
        steps = []
        for si, st in enumerate(lvl.get("steps") or ()):
            pid = str(st.get("ppv_id") or "")
            if not pid or pid in seen_ids:
                raise PersonaError(f"vault level {li} step {si}: ppv_id missing or duplicated")
            seen_ids.add(pid)
            steps.append(VaultStep(pid, str(st.get("label") or pid), _cents(st.get("price"), f"vault {pid}")))
        if not steps:
            raise PersonaError(f"vault level {li} has no steps")
        vault.append(VaultLevel(str(lvl.get("name") or f"Level {li + 1}"), tuple(steps)))
    prices = tuple(_price_item(p, f"prices[{i}]") for i, p in enumerate(d.get("prices") or ()))
    if len({p.key for p in prices}) != len(prices):
        raise PersonaError("duplicate price keys")
    c = d.get("customs") or {}
    sexting = str(d.get("sexting", "no")).lower()
    if sexting not in ("yes", "spenders", "no"):
        raise PersonaError("sexting must be yes | spenders | no")
    return Persona(
        creator_id=str(d["creator_id"]), display_name=str(d["display_name"]), voice=voice,
        facts={str(k): str(val) for k, val in (d.get("facts") or {}).items()},
        content_level=str(d.get("content_level", "PG-13")), sexting=sexting,
        customs_min_cents=_cents(c.get("min_price"), "customs"), customs_vip_only=bool(c.get("vip_only", False)),
        customs_need_approval=bool(c.get("need_approval", True)),
        rating=_price_item(d["rating"], "rating") if d.get("rating") else None,
        prices=prices, hard_no_keywords=tuple(str(k).lower() for k in d.get("hard_no_keywords") or ()),
        vault=tuple(vault), vip_spend_cents=_cents(d.get("vip_spend"), "vip_spend"),
        first_offer_after_turns=int(d.get("first_offer_after_turns", 3)),
        repeat_offer_after_turns=int(d.get("repeat_offer_after_turns", 4)),
        sales_cooldown_turns=int(d.get("sales_cooldown_turns", 6)),
        quiet_labels=tuple(d.get("quiet_labels") or ("⛔",)),
        shift_skip_labels=dict(d.get("shift_skip_labels") or {}),
        throttle_labels={k: int(m) for k, m in (d.get("throttle_labels") or {}).items()},
    )


def load_persona(path: str | Path) -> Persona:
    return persona_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
