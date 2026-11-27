# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-user event hub behind the SSE stream (with replay by Last-Event-ID)."""

from __future__ import annotations

import asyncio
import collections
import itertools
import time
from typing import Any, Callable, Optional

_REPLAY = 1000


class EventHub:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._buffers: dict[str, collections.deque] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self.on_first_subscriber: Optional[Callable[[str], None]] = None
        self.on_last_unsubscribe: Optional[Callable[[str], None]] = None

    def publish(self, user: str, event_type: str, session_id: Optional[str] = None, **data: Any) -> dict[str, Any]:
        event = {"id": next(self._ids), "type": event_type, "session_id": session_id, "ts": round(time.time(), 3),
                 **data}
        self._buffers.setdefault(user, collections.deque(maxlen=_REPLAY)).append(event)
        for queue in list(self._subscribers.get(user, ())):
            queue.put_nowait(event)
        return event

    def subscribe(self, user: str, last_event_id: int = 0) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        queue: asyncio.Queue = asyncio.Queue()
        subscribers = self._subscribers.setdefault(user, set())
        first = not subscribers
        subscribers.add(queue)
        if first and self.on_first_subscriber is not None:
            self.on_first_subscriber(user)
        replay = [e for e in self._buffers.get(user, ()) if e["id"] > last_event_id] if last_event_id else []
        return queue, replay

    def unsubscribe(self, user: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(user, set())
        subscribers.discard(queue)
        if not subscribers and self.on_last_unsubscribe is not None:
            self.on_last_unsubscribe(user)

    def has_subscribers(self, user: str) -> bool:
        return bool(self._subscribers.get(user))
