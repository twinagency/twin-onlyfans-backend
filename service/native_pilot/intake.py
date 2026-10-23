"""Live inbound for the one scoped fan (or, with ``fan=None``, every fan of the account), through FASTT's existing event machinery.

One ``of_ws.OFWebSocket`` on the SAME verified client (session + proxy) the
runner sends with: no second socket, no new WebSocket subsystem. Its own
reconnect/backoff is kept; this wrapper only adds a second backoff in case the
generator itself ever ends. Each plain-text message from the scoped fan is
persisted by ``event_transcoder._transcode_chat_message`` into the shared
Chat/Fan/Message rows (keyed ``of-<owner>``). That upsert is by
(account, fan, message_id), so it is idempotent with Twin's worker intake and no
second history exists. Everything else is dropped here:

* frames for another fan (all-fans mode: none are dropped for that) and above all our own
  account's echoes, which the transcoder would otherwise misfile as inbound;
* media / tip / priced / locked / gif input (``unsupported-input``): the ledger
  holds on those, exactly as in the recovered ``tools/intake.py``;
* messages already stored.

The runner's ledger, not this module, decides what is fresh enough to answer.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from twin_bridge import store

from .scope import AccountScope

BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0


def _emit(event):
    print(json.dumps(event, sort_keys=True, allow_nan=False), flush=True)


def _default_socket(client):
    from of_ws import OFWebSocket
    return OFWebSocket(client)


async def _default_transcode(account_id, message):
    from event_transcoder import _transcode_chat_message
    await _transcode_chat_message(account_id, message)


def unsupported(message: dict) -> bool:
    return bool(message.get("media") or message.get("mediaCount") or message.get("giphyId") or message.get("isTip")
                or message.get("price") or message.get("lockedText"))


class Intake:
    def __init__(self, scope: AccountScope, fan: int | None, path, *, socket_factory=_default_socket,
                 transcode=_default_transcode, emit=_emit, backoff_s: float = BACKOFF_MIN_S):
        self.scope, self.fan, self.path = scope, fan, path
        self._factory, self._transcode, self._emit = socket_factory, transcode, emit
        self._backoff0 = backoff_s
        self._task = None
        self._ws = None
        self._stopping = False

    def _exists(self, message_id: int, fan: int | None = None) -> bool:
        with store.readonly(self.path) as con:
            return con.execute("SELECT 1 FROM messages WHERE account_id=? AND fan_id=? AND message_id=?",
                               (self.scope.db_id, self.fan if fan is None else fan, message_id)).fetchone() is not None

    async def handle(self, event) -> str:
        """Process one frame; returns a fixed label (also what the tests assert)."""
        if not isinstance(event, dict):
            return "ignored"
        if event.get("connected") is True:
            self._emit({"event": "intake-connected", "account_id": self.scope.db_id, "fan_id": self.fan})
            return "connected"
        message = event.get("api2_chat_message")
        if not isinstance(message, dict):
            return "ignored"
        sender = message.get("fromUser")
        if not isinstance(sender, dict) or isinstance(sender.get("id"), bool):
            return "other-sender"
        if self.fan is None:
            # All fans of the account: any positive fan id except the account itself (its own
            # outbound echoes must never be filed as inbound).
            sid = sender.get("id")
            if type(sid) is not int or sid <= 0 or str(sid) == self.scope.provider_id:
                return "other-sender"
            fan = sid
        elif str(sender.get("id")) != str(self.fan):
            return "other-sender"
        else:
            fan = self.fan
        mid = message.get("id")
        if type(mid) is not int or mid <= 0:
            return "invalid"
        try:
            datetime.fromisoformat(str(message["createdAt"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return "invalid"
        if unsupported(message):
            self._emit({"event": "unsupported-input", "message_id": mid})
            return "unsupported"
        try:
            if await asyncio.to_thread(self._exists, mid, fan):
                return "duplicate"
            await self._transcode(self.scope.db_id, message)
            stored = await asyncio.to_thread(self._exists, mid, fan)
        except Exception:
            self._emit({"event": "intake-persist-failed", "message_id": mid})
            return "failed"
        self._emit({"event": "inbound-persisted", "message_id": mid, "verified": stored})
        return "persisted" if stored else "failed"

    async def _run(self, client) -> None:
        backoff = self._backoff0
        while not self._stopping:
            ws = self._ws = self._factory(client)
            try:
                async for event in ws.events():
                    await self.handle(event)
                    backoff = self._backoff0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit({"event": "intake-error", "error_type": type(exc).__name__})
            finally:
                try:
                    ws.stop()
                except Exception:
                    pass
            if self._stopping:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    def start(self, client) -> None:
        if self._task is not None:
            raise RuntimeError("intake-already-started")
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(self._run(client))

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            try:
                self._ws.stop()
            except Exception:
                pass
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, 10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                pass
