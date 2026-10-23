"""The one place where Twin's account id and OnlyFans' account id are related.

Twin's tables key an OnlyFans account as ``of-<owner id>`` (for example
``of-176774528``); FASTT's provider side (session, history, receipts) speaks the
bare decimal owner id (``176774528``). Live data is never renamed. The pilot
process holds one ``AccountScope`` and every call site picks the side it needs:

* ``db_id``       - what Twin's tables, FASTT's ``ai_chatter`` queries, the pilot
                    ledger and the model/budget scope use.
* ``provider_id`` - what the OnlyFans client, its history and its created-message
                    receipts use.

A bare decimal id (the legacy/SQLite fixture form) maps to itself on both sides,
so nothing changes for it. Anything that is not exactly one of the two forms is
refused; there is no fuzzy matching.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .ledger import Refused

_TWIN = re.compile(r"of-([1-9][0-9]{0,15})")
_BARE = re.compile(r"[1-9][0-9]{0,15}")


@dataclass(frozen=True, slots=True)
class AccountScope:
    db_id: str
    provider_id: str

    @classmethod
    def parse(cls, value) -> "AccountScope":
        if type(value) is not str or not value.isascii():
            raise Refused("invalid-account")
        match = _TWIN.fullmatch(value)
        if match:
            return cls(db_id=value, provider_id=match[1])
        if _BARE.fullmatch(value):
            return cls(db_id=value, provider_id=value)
        raise Refused("invalid-account")

    @classmethod
    def valid(cls, value) -> bool:
        try:
            cls.parse(value)
        except Refused:
            return False
        return True

    @property
    def twin_form(self) -> bool:
        return self.db_id != self.provider_id

    def matches(self, value) -> bool:
        """True when ``value`` names this account in either of its two forms."""
        return value in (self.db_id, self.provider_id)
