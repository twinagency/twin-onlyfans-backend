"""NP429: a provider 429 answering a FASTT-native POST is pacing evidence, never proof nothing was sent.

OnlyFans can create the message and still answer 429, so once the POST is on the wire the attempt is
``delivery_unknown`` (fan held for an operator, allowance unit kept, never resent) and the 429 only feeds
the account cooldown. Synthetic SQLite + fake client; no network.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class HttpError(Exception):
    def __init__(self, status, headers=None):
        super().__init__("http %s" % status)
        self.response = SimpleNamespace(status_code=status, headers=headers or {})


class RateLimitedClient:
    """Every POST reaches the wire and is answered 429 (the message may still exist on OnlyFans)."""

    def __init__(self):
        self.posts = []

    def post_json(self, url, **kw):
        self.posts.append(kw["json_body"])
        raise HttpError(429, {"retry-after": "7"})

    def send_text_once_body(self, fan_id, text):
        self.posts.append(text)
        raise HttpError(429, {"retry-after": "7"})

    def vault_media_by_id(self, media_id):
        return {"id": media_id, "type": "photo", "canView": True}


class Content:
    catalog_ids = None

    def check_catalog(self, con, account): pass
    def check_send(self, con, account, terms): return terms
    def evidence(self, raw, terms): return {}


class Reader:
    async def preflight_history(self, fan, **kw): return []


TERMS = {"media_ids": [2001], "media_types": {"2001": "photo"}, "preview_ids": [2001], "price_cents": 900}


@pytest.fixture()
def world(tmp_path):
    env = {"DATABASE_URL": "sqlite+aiosqlite:///" + (tmp_path / "bootstrap.db").as_posix(), "TWIN_BRIDGE_ENABLED": "0",
           "TWIN_BRIDGE_SEND_ENABLED": "0", "PYTHON_DOTENV_DISABLED": "1"}
    with patch.dict(os.environ, env):
        from native_pilot import runner
        current = datetime.utcnow().replace(microsecond=0)
        with patch.object(runner, "now", lambda: current), patch.object(runner, "emit", lambda e: None), \
                patch.object(runner, "check_provider_history", lambda *a, **k: None), \
                patch.object(runner, "check_local", lambda *a, **k: {}):
            yield SimpleNamespace(root=tmp_path, runner=runner, current=current)


def gateway(world, name, content):
    import test_native_pilot_ledger as L
    from twin_bridge import coordination
    runner, current = world.runner, world.current
    f = L.Fixture(world.root, name)
    L.NOW = current - timedelta(seconds=20)
    f.args.update(now=L.NOW, cutoff=L.NOW, deadline=L.NOW + timedelta(minutes=30))
    f.call = dict(pilot_id="synthetic-test", owner_token=L.OWNER, now=L.NOW + timedelta(seconds=10))
    os.environ["DATABASE_URL"] = L.database_url(f.path)
    p = f.activate()
    f.source(100, created_at=(L.NOW + timedelta(seconds=1)).isoformat(sep=" "))
    key = f.ledger.claim_input(**f.call, snapshot=f.ledger.snapshot(**f.call))
    coordination.register_owner(L.ACCT, L.FAN, p.owner_token, current + timedelta(minutes=29))
    client = RateLimitedClient()
    g = runner.Gateway.__new__(runner.Gateway)
    g.ledger, g.pilot, g.client, g.loop = f.ledger, p, client, asyncio.get_running_loop()
    g.content = Content() if content else None
    g.scope, g.reader = runner.AccountScope.parse(p.account_id), Reader()
    g.input_key, g.followup, g.bubble = key, False, 0
    g.sources = {100: {"created_at": (L.NOW + timedelta(seconds=1)).isoformat(sep=" ")}}
    g.stopped_reason, g.deadline, g.hold_check, g.not_sent, g.throttled = None, p.deadline, None, None, None
    g.expected_unsend, g.unsend_id, g.unsent_ids, g.contextual = None, None, set(), None
    return f, p, g, client


@pytest.mark.parametrize("terms", [None, TERMS], ids=["text", "card"])
def test_post_wire_429_is_unknown_and_never_resent(world, terms):
    from native_pilot.ledger import Refused

    async def scenario():
        f, p, g, client = gateway(world, "np429-" + ("card" if terms else "text"), content=terms is not None)
        for _ in range(2):
            with pytest.raises(Refused) as caught:
                await g._send("neutral reply", terms)
            assert caught.value.reason == "delivery-unknown"
        assert len(client.posts) == 1, "a 429 after the POST started is answered at most once"
        assert g.not_sent is None, "a post-wire 429 is never 'certainly not sent'"
        assert g.throttled is not None and g.throttled.retry_after == 7.0, "the 429 still feeds the account cooldown"
        status = f.ledger.status(p.pilot_id)
        assert status["attempts"] == {"delivery_unknown": 1} and status["state"] == "stopped"
        assert f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == "attention", "held for an operator"

    asyncio.run(scenario())
