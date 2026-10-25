"""Offline native pilot transport tests: no live DB, dotenv, key, or socket."""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import httpx

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

# The real result type imports db.engine. Suppress its dotenv read and point
# its unconnected engine at memory; the test never opens an application DB.
with patch.dict(os.environ, {"DATABASE_URL": "sqlite+aiosqlite:///:memory:"}), patch("dotenv.load_dotenv", return_value=False):
    from llm_client import LLMResult

from native_pilot.model import (
    ENDPOINT, MODEL, MAX_RESPONSE_BYTES, NativePilotModel, NativePilotModelError,
)

ACCOUNT = "176774528"
FAN = 318022380
SYNTHETIC_KEY = "synthetic-test-key-never-live"


def envelope(content="A neutral test response."):
    return {
        "model": MODEL,
        "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": content,
        }}],
        "usage": {"prompt_tokens": 31, "completion_tokens": 7,
                  "prompt_tokens_details": {"cached_tokens": 3}, "cost": 0.00234},
    }


class ModelTests(unittest.IsolatedAsyncioTestCase):
    def make(self, handler=None, **overrides):
        self.requests = []
        self.events = []
        self.reservations = []

        async def track(request):
            self.requests.append(request)
            if handler:
                result = handler(request)
                return await result if hasattr(result, "__await__") else result
            return httpx.Response(200, json=envelope())

        settings = {
            "api_key": SYNTHETIC_KEY, "account_id": ACCOUNT, "fan_id": FAN,
            "deadline": time.monotonic() + 60, "should_stop": lambda: False,
            "on_event": self.events.append,
            "reserve_call": lambda: self.reservations.append("reserved"),
            "transport": httpx.MockTransport(track),
        }
        settings.update(overrides)
        return NativePilotModel(**settings)

    async def call(self, adapter, **overrides):
        settings = {"model": MODEL, "messages": [{"role": "user", "content": "Synthetic test input"}],
                    "purpose": "ai_chatter", "account_id": ACCOUNT, "fan_id": FAN}
        settings.update(overrides)
        return await adapter.chat(**settings)

    async def test_database_callbacks_use_independent_workers_when_configured(self):
        import threading
        from native_pilot.local_io import run_blocking
        main_thread = threading.get_ident()
        callback_threads = []
        def stop_check():
            callback_threads.append(threading.get_ident())
            return False
        def reserve():
            callback_threads.append(threading.get_ident())
            self.reservations.append("reserved")
        adapter = self.make(should_stop=stop_check, reserve_call=reserve, blocking_call=run_blocking)
        await self.call(adapter)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.reservations, ["reserved"])
        self.assertEqual(len(callback_threads), 4)
        self.assertTrue(all(value != main_thread for value in callback_threads))

    async def test_pinned_wire_native_prompt_and_real_result(self):
        adapter = self.make()
        messages = [{"role": "system", "content": "Native synthetic instructions"},
                    {"role": "user", "content": "Synthetic test input"}]
        result = await self.call(adapter, messages=messages, temperature=0.34)
        self.assertIsInstance(result, LLMResult)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        body = json.loads(request.content)
        self.assertEqual(str(request.url), ENDPOINT)
        self.assertEqual(request.method, "POST")
        self.assertEqual(body["messages"], messages)
        self.assertEqual(body["temperature"], 0.34)
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["provider"], {"order": ["deepinfra/fp8", "together"], "allow_fallbacks": False})
        self.assertEqual(body["reasoning"], {"enabled": False})
        self.assertEqual(body["max_tokens"], 400)
        self.assertEqual(self.reservations, ["reserved"])
        self.assertEqual(result.model, MODEL)
        self.assertEqual(result.provider, "openrouter")
        self.assertEqual(result.tokens_in, 31)
        self.assertEqual(result.tokens_out, 7)
        self.assertEqual(result.cache_hit_tokens, 3)
        self.assertEqual(result.cost_cents, 23)
        self.assertEqual(result.call_id, None)
        self.assertEqual(result.parsed, None)
        self.assertEqual([e["event"] for e in self.events], ["model-start", "model-complete"])
        self.assertEqual(self.events[-1]["cost_usd"], 0.00234)
        for prohibited in (SYNTHETIC_KEY, "Synthetic test input", "neutral test response"):
            self.assertNotIn(prohibited, repr(self.events))
            self.assertNotIn(prohibited, repr(result.raw))

    async def test_resolver_purposes_get_bounded_larger_output_cap(self):
        adapter = self.make(lambda _: httpx.Response(200, json=envelope('{"verdicts":{}}')))
        for purpose, cap in (("content_resolver_verify", 1200), ("content_resolver_contract", 1200),
                             ("content_resolver_action", 400), ("content_resolver_hook", 400), ("ai_chatter", 400)):
            await self.call(adapter, purpose=purpose, response_format={"type": "json_object"})
            self.assertEqual(json.loads(self.requests[-1].content)["max_tokens"], cap, purpose)

    async def test_json_format_preserved_and_parsed(self):
        adapter = self.make(lambda _: httpx.Response(200, json=envelope('{"answer":"neutral"}')))
        result = await self.call(adapter, response_format={"type": "json_object"})
        self.assertEqual(result.parsed, {"answer": "neutral"})
        self.assertEqual(json.loads(self.requests[0].content)["response_format"], {"type": "json_object"})

    async def test_reject_bad_json_no_retry(self):
        for value in ("not json", "```json\n{}\n```", "[]", "null", '{"a":1,"a":2}', '{"a":NaN}'):
            with self.subTest(value=value):
                adapter = self.make(lambda _, value=value: httpx.Response(200, json=envelope(value)))
                with self.assertRaisesRegex(NativePilotModelError, "model-json-invalid"):
                    await self.call(adapter, response_format={"type": "json_object"})
                self.assertEqual(len(self.requests), 1)
                with self.assertRaises(NativePilotModelError):
                    await self.call(adapter)
                self.assertEqual(len(self.requests), 1)

    async def test_scope_route_and_inputs_refused_before_request(self):
        cases = [
            {"account_id": "another"}, {"account_id": 176774528},
            {"fan_id": None}, {"fan_id": "318022380"}, {"fan_id": True}, {"fan_id": 999},
            {"model": "grok-whatever"}, {"reasoning_effort": "high"},
            {"temperature": True}, {"temperature": float("nan")}, {"temperature": -1},
            {"response_format": {"type": "json_schema", "json_schema": {}}},
            {"messages": []}, {"messages": ["bad"]}, {"messages": [{"content": "\ud800"}]},
            {"messages": [{"content": float("nan")}]}, {"purpose": ""},
            {"messages": [{"content": "x" * (513 * 1024)}]},
        ]
        for case in cases:
            with self.subTest(case=list(case)):
                adapter = self.make()
                with self.assertRaises(NativePilotModelError):
                    await self.call(adapter, **case)
                self.assertEqual(self.requests, [])
                self.assertEqual(self.reservations, [])

    async def test_budget_consumes_failures_and_latches(self):
        adapter = self.make(max_calls=1)
        await self.call(adapter)
        with self.assertRaisesRegex(NativePilotModelError, "model-call-budget"):
            await self.call(adapter)
        self.assertEqual(len(self.requests), 1)
        adapter = self.make(lambda _: httpx.Response(500, text="sensitive body " + SYNTHETIC_KEY))
        with self.assertRaisesRegex(NativePilotModelError, "^model-http-failed$"):
            await self.call(adapter)
        self.assertEqual(adapter.calls, 1)
        with self.assertRaises(NativePilotModelError):
            await self.call(adapter)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(SYNTHETIC_KEY, repr(self.events))

    async def test_stop_deadline_and_reservation_refusal(self):
        def refuse():
            raise RuntimeError("private failure text")
        for extra in ({"should_stop": lambda: True}, {"deadline": time.monotonic() - 1},
                      {"should_stop": refuse}, {"should_stop": lambda: "false"},
                      {"reserve_call": refuse}, {"on_event": refuse}):
            adapter = self.make(**extra)
            with self.assertRaises(NativePilotModelError) as raised:
                await self.call(adapter)
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(self.requests, [])

    async def test_deadline_bounds_total_request_and_cancellation(self):
        async def slow(_):
            await asyncio.sleep(1)
            return httpx.Response(200, json=envelope())
        adapter = self.make(slow, deadline=time.monotonic() + 0.02)
        with self.assertRaisesRegex(NativePilotModelError, "model-timeout"):
            await self.call(adapter)
        self.assertEqual(len(self.requests), 1)
        adapter = self.make(slow)
        task = asyncio.create_task(self.call(adapter))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(adapter.stopped_reason, "model-cancelled")

    async def test_transport_error_does_not_leak(self):
        def fail(_):
            raise httpx.ReadError("secret text " + SYNTHETIC_KEY)
        adapter = self.make(fail)
        with self.assertRaisesRegex(NativePilotModelError, "^model-response-failed$") as raised:
            await self.call(adapter)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(SYNTHETIC_KEY, repr(self.events))

    async def test_response_bound(self):
        for response in (httpx.Response(200, headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}, content=b"{}"),
                         httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
                         httpx.Response(200, headers={"content-length": "not a size"}, content=b"{}")):
            adapter = self.make(lambda _, response=response: response)
            with self.assertRaisesRegex(NativePilotModelError, "model-response-too-large"):
                await self.call(adapter)

    async def test_invalid_provider_envelopes_and_usage(self):
        cases = []
        for key, value in (("model", "wrong"), ("error", "sensitive"), ("choices", []), ("usage", [])):
            data = envelope(); data[key] = value; cases.append(data)
        for key, value in (("finish_reason", "length"), ("finish_reason", None)):
            data = envelope(); data["choices"][0][key] = value; cases.append(data)
        for key, value in (("content", ""), ("content", []), ("role", "user"), ("tool_calls", [{}]), ("refusal", "no")):
            data = envelope(); data["choices"][0]["message"][key] = value; cases.append(data)
        for key, value in (("prompt_tokens", True), ("completion_tokens", -1), ("cost", True), ("cost", -0.1),
                           ("cost", "0.01"), ("prompt_tokens_details", [])):
            data = envelope(); data["usage"][key] = value; cases.append(data)
        data = envelope(); data["usage"]["prompt_tokens_details"]["cached_tokens"] = True; cases.append(data)
        for data in cases:
            adapter = self.make(lambda _, data=data: httpx.Response(200, json=data))
            with self.assertRaises(NativePilotModelError):
                await self.call(adapter)
            self.assertEqual(len(self.requests), 1)

    async def test_unknown_cost_is_not_reported_as_free(self):
        data = envelope(); del data["usage"]["cost"]
        adapter = self.make(lambda _: httpx.Response(200, json=data))
        result = await self.call(adapter)
        self.assertFalse(result.raw["pilot_cost_known"])
        self.assertFalse(self.events[-1]["cost_known"])
        self.assertIsNone(self.events[-1]["cost_usd"])

    async def test_post_response_stop_discards_result(self):
        stopped = False
        def handler(_):
            nonlocal stopped
            stopped = True
            return httpx.Response(200, json=envelope())
        adapter = self.make(handler, should_stop=lambda: stopped)
        with self.assertRaisesRegex(NativePilotModelError, "model-pilot-stopped"):
            await self.call(adapter)
        self.assertEqual(self.events[-1]["event"], "model-failed")

    async def test_concurrent_calls_reserve_without_overshoot(self):
        adapter = self.make(max_calls=1)
        results = await asyncio.gather(self.call(adapter), self.call(adapter), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, LLMResult) for r in results), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.reservations, ["reserved"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ModelTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("all cases passed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
