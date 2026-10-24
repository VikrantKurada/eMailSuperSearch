"""The event bus behind the host's server-sent event streams.

Supervisors publish sync events from their own threads; the SSE routes subscribe on the event loop. ``publish``
hands each event over to the loop with ``call_soon_threadsafe``, where it is put on every subscriber's queue.
Keepalive pings are left to sse-starlette.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator


class EventBus:
    """Topic-based fan-out of event dicts to asyncio subscribers; ``publish`` may be called from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: dict[str, set[asyncio.Queue[dict]]] = {}
        self._last: dict[str, dict] = {}

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Deliver events on ``loop`` from now on; the app's lifespan calls this at startup."""
        self._loop = loop

    def publish(self, topic: str, event: dict) -> None:
        """Record ``event`` as the topic's last and queue it for the topic's subscribers.

        Off the loop it is handed over with ``call_soon_threadsafe``; on the loop, or before a loop is attached, it
        is delivered at once. Once the loop is closed only the last event is recorded.
        """
        with self._lock:
            self._last[topic] = event
        loop = self._loop
        if loop is not None and not _running_on(loop):
            if loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(self._deliver, topic, event)
            except RuntimeError:  # the loop closed in between: nobody is listening any more
                pass
            return
        self._deliver(topic, event)

    def last(self, topic: str) -> dict | None:
        """The most recent event published on ``topic``, or None."""
        with self._lock:
            return self._last.get(topic)

    async def subscribe(self, topic: str) -> AsyncIterator[dict]:
        """Yield every event published on ``topic`` from now until the consumer stops iterating."""
        queue: asyncio.Queue[dict] = asyncio.Queue()
        with self._lock:
            self._subscribers.setdefault(topic, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            with self._lock:
                queues = self._subscribers.get(topic)
                if queues is not None:
                    queues.discard(queue)
                    if not queues:
                        del self._subscribers[topic]

    def _deliver(self, topic: str, event: dict) -> None:
        with self._lock:
            queues = list(self._subscribers.get(topic, ()))
        for queue in queues:
            queue.put_nowait(event)


def _running_on(loop: asyncio.AbstractEventLoop) -> bool:
    """Whether the calling thread is running ``loop``."""
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False
