"""Synthetic checks for the repeat's fresh-input gate; no network calls."""
import asyncio
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from twin_bridge.client import TwinClientError
from twin_bridge.repeat_runner import FreshClient


class Client:
    def __init__(self):
        self.calls = []

    async def request_draft(self, event):
        self.calls.append("draft")
        return "draft"

    async def read_status(self, event):
        self.calls.append("status")
        return "status"

    async def acknowledge_delivery(self, event):
        self.calls.append("ack")
        return "ack"


async def main():
    cutoff = datetime(2026, 10, 17)
    later = (cutoff + timedelta(seconds=1)).isoformat(" ")
    checks = 0
    with tempfile.TemporaryDirectory(prefix="twin-repeat-fresh-") as root:
        path = Path(root) / "test.sqlite"
        con = sqlite3.connect(path)
        con.executescript("""
          CREATE TABLE twin_bridge_turns(account_id TEXT, fan_id INTEGER, turn_id TEXT, member_ids_json TEXT);
          CREATE TABLE twin_bridge_inputs(account_id TEXT, fan_id INTEGER, provider_message_id INTEGER, occurred_at TEXT, received_at TEXT);
        """)
        con.execute("INSERT INTO twin_bridge_turns VALUES('a',7,'t',?)", (json.dumps([1, 2]),))
        con.executemany("INSERT INTO twin_bridge_inputs VALUES('a',7,?,?,?)", [(1, later, later), (2, later, later)])
        con.commit()
        real = Client()
        client = FreshClient(real, path, "a", 7, cutoff)
        event = {"accountId": "a", "fanId": "7", "messageId": "t"}
        assert await client.request_draft(event) == "draft"
        assert await client.read_status(event) == "status"
        checks += 2
        for field in ("occurred_at", "received_at"):
            for stamp in (cutoff, cutoff - timedelta(days=1)):
                con.execute(f"UPDATE twin_bridge_inputs SET {field}=? WHERE provider_message_id=2", (stamp.isoformat(" "),))
                con.commit()
                for route in (client.request_draft, client.read_status):
                    before = list(real.calls)
                    try:
                        await route(event)
                        raise AssertionError("old source accepted")
                    except TwinClientError as exc:
                        assert exc.category == "repeat-source-refused" and not exc.uncertain
                        assert real.calls == before
                        checks += 1
            con.execute(f"UPDATE twin_bridge_inputs SET {field}=?", (later,))
            con.commit()
        for field, value in (("accountId", "other"), ("fanId", "8"), ("messageId", "missing")):
            try:
                await client.request_draft({**event, field: value})
                raise AssertionError("wrong scope accepted")
            except TwinClientError:
                checks += 1
        con.execute("DELETE FROM twin_bridge_inputs WHERE provider_message_id=2")
        con.commit()
        try:
            await client.request_draft(event)
            raise AssertionError("missing source accepted")
        except TwinClientError:
            checks += 1
        # Acknowledgement remains delegated; stopping new generation cannot erase delivery evidence.
        assert await client.acknowledge_delivery(event) == "ack"
        checks += 1
        con.close()
    print(f"all cases passed ({checks} checks)")


if __name__ == "__main__":
    asyncio.run(main())
