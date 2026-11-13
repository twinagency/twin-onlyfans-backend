@pytest.mark.xfail(strict=True, reason="G03b: per-fan backoff is process memory "
                   "(HeadState.failed); restart durability needs a schema column")
def test_failed_fan_backoff_survives_worker_restart(native, monkeypatch):
    store, account, reader = native
    now = _fake_clock(monkeypatch)
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([BAD], None, {BAD: 500}))
    calls = [0]

    def messages(**kw):
        calls[0] += 1
        raise NativeProtocolError("synthetic permanently malformed conversation")

    monkeypatch.setattr(reader, "messages", messages)
    for restart in range(6):  # six worker restarts, 5 simulated minutes each
        store._recent_inbox_states = {}
        _run(store, account, reader, now, 300)
    assert calls[0] <= max_reads(1800), f"inv.14: failing fan read {calls[0]}x over 30 min with 6 restarts"
