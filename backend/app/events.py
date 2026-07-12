"""In-process pub/sub used to stream live progress to the dashboard over SSE.

Background workers run in threads, so ``publish`` is thread-safe: it hops onto
the asyncio loop captured at startup and fans the event out to each subscriber
queue. The API layer turns those queues into an ``text/event-stream`` response.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

_loop: Optional[asyncio.AbstractEventLoop] = None
_subscribers: set[asyncio.Queue] = set()


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def _deliver(payload: str) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def publish(event_type: str, data: dict[str, Any]) -> None:
    """Publish an event from any thread. No-op if the loop isn't running yet."""
    payload = json.dumps({"type": event_type, "data": data})
    loop = _loop
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(_deliver, payload)
    except RuntimeError:
        pass
