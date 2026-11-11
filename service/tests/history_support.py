"""Shared scaffolding for the history coverage suites: a scripted provider wire and small builders.

Not a test module. Mocked wire only; the provider answers from per-route scripts and records every request.
"""
import re
from urllib.parse import parse_qsl
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_engine_real_worker_pg import answer
from test_real_transport_pg import _item
from test_commercial_preparation_pg import setup, factory, identity, page, config, TENANT, ACTOR
from conversation_engine import preparation as prep

BASE = "https://api.fansapi.com"
START = "2016-01-01 00:00:00"


def key(n):
    return format(n, "x").rjust(64, "0")


class Provider:
    """Scripted wire. Each route pops its next scripted response; the last one repeats.

    A script entry is a callable returning an httpx.Response. `calls` records (kind, fan_or_None, query dict).
    """
    def __init__(self, cfg):
        self.cfg, self.calls = cfg, []
        self.vault, self.chats, self.tx, self.cb = [], {}, [], []

    @staticmethod
    def _next(script, default):
        if not script:
            return default
        return script.pop(0) if len(script) > 1 else script[0]

    def __call__(self, req):
        path, query = req.url.path, dict(parse_qsl(req.url.query.decode() if isinstance(req.url.query, bytes) else req.url.query))
        if path == "/api/accounts":
            self.calls.append(("identity", None, {}))
            return identity(self.cfg)
        if path.endswith("/media/vault"):
            self.calls.append(("vault", None, query))
            return self._next(self.vault, lambda: page([_item(501)]))()
        found = re.search(r"/chats/(\d+)/messages$", path)
        if found:
            fan = int(found.group(1))
            self.calls.append(("chats", fan, query))
            return self._next(self.chats.get(fan, []), chat_page([]))()
        if path.endswith("/transactions"):
            self.calls.append(("transactions", None, query))
            return self._next(self.tx, tx_page([]))()
        if path.endswith("/chargebacks"):
            self.calls.append(("chargebacks", None, query))
            return self._next(self.cb, cb_page([]))()
        raise AssertionError("unscripted request " + path)

    def kinds(self, *names):
        return [c for c in self.calls if c[0] in names]

    def count(self, kind):
        return len([c for c in self.calls if c[0] == kind])


def chat_page(messages, next_page=None):
    return lambda: answer(200, {"data": list(messages), "_pagination": {"next_page": next_page}})


def tx_page(rows, marker=None):
    return lambda: answer(200, {"data": {"list": list(rows), "hasMore": marker is not None, "nextMarker": marker}})


def cb_page(rows):
    return lambda: answer(200, {"data": {"list": list(rows)}})


def fail(status=503, headers=None, body=None):
    return lambda: answer(status, body if body is not None else {"error": "synthetic"}, headers or {})


def next_url(cfg, fan, marker):
    return f"{BASE}/api/{cfg['provider_account']}/chats/{fan}/messages?limit=100&order=desc&last_id={marker}"


def fan_message(mid, fan, text="hello", created="2026-10-01T10:00:00Z", **extra):
    return {"id": mid, "fromUser": {"id": fan}, "text": text, "createdAt": created, "isSentByMe": False, "media": [], "price": 0, **extra}


def creator_message(mid, cfg, text="hi", created="2026-10-01T10:05:00Z", **extra):
    return {"id": mid, "fromUser": {"id": int(cfg["creator_id"])}, "text": text, "createdAt": created, "isSentByMe": True,
            "media": [], "price": 0, **extra}


def card(mid, cfg, media, price, *, previews=None, opened=None, created="2026-10-01T10:10:00Z", **extra):
    """A creator pay-per-view message. previews=None leaves the key out (unknown), [] is a fact."""
    row = creator_message(mid, cfg, text="locked", created=created, media=[{"id": m} for m in media], price=price, **extra)
    if previews is not None:
        row["previews"] = list(previews)
    if opened is not None:
        row["isOpened"] = opened
        if opened:
            row["changedAt"] = "2026-10-02T09:00:00Z"
    return row


def tx_row(txid, fan, amount=15.0, created="2026-10-02T09:00:01Z", **extra):
    return {"id": txid, "type": "message", "status": "done", "amount": amount, "currency": "USD", "createdAt": created,
            "user": {"id": fan}, **extra}


def cb_row(txid, fan, amount=15.0, created="2026-10-05T09:00:00Z"):
    return {"payment": {"id": txid, "amount": amount, "user": {"id": fan}, "createdAt": created}, "createdAt": created}


def sources(vault=(40, 80), chats=(60, 120), transactions=(20, 40), chargebacks=(20, 40), interval=0):
    def one(v):
        return {"requests": v[0], "credits": v[1], "min_interval_seconds": interval}
    return {"vault_window": one(vault), "chats": one(chats), "transactions": one(transactions), "chargebacks": one(chargebacks)}


def spec(webhooks=True, start_date=START, **kw):
    return {"contract_version": "history-import.v1", "start_date": start_date,
            "webhooks": {"reference": "agency attested webhook subscription"} if webhooks else None,
            "sources": sources(**kw)}


def prepared(database, i=1, *, requests=400, credits=800, begin=True, keep_seed_fan=False, **kw):
    """A configured disabled preparation account (+ started history import unless begin is False)."""
    from fans_api_adapter import history_import
    store, account, cfg = setup(database, i, requests=requests, credits=credits)
    if not keep_seed_fan:
        # seed() creates one local fan (101). History tests list only the fans they name.
        from sqlalchemy import text
        with database[1].begin() as c:
            for table in ("of_conversation_state", "chats", "fans"):
                c.execute(text(f"DELETE FROM {table} WHERE account_id=:a AND fan_id=101"), {"a": account})
    if begin:
        history_import.configure(store, TENANT, account, spec(**kw), expected_revision=0, command_key=key(1000 + i),
                                 actor=ACTOR, reason="Explicit history import budgets")
    return store, account, cfg


def run(store, account, provider):
    return prep.prepare_one(store, TENANT, account, client_factory=factory(provider))


def rows(database, model, account, *where):
    with Session(database[1]) as s:
        out = list(s.scalars(select(model).where(model.account_id == account, *where)))
        s.expunge_all()
        return out
