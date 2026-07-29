"""Outgoing webhooks.

Subscribes to the event bus and turns events into HTTP POSTs to whatever URLs
``ANIWORLD_WEBHOOK_URLS`` names.

Delivery goes through a database-backed outbox rather than straight out of the
event handler. Two reasons, both about not letting a receiver hurt the
downloader: a slow or unreachable endpoint must not block the thread that is
downloading, and an event must not be lost because the container restarted
between "download finished" and "webhook delivered".
"""

import hashlib
import hmac
import json
import os
import threading
import time

import niquests

from ..events import ALL_EVENTS, Event
from ..logger import get_logger
from .base import env_int, read_secret

logger = get_logger(__name__)

DELIVERY_TIMEOUT = 15
POLL_INTERVAL = 5
MAX_ATTEMPTS = 8

_dispatcher_started = False
_dispatcher_lock = threading.Lock()
_wake = threading.Event()


def configured_urls():
    raw = os.getenv("ANIWORLD_WEBHOOK_URLS", "")
    return [url.strip() for url in raw.split(",") if url.strip()]


def enabled():
    return bool(configured_urls())


def sign(body, secret=None):
    """HMAC-SHA256 over the exact bytes sent, as ``sha256=<hex>``.

    Signing the serialised body rather than the dict is what lets a receiver
    verify it: it only ever sees the bytes.
    """
    if secret is None:
        secret = read_secret("ANIWORLD_WEBHOOK_SECRET")
    if not secret:
        return None
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def on_event(event):
    """Event-bus subscriber. Only ever writes rows — never sends."""
    if not isinstance(event, Event) or event.event not in ALL_EVENTS:
        return
    urls = configured_urls()
    if not urls:
        return

    from ..web.db import enqueue_webhook

    payload = json.dumps(event.to_payload(), sort_keys=True)
    for url in urls:
        try:
            enqueue_webhook(url, event.event, payload)
        except Exception as exc:
            logger.warning(f"Could not queue webhook for {url}: {exc}")
    _wake.set()


def deliver(url, payload):
    """POST one payload. Returns ``(ok, detail)``."""
    body = payload.encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "AniWorld-Downloader",
    }
    signature = sign(body)
    if signature:
        headers["X-AniWorld-Signature"] = signature

    try:
        response = niquests.post(
            url, data=body, headers=headers, timeout=DELIVERY_TIMEOUT
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if 200 <= response.status_code < 300:
        return True, None
    return False, f"HTTP {response.status_code}"


def backoff_seconds(attempts):
    base = env_int("ANIWORLD_WEBHOOK_BACKOFF_BASE", 10)
    return min(base * (2 ** max(0, attempts)), 3600)


def _dispatch_once(batch_size=20):
    """Send everything currently due. Returns how many were delivered."""
    from ..web.db import get_due_webhooks, mark_webhook_delivered, mark_webhook_failed

    delivered = 0
    for row in get_due_webhooks(batch_size):
        ok, detail = deliver(row["url"], row["payload"])
        if ok:
            mark_webhook_delivered(row["id"])
            delivered += 1
            continue

        retrying = mark_webhook_failed(
            row["id"],
            detail,
            backoff_seconds(row["attempts"] or 0),
            max_attempts=MAX_ATTEMPTS,
        )
        level = logger.debug if retrying else logger.warning
        level(
            f"Webhook {row['event']} to {row['url']} failed ({detail}); "
            + ("will retry" if retrying else "giving up")
        )
    return delivered


def _dispatcher_loop():
    from ..web.db import purge_delivered_webhooks

    last_purge = 0.0
    while True:
        try:
            _dispatch_once()
            if time.time() - last_purge > 3600:
                purge_delivered_webhooks()
                last_purge = time.time()
        except Exception as exc:
            logger.warning(f"Webhook dispatcher error: {exc}")
        _wake.wait(POLL_INTERVAL)
        _wake.clear()


def start_dispatcher():
    """Start the delivery thread once. Safe to call repeatedly."""
    global _dispatcher_started
    with _dispatcher_lock:
        if _dispatcher_started:
            return
        _dispatcher_started = True

    from ..web.db import init_webhook_db

    init_webhook_db()
    thread = threading.Thread(
        target=_dispatcher_loop, name="webhook-dispatcher", daemon=True
    )
    thread.start()
    logger.debug(f"Webhook dispatcher started for {len(configured_urls())} URL(s)")
