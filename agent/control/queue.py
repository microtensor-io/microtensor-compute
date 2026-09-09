from __future__ import annotations

import asyncio
import contextlib
import threading
from collections import deque
from typing import Any

from protocol.messages import AgentMessage, stamp

MAX_DEPTH = 2000
DROPPABLE = {AgentMessage.STATE.value, AgentMessage.PING.value}
PRECIOUS = {AgentMessage.RESULT.value, AgentMessage.EVENT.value}


class OutboundQueue:
    def __init__(self, max_depth: int = MAX_DEPTH) -> None:
        self.max_depth = max_depth
        self._items: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._ready: asyncio.Event | None = None
        self.dropped = 0

    def _event(self) -> asyncio.Event:
        if self._ready is None:
            self._ready = asyncio.Event()
        return self._ready

    @property
    def depth(self) -> int:
        return len(self._items)

    def _drop_oldest_droppable(self) -> bool:
        for position, item in enumerate(self._items):
            if item.get("type") in DROPPABLE:
                del self._items[position]
                self.dropped += 1
                return True
        return False

    def put(self, message: dict[str, Any], front: bool = False) -> None:
        message.setdefault("sent_at", stamp())
        with self._lock:
            if len(self._items) >= self.max_depth and message.get("type") not in PRECIOUS:
                if not self._drop_oldest_droppable() or len(self._items) >= self.max_depth:
                    self.dropped += 1
                    return
            elif len(self._items) >= self.max_depth:
                self._drop_oldest_droppable()
            if front:
                self._items.appendleft(message)
            else:
                self._items.append(message)
        self._wake()

    def requeue(self, message: dict[str, Any]) -> None:
        with self._lock:
            self._items.appendleft(message)
        self._wake()

    def _wake(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._event().set()

    async def get(self) -> dict[str, Any]:
        while True:
            with self._lock:
                if self._items:
                    return self._items.popleft()
                self._event().clear()
            await self._event().wait()

    def stamp(self, message: dict[str, Any]) -> dict[str, Any]:
        message["queue_depth"] = self.depth
        message["forwarded_at"] = stamp()
        return message

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            kinds: dict[str, int] = {}
            for item in self._items:
                kind = str(item.get("type", "?"))
                kinds[kind] = kinds.get(kind, 0) + 1
        return {"depth": self.depth, "dropped": self.dropped, **kinds}
