"""The event bus: cross-thread delivery, eager subscription and the bounded subscriber queue."""

import asyncio
import threading

from emailsupersearch.host.sse import QUEUE_MAX, EventBus


def test_publish_from_thread_reaches_loop_subscriber():
    bus = EventBus()

    async def main():
        bus.attach_loop(asyncio.get_running_loop())
        events = bus.subscribe("sync:p")
        thread = threading.Thread(target=bus.publish, args=("sync:p", {"type": "scan_started"}))
        thread.start()
        try:
            return await asyncio.wait_for(anext(events), 5)
        finally:
            thread.join()
            await events.aclose()

    assert asyncio.run(main()) == {"type": "scan_started"}
    assert bus.last("sync:p") == {"type": "scan_started"}


def test_subscribe_registers_before_first_next():
    bus = EventBus()

    async def main():
        bus.attach_loop(asyncio.get_running_loop())
        events = bus.subscribe("t")
        bus.publish("t", {"n": 1})  # before the first __anext__
        try:
            return await asyncio.wait_for(anext(events), 5)
        finally:
            await events.aclose()

    assert asyncio.run(main()) == {"n": 1}


def test_full_queue_drops_oldest():
    bus = EventBus()

    async def main():
        bus.attach_loop(asyncio.get_running_loop())
        events = bus.subscribe("t")
        for n in range(QUEUE_MAX + 1):
            bus.publish("t", {"n": n})
        try:
            return await anext(events)
        finally:
            await events.aclose()

    assert asyncio.run(main()) == {"n": 1}
