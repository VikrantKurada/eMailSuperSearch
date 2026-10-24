"""The event bus behind the host's server-sent event streams.

Supervisors publish sync events from their own threads; the SSE routes subscribe on the event loop. ``publish``
hands each event over to the loop with ``call_soon_threadsafe``, where it is put on every subscriber's queue.
Keepalive pings are left to sse-starlette.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

QUEUE_MAX = 1000  # events held per subscriber; on overflow the oldest is dropped


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

    def subscribe(self, topic: str) -> Subscription:
        """Start receiving ``topic`` now and return a ``Subscription`` over its events, from the loop.

        The subscriber's queue is registered by this call, so nothing published before the first ``__anext__`` is
        lost. The caller unregisters it with ``aclose()``, in a ``finally``: that works whether or not iteration
        ever started, a client that disconnects before the first event included.
        """
        return Subscription(self, topic)

    def _register(self, topic: str, queue: asyncio.Queue[dict]) -> None:
        with self._lock:
            self._subscribers.setdefault(topic, set()).add(queue)

    def _unregister(self, topic: str, queue: asyncio.Queue[dict]) -> None:
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
            if queue.full():  # a stalled subscriber: drop its oldest event rather than grow without bound
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)


class Subscription:
    """One subscriber's events on one topic, as an async iterator; made by ``EventBus.subscribe``.

    Its queue (at most ``QUEUE_MAX`` events) is registered on creation. ``aclose()`` unregisters it, whether or not
    iteration ever started, and may be called again; once closed, iteration ends.
    """

    def __init__(self, bus: EventBus, topic: str) -> None:
        self._bus = bus
        self._topic = topic
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._closed = False
        bus._register(topic, self._queue)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> dict:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        """Stop receiving: unregister the queue. Safe to call twice, and before any ``__anext__``."""
        if self._closed:
            return
        self._closed = True
        self._bus._unregister(self._topic, self._queue)


def _running_on(loop: asyncio.AbstractEventLoop) -> bool:
    """Whether the calling thread is running ``loop``."""
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False
