"""Queue schema migration, priority ordering and retry backoff.

The database module resolves its path at import time from the config dir, so
each test points it at a temporary file and re-runs the initialisers.
"""

import importlib
import sqlite3

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh aniworld.db, with the module's DB_PATH pointed at it."""
    from aniworld.web import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "aniworld.db")
    monkeypatch.setattr(db_module, "ANIWORLD_CONFIG_DIR", tmp_path)
    monkeypatch.delenv("ANIWORLD_MAX_RETRIES", raising=False)
    monkeypatch.delenv("ANIWORLD_RETRY_BACKOFF_BASE", raising=False)
    db_module.init_queue_db()
    return db_module


def add(db, title="Show", url="https://example.com/s", episodes=("e1",), **kwargs):
    return db.add_to_queue(title, url, list(episodes), "German Dub", "VOE", **kwargs)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_new_statuses_are_accepted(db):
    queue_id = add(db)
    for status in db.QUEUE_STATUSES:
        db.set_queue_status(queue_id, status)
    assert db.get_queue()[0]["status"] == db.QUEUE_STATUSES[-1]


def test_an_unknown_status_is_still_rejected(db):
    queue_id = add(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.set_queue_status(queue_id, "banana")


def test_legacy_database_is_migrated(tmp_path, monkeypatch):
    """A pre-rename database keeps its rows and gains the new columns."""
    path = tmp_path / "aniworld.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE download_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            series_url TEXT NOT NULL,
            episodes TEXT NOT NULL,
            total_episodes INTEGER NOT NULL,
            language TEXT NOT NULL,
            provider TEXT NOT NULL,
            username TEXT,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued','running','completed','failed','cancelled')),
            current_episode INTEGER NOT NULL DEFAULT 0,
            current_url TEXT,
            errors TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            position INTEGER NOT NULL DEFAULT 0,
            custom_path_id INTEGER,
            source TEXT NOT NULL DEFAULT 'manual',
            captcha_url TEXT,
            discord_user_id TEXT
        );
        INSERT INTO download_queue
            (title, series_url, episodes, total_episodes, language, provider, status)
        VALUES
            ('Mid Download', 'u1', '["a"]', 1, 'German Dub', 'VOE', 'running'),
            ('Done',         'u2', '["b"]', 1, 'German Dub', 'VOE', 'completed');
        """
    )
    legacy.commit()
    legacy.close()

    from aniworld.web import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", path)
    monkeypatch.setattr(db_module, "ANIWORLD_CONFIG_DIR", tmp_path)
    db_module.init_queue_db()

    rows = {row["title"]: row for row in db_module.get_queue()}
    assert rows["Mid Download"]["status"] == "downloading", "running must be renamed"
    assert rows["Done"]["status"] == "completed"
    assert rows["Done"]["priority"] == 0
    assert "next_attempt_at" in rows["Done"]
    # And the new statuses now pass the rebuilt CHECK constraint.
    db_module.set_queue_status(rows["Done"]["id"], "imported")


def test_migration_is_idempotent(db):
    add(db, title="Kept")
    db.init_queue_db()
    db.init_queue_db()
    assert [row["title"] for row in db.get_queue()] == ["Kept"]


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #


def test_priority_beats_position(db):
    first = add(db, title="normal")
    urgent = add(db, title="urgent", priority=10)

    assert db.get_next_queued()["id"] == urgent
    db.set_queue_status(urgent, "downloading")
    assert db.get_next_queued()["id"] == first


def test_position_still_orders_within_a_priority_band(db):
    a = add(db, title="a")
    add(db, title="b")

    assert db.get_next_queued()["id"] == a


def test_an_item_in_backoff_is_skipped(db):
    delayed = add(db, title="delayed")
    later = add(db, title="later")
    db.set_queue_status(delayed, "downloading")
    db.schedule_queue_retry(delayed, 3600, error="hoster down")

    assert db.get_next_queued()["id"] == later


def test_an_elapsed_backoff_makes_the_item_eligible_again(db):
    queue_id = add(db)
    db.set_queue_status(queue_id, "downloading")
    db.schedule_queue_retry(queue_id, -10, error="transient")

    assert db.get_next_queued()["id"] == queue_id


def test_paused_items_are_never_picked_up(db):
    queue_id = add(db)

    assert db.pause_queue_item(queue_id) == (True, None)
    assert db.get_next_queued() is None

    assert db.resume_queue_item(queue_id) == (True, None)
    assert db.get_next_queued()["id"] == queue_id


def test_pause_only_applies_to_queued_items(db):
    queue_id = add(db)
    db.set_queue_status(queue_id, "downloading")

    ok, error = db.pause_queue_item(queue_id)

    assert ok is False
    assert "queued" in error


def test_resume_clears_a_pending_backoff(db):
    queue_id = add(db)
    db.set_queue_status(queue_id, "downloading")
    db.schedule_queue_retry(queue_id, 3600)
    db.pause_queue_item(queue_id)

    db.resume_queue_item(queue_id)

    assert db.get_next_queued()["id"] == queue_id


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #


def test_retries_are_capped_by_max_attempts(db, monkeypatch):
    monkeypatch.setenv("ANIWORLD_MAX_RETRIES", "3")
    queue_id = add(db)

    assert db.schedule_queue_retry(queue_id, 1, "boom") is True
    assert db.schedule_queue_retry(queue_id, 1, "boom") is True
    # Third failure exhausts the budget: the caller must mark it failed.
    assert db.schedule_queue_retry(queue_id, 1, "boom") is False

    row = db.get_queue()[0]
    assert row["attempts"] == 3
    assert row["last_error"] == "boom"


def test_per_item_max_attempts_overrides_the_default(db, monkeypatch):
    monkeypatch.setenv("ANIWORLD_MAX_RETRIES", "10")
    queue_id = add(db, max_attempts=1)

    assert db.schedule_queue_retry(queue_id, 1, "boom") is False


def test_backoff_grows_and_is_capped(db, monkeypatch):
    monkeypatch.setenv("ANIWORLD_RETRY_BACKOFF_BASE", "60")

    assert db.retry_backoff_seconds(1) == 60
    assert db.retry_backoff_seconds(2) == 120
    assert db.retry_backoff_seconds(3) == 240
    assert db.retry_backoff_seconds(20) == 3600


def test_backoff_falls_back_on_a_nonsense_value(db, monkeypatch):
    monkeypatch.setenv("ANIWORLD_RETRY_BACKOFF_BASE", "not a number")

    assert db.retry_backoff_seconds(1) == 60


def test_an_explicit_retry_clears_the_automatic_retry_state(db):
    queue_id = add(db)
    db.set_queue_status(queue_id, "downloading")
    db.schedule_queue_retry(queue_id, 3600, "boom")
    db.set_queue_status(queue_id, "failed", last_error="boom")

    assert db.requeue_item(queue_id) is True

    row = db.get_queue()[0]
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert row["next_attempt_at"] is None
    assert db.get_next_queued()["id"] == queue_id


def test_a_completed_item_can_be_retried(db):
    queue_id = add(db)
    db.set_queue_status(queue_id, "imported")

    assert db.requeue_item(queue_id) is True


# --------------------------------------------------------------------------- #
# Active state
# --------------------------------------------------------------------------- #


def test_verifying_counts_as_running(db):
    queue_id = add(db)
    db.set_queue_status(queue_id, "verifying")

    assert db.get_running()["id"] == queue_id
    assert db.count_running() == 1


def test_a_verifying_item_can_be_cancelled(db):
    queue_id = add(db)
    db.set_queue_status(queue_id, "verifying")

    assert db.cancel_queue_item(queue_id) == (True, None)


def test_a_queued_item_cannot_be_cancelled(db):
    queue_id = add(db)

    ok, error = db.cancel_queue_item(queue_id)

    assert ok is False
    assert "running" in error


def test_duplicate_detection_covers_the_active_states(db):
    queue_id = add(db, url="https://example.com/dup")
    db.set_queue_status(queue_id, "verifying")

    assert db.is_series_queued_or_running("https://example.com/dup") is True

    db.set_queue_status(queue_id, "imported")
    assert db.is_series_queued_or_running("https://example.com/dup") is False


def test_media_type_and_import_status_round_trip(db):
    queue_id = add(db)

    db.set_queue_media_type(queue_id, "movie")
    db.set_queue_import_status(queue_id, "imported")

    row = db.get_queue()[0]
    assert row["media_type"] == "movie"
    assert row["import_status"] == "imported"


def test_terminal_status_records_completed_at_and_error(db):
    queue_id = add(db)

    db.set_queue_status(queue_id, "failed", last_error="   too    many   spaces  ")

    row = db.get_queue()[0]
    assert row["completed_at"] is not None
    assert row["last_error"] == "too many spaces"


def test_clear_completed_removes_every_terminal_status(db):
    """'imported' used to survive the clear, so successful items piled up."""
    for status in db.TERMINAL_STATUSES:
        db.set_queue_status(add(db, title=status), status)
    queued = add(db, title="still queued")
    running = add(db, title="running")
    db.set_queue_status(running, "downloading")

    db.clear_completed()

    assert sorted(item["id"] for item in db.get_queue()) == sorted([queued, running])
