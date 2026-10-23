"""Strict, default-off settings and immutable delivery proposals."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re

from ..native_context import canonical
from ..store import Refused


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identity(key, actor):
    if (type(key) is not str or not re.fullmatch(r"[a-f0-9]{64}", key)
            or type(actor) is not str or not actor.strip() or len(actor) > 128):
        raise Refused("invalid engagement command identity")
    from onlyfans_read.agency_scope import current_scope
    scope = current_scope.get()
    if scope is not None and (not scope.can_manage or actor != scope.actor):
        raise Refused("engagement management permission required")


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    offers: bool = False
    follow_ups: bool = False
    reengagement: bool = False
    offer_delay_hours: int = 24
    follow_up_delay_hours: int = 48
    reengage_delay_hours: int = 168
    expiry_hours: int = 24
    fan_daily_limit: int = 1
    fan_weekly_limit: int = 3
    account_hourly_limit: int = 10
    account_daily_limit: int = 60
    unpaid_cooldown_hours: int = 72
    repeat_item_days: int = 30
    # Operator-authored neutral text. Never implies a purchase or sends media.
    follow_up_text: str = "Would you like to keep chatting?"
    reengage_text: str = "How have you been?"

    @classmethod
    def parse(cls, raw):
        if type(raw) is not dict or set(raw) - set(cls.__dataclass_fields__):
            raise Refused("invalid engagement settings")
        values = {**asdict(cls()), **raw}
        for name in ("enabled", "offers", "follow_ups", "reengagement"):
            if type(values[name]) is not bool:
                raise Refused("invalid engagement switch")
        limits = {"offer_delay_hours": (24, 168), "follow_up_delay_hours": (24, 168),
                  "reengage_delay_hours": (168, 2160), "expiry_hours": (1, 48),
                  "fan_daily_limit": (1, 8), "fan_weekly_limit": (1, 14),
                  "account_hourly_limit": (1, 20), "account_daily_limit": (1, 120),
                  "unpaid_cooldown_hours": (24, 720), "repeat_item_days": (7, 365)}
        for name, (lo, hi) in limits.items():
            if type(values[name]) is not int or not lo <= values[name] <= hi:
                raise Refused("invalid engagement limit")
        for name in ("follow_up_text", "reengage_text"):
            value = values[name]
            if type(value) is not str or not value.strip() or "\x00" in value or len(value.encode()) > 1000:
                raise Refused("invalid engagement text")
        if values["fan_weekly_limit"] < values["fan_daily_limit"]:
            raise Refused("weekly limit below daily limit")
        return cls(**values)

    def json(self):
        return canonical(asdict(self))


def load_object(raw):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate key")
            result[k] = v
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, TypeError) as exc:
        raise Refused("invalid engagement snapshot") from exc


def intent_key(action_id):
    return digest({"purpose": "onlyfans-engagement.v1", "action": action_id})
