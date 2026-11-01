"""Per-fan workflow state, stored as one JSON file per (creator, fan). Small, explicit, inspectable."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class FanState:
    creator_id: str
    fan_id: str
    labels: list = field(default_factory=list)          # operator labels, e.g. ["🐋"], ["⏰"]
    vip: bool = False
    vip_since: str | None = None
    spend_cents: int = 0
    purchased: list = field(default_factory=list)       # ppv ids / item keys bought
    vault_level: int = 0
    vault_step: int = 0
    open_offer: dict | None = None                      # {"kind","id","price_cents","turn"} awaiting payment
    invite: dict | None = None                          # {"kind","id"} asked "want it?" awaiting yes/no
    turns: int = 0                                      # creator replies sent
    last_offer_turn: int | None = None
    sales_paused_until_turn: int = 0
    declined: list = field(default_factory=list)        # ids the fan said no to (not re-pitched)
    boundary_pushes: int = 0
    last_reply_at: str | None = None
    recent_templates: list = field(default_factory=list)  # last template ids, to avoid repeats
    escalated: str | None = None                        # reason; engine stays silent until an operator clears it

    @classmethod
    def from_dict(cls, d: dict) -> "FanState":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class JsonStateStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, creator_id: str, fan_id: str) -> Path:
        safe = lambda s: "".join(c for c in str(s) if c.isalnum() or c in "-_")
        return self.root / safe(creator_id) / f"{safe(fan_id)}.json"

    def load(self, creator_id: str, fan_id: str) -> FanState:
        p = self._path(creator_id, fan_id)
        if p.exists():
            return FanState.from_dict(json.loads(p.read_text(encoding="utf-8")))
        return FanState(str(creator_id), str(fan_id))

    def save(self, st: FanState) -> None:
        p = self._path(st.creator_id, st.fan_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(st), f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
