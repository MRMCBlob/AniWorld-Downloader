"""Outgoing webhooks: the outbox, signing and retry behaviour."""

import hashlib
import hmac
import json

import pytest

from aniworld import events
from aniworld.integrations import webhooks


@pytest.fixture
def db(tmp_path, monkeypatch):
    from aniworld.web import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "aniworld.db")
    monkeypatch.setattr(db_module, "ANIWORLD_CONFIG_DIR", tmp_path)
    db_module.init_webhook_db()
    return db_module


@pytest.fixture(autouse=True)
def clean_bus():
    events.clear()
    yield
    events.clear()


def test_an_event_is_queued_for_every_url(db, monkeypatch):
    monkeypatch.setenv(
        "ANIWORLD_WEBHOOK_URLS", "https://a.example/hook, https://b.example/hook"
    )

    webhooks.on_event(
        events.Event(event=events.DOWNLOAD_COMPLETED, title="Show", path="/x.mkv")
    )

    rows = db.get_due_webhooks()
    assert {row["url"] for row in rows} == {
        "https://a.example/hook",
        "https://b.example/hook",
    }
    payload = json.loads(rows[0]["payload"])
    assert payload["event"] == "download_completed"
    assert payload["title"] == "Show"


def test_nothing_is_queued_without_configured_urls(db, monkeypatch):
    monkeypatch.delenv("ANIWORLD_WEBHOOK_URLS", raising=False)

    webhooks.on_event(events.Event(event=events.DOWNLOAD_COMPLETED))

    assert db.get_due_webhooks() == []


def test_unknown_event_names_are_ignored(db, monkeypatch):
    monkeypatch.setenv("ANIWORLD_WEBHOOK_URLS", "https://a.example/hook")

    webhooks.on_event(events.Event(event="something_else"))

    assert db.get_due_webhooks() == []


def test_the_event_path_never_sends_inline(db, monkeypatch):
    """Writing a row is all the download thread may do."""
    monkeypatch.setenv("ANIWORLD_WEBHOOK_URLS", "https://a.example/hook")
    monkeypatch.setattr(
        webhooks.niquests, "post", lambda *a, **k: pytest.fail("sent inline")
    )

    webhooks.on_event(events.Event(event=events.DOWNLOAD_COMPLETED))

    assert len(db.get_due_webhooks()) == 1


def test_signature_covers_the_exact_body(monkeypatch):
    monkeypatch.setenv("ANIWORLD_WEBHOOK_SECRET", "s3cret")
    body = b'{"event":"download_completed"}'

    signature = webhooks.sign(body)

    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert signature == f"sha256={expected}"


def test_no_signature_without_a_secret(monkeypatch):
    monkeypatch.delenv("ANIWORLD_WEBHOOK_SECRET", raising=False)

    assert webhooks.sign(b"{}") is None


def test_a_secret_can_come_from_a_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret"
    secret_file.write_text("from-docker-secret\n")
    monkeypatch.delenv("ANIWORLD_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("ANIWORLD_WEBHOOK_SECRET_FILE", str(secret_file))

    signature = webhooks.sign(b"{}")

    expected = hmac.new(b"from-docker-secret", b"{}", hashlib.sha256).hexdigest()
    assert signature == f"sha256={expected}"


def test_successful_delivery_marks_the_row_done(db, monkeypatch):
    monkeypatch.setenv("ANIWORLD_WEBHOOK_URLS", "https://a.example/hook")
    monkeypatch.setenv("ANIWORLD_WEBHOOK_SECRET", "s3cret")
    webhooks.on_event(events.Event(event=events.IMPORT_COMPLETED, title="Show"))

    sent = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        sent["url"] = url
        sent["headers"] = headers
        return _Response(204)

    monkeypatch.setattr(webhooks.niquests, "post", fake_post)

    assert webhooks._dispatch_once() == 1
    assert db.get_due_webhooks() == []
    assert sent["headers"]["X-AniWorld-Signature"].startswith("sha256=")
    assert sent["headers"]["Content-Type"] == "application/json"


def test_a_failed_delivery_is_retried_later(db, monkeypatch):
    monkeypatch.setenv("ANIWORLD_WEBHOOK_URLS", "https://a.example/hook")
    webhooks.on_event(events.Event(event=events.DOWNLOAD_FAILED))
    monkeypatch.setattr(webhooks.niquests, "post", lambda *a, **k: _Response(500))

    assert webhooks._dispatch_once() == 0
    # Still pending, but pushed into the future by the backoff.
    assert db.get_due_webhooks() == []
    assert db.get_webhook_stats()["pending"] == 1


def test_a_connection_error_is_a_retry_not_a_crash(db, monkeypatch):
    monkeypatch.setenv("ANIWORLD_WEBHOOK_URLS", "https://a.example/hook")
    webhooks.on_event(events.Event(event=events.DOWNLOAD_FAILED))

    def explode(*args, **kwargs):
        raise ConnectionError("receiver is down")

    monkeypatch.setattr(webhooks.niquests, "post", explode)

    assert webhooks._dispatch_once() == 0
    assert db.get_webhook_stats()["pending"] == 1


def test_delivery_gives_up_after_the_attempt_budget(db, monkeypatch):
    monkeypatch.setenv("ANIWORLD_WEBHOOK_URLS", "https://a.example/hook")
    webhooks.on_event(events.Event(event=events.DOWNLOAD_FAILED))
    row_id = db.get_due_webhooks()[0]["id"]

    for _ in range(webhooks.MAX_ATTEMPTS):
        db.mark_webhook_failed(row_id, "HTTP 500", 0, webhooks.MAX_ATTEMPTS)

    assert db.get_webhook_stats()["pending"] == 0, "must stop retrying forever"


def test_backoff_grows_and_is_capped():
    assert webhooks.backoff_seconds(0) == 10
    assert webhooks.backoff_seconds(1) == 20
    assert webhooks.backoff_seconds(2) == 40
    assert webhooks.backoff_seconds(30) == 3600


def test_delivered_rows_are_purged(db, monkeypatch):
    monkeypatch.setenv("ANIWORLD_WEBHOOK_URLS", "https://a.example/hook")
    webhooks.on_event(events.Event(event=events.DOWNLOAD_COMPLETED))
    row_id = db.get_due_webhooks()[0]["id"]
    db.mark_webhook_delivered(row_id)

    conn = db.get_db()
    conn.execute(
        "UPDATE webhook_outbox SET delivered_at = datetime('now', '-30 days')"
    )
    conn.commit()
    conn.close()

    db.purge_delivered_webhooks(keep_days=7)

    conn = db.get_db()
    remaining = conn.execute("SELECT COUNT(*) AS c FROM webhook_outbox").fetchone()["c"]
    conn.close()
    assert remaining == 0


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code
