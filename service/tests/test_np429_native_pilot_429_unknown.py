"""NP429 (D429): a 429 answering a FASTT-native message POST is held unknown + cooldown, never resent."""
import asyncio
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_native_pilot_postsale import HttpError


class Client(SimpleNamespace):
    def post_json(self, url, **kw): self.posts.append(kw); raise HttpError(429, {"retry-after": "7"})
    def send_text_once_body(self, fan, text): self.posts.append(text); raise HttpError(429, {"retry-after": "7"})
    def vault_media_by_id(self, media_id): return {"id": media_id, "type": "photo", "canView": True}


async def history(fan, **kw): return []
CONTENT = SimpleNamespace(catalog_ids=None, check_catalog=lambda *a: None, check_send=lambda c, a, t: t, evidence=lambda r, t: {})
TERMS = {"media_ids": [2001], "media_types": {"2001": "photo"}, "preview_ids": [2001], "price_cents": 900}


@pytest.mark.parametrize("terms", [None, TERMS], ids=["text", "card"])
def test_post_wire_429_is_unknown_and_never_resent(tmp_path, terms):
    with patch.dict(os.environ, {"TWIN_BRIDGE_ENABLED": "0", "TWIN_BRIDGE_SEND_ENABLED": "0", "PYTHON_DOTENV_DISABLED": "1"}):
        from native_pilot import runner
        from native_pilot.ledger import Refused
        from twin_bridge import coordination
        import test_native_pilot_ledger as L
        current = datetime.utcnow().replace(microsecond=0)

        async def scenario():
            f = L.Fixture(tmp_path, "np429")
            L.NOW = current - timedelta(seconds=20)
            f.args.update(now=L.NOW, cutoff=L.NOW, deadline=L.NOW + timedelta(minutes=30))
            f.call = dict(pilot_id="synthetic-test", owner_token=L.OWNER, now=L.NOW + timedelta(seconds=10))
            os.environ["DATABASE_URL"] = L.database_url(f.path)
            p = f.activate()
            created = (L.NOW + timedelta(seconds=1)).isoformat(sep=" ")
            f.source(100, created_at=created)
            coordination.register_owner(L.ACCT, L.FAN, p.owner_token, current + timedelta(minutes=29))
            g = runner.Gateway.__new__(runner.Gateway)
            g.__dict__.update(ledger=f.ledger, pilot=p, client=Client(posts=[]), loop=asyncio.get_running_loop(),
                              content=CONTENT if terms else None, scope=runner.AccountScope.parse(p.account_id),
                              reader=SimpleNamespace(preflight_history=history), followup=False, bubble=0,
                              input_key=f.ledger.claim_input(**f.call, snapshot=f.ledger.snapshot(**f.call)),
                              sources={100: {"created_at": created}}, stopped_reason=None, deadline=p.deadline,
                              hold_check=None, not_sent=None, throttled=None, expected_unsend=None, unsend_id=None,
                              unsent_ids=set(), contextual=None)
            for _ in range(2):
                with pytest.raises(Refused, match="delivery-unknown"):
                    await g._send("neutral reply", terms)
            assert len(g.client.posts) == 1 and g.not_sent is None and g.throttled.retry_after == 7.0
            status = f.ledger.status(p.pilot_id)
            assert status["attempts"] == {"delivery_unknown": 1} and status["state"] == "stopped"
            assert f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == "attention"

        with patch.object(runner, "now", lambda: current), patch.object(runner, "emit", lambda e: None), \
                patch.object(runner, "check_provider_history", lambda *a, **k: None), \
                patch.object(runner, "check_local", lambda *a, **k: {}):
            asyncio.run(scenario())
