"""Hand-off queue from the vault page reader to the preview worker (no DB, no provider)."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from onlyfans_runtime import vault_previews as vp


def setup_function(_):
    vp._page_queue.clear()


def test_async_flag_needs_both_flags(monkeypatch):
    monkeypatch.delenv("ONLYFANS_VAULT_PREVIEWS_ENABLED", raising=False)
    monkeypatch.delenv("ONLYFANS_VAULT_PREVIEW_ASYNC", raising=False)
    assert not vp.async_enabled()
    monkeypatch.setenv("ONLYFANS_VAULT_PREVIEWS_ENABLED", "1")
    assert not vp.async_enabled()
    monkeypatch.setenv("ONLYFANS_VAULT_PREVIEW_ASYNC", "1")
    assert vp.async_enabled()


def test_queue_is_fifo_per_account_and_bounded():
    assert not vp.enqueue_page("a", "b", "o", 1, [])
    for i in range(vp.QUEUE_PAGES_PER_ACCOUNT + 3):
        assert vp.enqueue_page("a", "b", "o", 1, [{"id": i}])
    assert vp.queued_accounts() == ["a"]
    first = vp.pop_page("a")
    assert first[3] == [{"id": 3}]  # oldest 3 dropped by maxlen, recovered by the sweep
    while vp.pop_page("a"):
        pass
    assert vp.queued_accounts() == [] and vp.pop_page("a") is None
