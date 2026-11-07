"""Example-line retrieval hook for the writer prompt (rule 2: approved creator examples guide her voice).

Interface plus a file-backed implementation. The JSONL files hold REAL approved chat lines and are operator data:
they live outside the repository (a directory chosen by deployment config) and must never be committed. One JSON
object per line: {"fan_text": "...", "reply": ["..."]} where reply is the creator's text bubbles. The directory holds
one file per account: <account_id>.jsonl. Retrieval is deterministic token overlap on the fan text; examples are
style only and never carry facts, prices or permissions, so lines with prices, contact details or sales wording are
skipped unless the turn is allowed to sell. Sexual wording is NOT filtered: explicit voice examples are valid.
"""
from collections import Counter
from dataclasses import dataclass
import json
import os
import re
from typing import Protocol

MAX_EXAMPLES = 20
MAX_REPLY_CHARS = 140
_TOKEN = re.compile(r"[a-z0-9']+")
_STOP = frozenset("a an the i you u me my your ur to of and or is are it its it's im i'm in on for so do just that this "
                  "be was what with at have has had but not no yes ok okay lol haha".split())
_ACCOUNT = re.compile(r'[A-Za-z0-9_.-]{1,128}')
_COMMERCIAL = re.compile(r"[$£€]\s*\d|\b(unlock|ppv|bundle|tip me|spoil me|credits?|stars?)\b", re.I)


@dataclass(frozen=True)
class ExampleLine:
    fan_text: str
    reply: tuple


class ExampleRetriever(Protocol):
    def retrieve(self, account_id: str, query: str, *, k: int = 12, allow_selling: bool = False,
                 exclude: frozenset = frozenset()) -> tuple: ...


class NullExamples:
    def retrieve(self, account_id, query, **_):
        return ()


def _tokens(text):
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOP}


def line_is_usable(lines, *, allow_selling):
    from .model_output import _RULES, ASSISTANT_RX, REFUSAL_RX
    text = ' '.join(lines)
    if not text.strip() or len(text) > MAX_REPLY_CHARS:
        return False
    if any(rx.search(text) for _, rx in _RULES) or REFUSAL_RX.search(text) or ASSISTANT_RX.search(text):
        return False
    return allow_selling or not _COMMERCIAL.search(text)


class JsonlExamples:
    def __init__(self, directory):
        self._dir = os.fspath(directory)
        self._cache = {}

    def _bank(self, account_id):
        if type(account_id) is not str or not _ACCOUNT.fullmatch(account_id):
            return []
        path = os.path.join(self._dir, account_id + '.jsonl')
        stamp = os.stat(path).st_mtime_ns if os.path.exists(path) else None
        cached = self._cache.get(account_id)
        if cached and cached[0] == stamp:
            return cached[1]
        rows = []
        if stamp is not None:
            with open(path, encoding='utf-8') as handle:
                for raw in handle:
                    try:
                        item = json.loads(raw)
                        fan, reply = item['fan_text'], item['reply']
                    except (ValueError, KeyError, TypeError):
                        continue
                    if type(fan) is str and type(reply) is list and reply and all(type(r) is str for r in reply):
                        rows.append(ExampleLine(fan, tuple(reply)))
        self._cache[account_id] = (stamp, rows)
        return rows

    def retrieve(self, account_id, query, *, k=12, allow_selling=False, exclude=frozenset()):
        bank = self._bank(account_id)
        if not bank or type(query) is not str:
            return ()
        k = max(1, min(MAX_EXAMPLES, k))
        wanted = _tokens(query)
        df = Counter(t for line in bank for t in _tokens(line.fan_text))
        scored = sorted(((-sum(1.0 / df[t] for t in wanted & _tokens(line.fan_text)), i) for i, line in enumerate(bank)))
        seen = {r.lower() for r in exclude}
        out = []
        for _, i in scored:
            reply = ' '.join(bank[i].reply)
            if reply.lower() in seen or not line_is_usable(bank[i].reply, allow_selling=allow_selling):
                continue
            seen.add(reply.lower()); out.append(bank[i])
            if len(out) >= k:
                break
        return tuple(out)
