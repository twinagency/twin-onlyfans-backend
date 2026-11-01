"""Budget controls only: mocked provider responses, no model-quality evidence."""
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[2] / "scripts"), str(Path(__file__).resolve().parents[1])]
from onlyfans_eval.transports import OpenRouterTransport, SpendRefused


def transport(tmp_path, cap=.003):
    return OpenRouterTransport("synthetic-unused", cap, .044, .132, budget_state=tmp_path / "budget.json")


def generate(t, **kw):
    return t.generate([{"role": "user", "content": "hello"}], case={}, seed=1, params=kw,
                      output_json=False, last_in="hello")


def test_reserve_before_http_and_share_cap_across_routes_and_restart(tmp_path, monkeypatch):
    bodies = []
    def post(url, **kw):
        bodies.append(kw["json"])
        state = json.loads((tmp_path / "budget.json").read_text())
        assert state["pending"] and state["spent"] == "0"
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "cost": .002}})
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(SpendRefused, match="remaining"):
        generate(transport(tmp_path, cap=.002))
    assert bodies == []
    # A fresh journal for the approved cap; no call occurred in the refused one.
    (tmp_path / "budget.json").unlink(missing_ok=True)
    assert generate(transport(tmp_path)).cost_usd == .002
    with pytest.raises(SpendRefused, match="remaining"):
        generate(transport(tmp_path))
    assert len(bodies) == 1
    assert bodies[0]["max_tokens"] == 400  # repair calls pass params={}
    assert bodies[0]["model"] == "deepseek/deepseek-v4.1-flash"
    assert bodies[0]["reasoning"] == {"enabled": False}
    assert bodies[0]["provider"]["only"] == ["deepinfra/fp8", "together"]
    assert bodies[0]["provider"]["allow_fallbacks"] is False
    assert bodies[0]["provider"]["max_price"]["request"] == 0


@pytest.mark.parametrize("outcome", ["timeout", "http", "missing_cost", "invalid_cost", "over_reservation"])
def test_ambiguous_cost_remains_reserved_and_prevents_another_call(tmp_path, monkeypatch, outcome):
    calls = []
    def post(url, **kw):
        calls.append(1)
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic")
        usage = {"cost": .2 if outcome == "over_reservation" else float("nan")}
        if outcome == "missing_cost":
            usage = {}
        # Encode NaN manually: this deliberately malformed provider value must fail closed.
        return httpx.Response(500 if outcome == "http" else 200, request=httpx.Request("POST", url),
                              content=json.dumps({"usage": usage, "choices": [{"message": {"content": "x"}}]}))
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises((SpendRefused, httpx.HTTPError)):
        generate(transport(tmp_path))
    state = json.loads((tmp_path / "budget.json").read_text())
    assert state["pending"] and state["spent"] == "0"
    with pytest.raises(SpendRefused, match="unresolved"):
        generate(transport(tmp_path))
    assert len(calls) == 1


def test_inflight_reservation_lock_and_cap_change_fail_closed(tmp_path):
    first = transport(tmp_path)
    first.budget.reserve(.001)
    with pytest.raises(SpendRefused, match="unresolved"):
        generate(transport(tmp_path))
    with pytest.raises(SpendRefused, match="differ"):
        generate(transport(tmp_path, cap=2))
    (tmp_path / "budget.json.lock").touch()
    with pytest.raises(SpendRefused, match="locked"):
        generate(first)


@pytest.mark.parametrize("params", [{"max_tokens": 401}, {"max_tokens": 0}, {"max_tokens": True}, {"model": "other"}])
def test_output_and_route_cannot_escape_reservation(tmp_path, params):
    with pytest.raises(SpendRefused, match="sampling"):
        generate(transport(tmp_path), **params)
    assert not (tmp_path / "budget.json").exists()
