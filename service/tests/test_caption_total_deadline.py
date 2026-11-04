"""Caption deadlines must finish the existing receipt and cap reconciliation."""
import asyncio
import contextlib
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import llm_client as llm


class CaptionDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.dict(os.environ, {}, clear=True))
        model = SimpleNamespace(wire_model=lambda: "synthetic", reasoning_keys=lambda: {})
        provider = SimpleNamespace(base_url="https://synthetic.invalid/v1", name="synthetic")
        self.patches.enter_context(patch.object(llm, "_resolve", return_value=(model, provider)))
        self.patches.enter_context(patch.object(llm, "telegram_call_refusal", return_value=None))
        self.patches.enter_context(patch.object(llm, "_resolve_effort", return_value=""))
        self.patches.enter_context(patch.object(llm, "_estimate_input_tokens", return_value=10))
        self.patches.enter_context(patch.object(llm, "_cost_millicents", return_value=100))
        self.patches.enter_context(patch.object(llm, "_CAPTION_TOTAL_TIMEOUT_S", .05))
        self.patches.enter_context(patch.object(llm, "_LLM_RETRY_BACKOFF_S", .01))
        self.mocks = {}
        for name, value in [("_account_effort", ""), ("_tenant_api_key", "synthetic-key"),
                            ("_get_cap_cents", 1000), ("_reserve", None),
                            ("_insert_pending_call", 101), ("_finalize_call", None),
                            ("_adjust_daily", None)]:
            mock = AsyncMock(return_value=value)
            self.mocks[name] = mock
            self.patches.enter_context(patch.object(llm, name, mock))
        self.client = SimpleNamespace(post=AsyncMock())
        manager = SimpleNamespace()
        class ClientManager:
            async def __aenter__(inner): return self.client
            async def __aexit__(inner, *args): return False
        self.patches.enter_context(patch.object(llm.httpx, "AsyncClient", return_value=ClientManager()))

    async def call(self, purpose="describe_media"):
        return await llm.chat(model="synthetic", messages=[{"role": "user", "content": "synthetic"}],
                              purpose=purpose, account_id="of-900001")

    @staticmethod
    def response():
        return httpx.Response(200, request=httpx.Request("POST", "https://synthetic.invalid/v1"),
            json={"choices": [{"message": {"content": "synthetic description"}}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 10}})

    def assert_finalized_error_once(self):
        self.mocks["_reserve"].assert_awaited_once()
        self.mocks["_insert_pending_call"].assert_awaited_once()
        self.mocks["_finalize_call"].assert_awaited_once()
        self.assertEqual(self.mocks["_finalize_call"].await_args.kwargs["status"], "error")
        self.mocks["_adjust_daily"].assert_awaited_once()
        self.assertEqual(self.mocks["_adjust_daily"].await_args.args[2:4], (-100, -1))

    async def test_nonfinishing_caption_is_cancelled_and_finalized(self):
        cancelled = asyncio.Event()
        async def never(*args, **kwargs):
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        self.client.post.side_effect = never
        began = time.monotonic()
        with self.assertRaises(llm.LLMHTTPError): await self.call()
        self.assertLess(time.monotonic() - began, 1)
        self.assertTrue(cancelled.is_set())
        self.client.post.assert_awaited_once()
        self.assert_finalized_error_once()

    async def test_retries_share_one_deadline_and_one_reservation(self):
        calls = 0
        async def transient_then_hang(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1: raise httpx.ConnectError("synthetic transient failure")
            await asyncio.Event().wait()
        self.client.post.side_effect = transient_then_hang
        with self.assertRaises(llm.LLMHTTPError): await self.call()
        self.assertEqual(calls, 2)
        self.assert_finalized_error_once()

    async def test_successful_retry_within_deadline_reconciles_normally(self):
        self.client.post.side_effect = [httpx.ConnectError("synthetic transient failure"), self.response()]
        result = await self.call()
        self.assertEqual(result.content, "synthetic description")
        self.assertEqual(self.client.post.await_count, 2)
        self.mocks["_finalize_call"].assert_awaited_once()
        self.assertEqual(self.mocks["_finalize_call"].await_args.kwargs["status"], "done")
        self.assertEqual(self.mocks["_adjust_daily"].await_args.args[2:4], (0, 0))

    async def test_unrelated_model_purpose_keeps_existing_timeout_behavior(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(.08)
            return self.response()
        self.client.post.side_effect = slow
        result = await self.call("synthetic_non_caption")
        self.assertEqual(result.content, "synthetic description")
        self.assertEqual(self.mocks["_finalize_call"].await_args.kwargs["status"], "done")

    async def test_budget_refusal_sends_nothing_and_creates_no_receipt(self):
        self.mocks["_reserve"].side_effect = llm.LLMCapExceeded("of-900001", 1, 2)
        with self.assertRaises(llm.LLMCapExceeded): await self.call()
        self.client.post.assert_not_awaited()
        self.mocks["_insert_pending_call"].assert_not_awaited()
        self.mocks["_finalize_call"].assert_not_awaited()
        self.mocks["_adjust_daily"].assert_not_awaited()


if __name__ == "__main__": unittest.main()
