"""A tiny in-process event bus.

This is what keeps the download core free of any dependency on external
services. The queue worker publishes facts ("this download finished"); anything
that cares — the webhook dispatcher today — subscribes. Neither side imports the
other.

Subscribers run synchronously on the publishing thread, so they must be quick
and must not raise: a slow or broken subscriber would otherwise stall a
download. Anything that can block belongs behind its own queue (see
``integrations.webhooks``, which only writes a row and returns).
"""

import threading
import time
from dataclasses import asdict, dataclass, field

from .logger import get_logger

logger = get_logger(__name__)

DOWNLOAD_STARTED = "download_started"
DOWNLOAD_COMPLETED = "download_completed"
DOWNLOAD_FAILED = "download_failed"
IMPORT_COMPLETED = "import_completed"

ALL_EVENTS = (
    DOWNLOAD_STARTED,
    DOWNLOAD_COMPLETED,
    DOWNLOAD_FAILED,
    IMPORT_COMPLETED,
)


@dataclass
class Event:
    """A single thing that happened to a download.

    The field set is the webhook payload from the documentation, plus the queue
    id and a timestamp so a receiver can correlate and order deliveries.
    """

    event: str
    type: str = None
    title: str = None
    season: int = None
    episode: int = None
    path: str = None
    queue_id: int = None
    error: str = None
    timestamp: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)

    def to_payload(self):
        payload = asdict(self)
        extra = payload.pop("extra", None) or {}
        payload.update(extra)
        return {k: v for k, v in payload.items() if v is not None}


_subscribers = []
_lock = threading.Lock()


def subscribe(callback):
    """Register a callable taking one :class:`Event`. Idempotent."""
    with _lock:
        if callback not in _subscribers:
            _subscribers.append(callback)
    return callback


def unsubscribe(callback):
    with _lock:
        if callback in _subscribers:
            _subscribers.remove(callback)


def clear():
    """Drop all subscribers. Only used by tests."""
    with _lock:
        _subscribers.clear()


def publish(event):
    """Deliver an event to every subscriber.

    A failing subscriber is logged and skipped; it must never propagate into
    the download path that published the event.
    """
    with _lock:
        targets = list(_subscribers)
    for callback in targets:
        try:
            callback(event)
        except Exception as exc:
            logger.warning(
                f"Event subscriber {getattr(callback, '__name__', callback)} "
                f"failed for {event.event}: {exc}"
            )


def emit(event_name, **kwargs):
    """Convenience wrapper: build an :class:`Event` and publish it."""
    publish(Event(event=event_name, **kwargs))
