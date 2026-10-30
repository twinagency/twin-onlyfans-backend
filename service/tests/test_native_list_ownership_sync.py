import sqlite3
import sys
import types
from contextlib import contextmanager

import pytest

from native_list_ownership_sync import Mirror, assignment, snapshot


def row(mode="automated", takeover="none", attempt=None):
    return dict(mode=mode, takeover_state=takeover, unresolved_attempt_id=attempt)


class Provider:
    def __init__(self):
        self.members = {1: set(), 2: set(), 3: set()}
        self.calls = []
        self.fail = False

    def remove_user_from_list(self, lid, fan):
        self.calls.append(("remove", lid, fan))
        if fan not in self.members[lid]:
            exc = RuntimeError("member not found")
            exc.response = types.SimpleNamespace(status_code=404)
            raise exc
        self.members[lid].discard(fan)

    def get_list(self, lid):
        if lid not in self.members:
            raise RuntimeError("list missing")
        return {"id": lid}

    def add_user_to_list(self, lid, fan):
        self.calls.append(("add", lid, fan))
        self.members[lid].add(fan)
        if self.fail:
            self.fail = False
            raise TimeoutError("accepted remotely, response lost")


LISTS = dict(ai=1, human=2, hold=3)


@pytest.mark.parametrize("control,expected", [
    (None, None), (row(), "ai"), (row(attempt="in-flight-ai"), "ai"),
    (row("human_controlled", "requested", "in-flight-ai"), "hold"),
    (row("human_controlled", "effective"), "human"),
    (row("human_controlled", "effective", "unknown"), "hold"),
    (row("paused"), "hold"), (row("corrupt"), "hold"),
])
def test_control_assignment(control, expected):
    assert assignment(control) == expected


def test_ambiguous_write_survives_restart_and_retries_idempotently(tmp_path):
    path = tmp_path / "ledger.db"
    mirror = Mirror(path, "of-1")
    provider = Provider()
    provider.fail = True
    with pytest.raises(TimeoutError):
        mirror.sync_fan(provider, LISTS, 7, lambda _: row())
    mirror.db.close()
    recovered = Mirror(path, "of-1")
    assert list(recovered.changed({7: row()})) == [7]
    assert recovered.sync_fan(provider, LISTS, 7, lambda _: row())
    assert provider.members == {1: {7}, 2: set(), 3: set()}
    assert list(recovered.changed({7: row()})) == []
    recovered.db.close()


def test_human_ready_is_added_after_old_assignment_removed(tmp_path):
    mirror = Mirror(tmp_path / "ledger.db", "of-1")
    provider = Provider()
    mirror.sync_fan(provider, LISTS, 7, lambda _: row())
    provider.calls.clear()
    assert mirror.sync_fan(provider, LISTS, 7, lambda _: row("human_controlled", "effective"))
    assert provider.calls == [("remove", 1, 7), ("remove", 3, 7), ("add", 2, 7)]
    assert provider.members[2] == {7}


def test_control_change_during_removals_cannot_publish_stale_assignment(tmp_path):
    mirror = Mirror(tmp_path / "ledger.db", "of-1")
    provider = Provider()
    states = iter([row(), row("human_controlled", "requested")])
    assert not mirror.sync_fan(provider, LISTS, 7, lambda _: next(states))
    assert all(call[0] != "add" for call in provider.calls)
    assert list(mirror.changed({7: row("human_controlled", "requested")})) == [7]


def test_deleted_control_is_removed_from_managed_lists(tmp_path):
    mirror = Mirror(tmp_path / "ledger.db", "of-1")
    provider = Provider()
    mirror.sync_fan(provider, LISTS, 7, lambda _: row())
    assert list(mirror.changed({})) == [7]
    mirror.sync_fan(provider, LISTS, 7, lambda _: None)
    assert provider.members[1] == set()
    assert list(mirror.changed({})) == []


def test_missing_list_does_not_turn_404_into_success(tmp_path):
    mirror = Mirror(tmp_path / "ledger.db", "of-1")
    class Deleted(Provider):
        def get_list(self, lid):
            raise RuntimeError("list missing")
    provider = Deleted()
    with pytest.raises(RuntimeError, match="list missing"):
        mirror.sync_fan(provider, LISTS, 7, lambda _: row())
    assert list(mirror.changed({7: row()})) == [7]
    assert all(call[0] != "add" for call in provider.calls)


def test_cross_account_ledger_is_refused(tmp_path):
    path = tmp_path / "ledger.db"
    Mirror(path, "of-1").db.close()
    with pytest.raises(ValueError, match="different account"):
        Mirror(path, "of-2")


def test_lost_create_response_recovers_unique_list_without_duplicate(tmp_path):
    mirror = Mirror(tmp_path / "ledger.db", "of-1")
    class Lists:
        def __init__(self):
            self.rows = []
            self.fail = True
        def create_list(self, name):
            result = dict(id=len(self.rows)+1, name=name)
            self.rows.append(result)
            if self.fail:
                self.fail = False
                raise TimeoutError()
            return result
        def get_lists(self, **kw):
            return dict(list=self.rows[:], hasMore=False)
        def set_list_pinned_to_chat(self, *args):
            pass
    client = Lists()
    with pytest.raises(TimeoutError):
        mirror.lists(client)
    assert mirror.lists(client) == LISTS
    assert len(client.rows) == 3


def test_complete_100001_fan_snapshot_and_noop_delta(tmp_path, monkeypatch):
    path = tmp_path / "source.db"
    source = sqlite3.connect(path)
    source.execute("CREATE TABLE of_conversation_state (account_id TEXT, fan_id INTEGER, mode TEXT, takeover_state TEXT, unresolved_attempt_id TEXT, PRIMARY KEY(account_id,fan_id))")
    source.executemany("INSERT INTO of_conversation_state VALUES ('of-1',?,'automated','none',NULL)", ((i,) for i in range(1, 100002)))
    source.execute("INSERT INTO of_conversation_state VALUES ('of-2',100002,'automated','none',NULL)")
    source.commit()
    source.close()
    @contextmanager
    def readonly(target):
        con = sqlite3.connect(target)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        try:
            yield con
        finally:
            con.close()
    monkeypatch.setitem(sys.modules, "twin_bridge", types.SimpleNamespace(store=types.SimpleNamespace(readonly=readonly)))
    rows = snapshot(str(path), "of-1")
    assert len(rows) == 100001 and max(rows) == 100001
    mirror = Mirror(tmp_path / "ledger.db", "of-1")
    mirror.db.executemany("INSERT INTO applied VALUES (?,'ai')", ((i,) for i in rows))
    mirror.db.commit()
    assert list(mirror.changed(rows)) == []
    rows[99999] = row("human_controlled", "effective")
    assert list(mirror.changed(rows)) == [99999]
