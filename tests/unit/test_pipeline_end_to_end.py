"""The whole chain, with only the network faked.

Every unit below this is tested in isolation; this exercises them wired
together, because the interesting failures live in the seams — a queue status
that never advances, a path that arrives at Sonarr in the wrong namespace, an
event that fires for a download nobody imported.

Real components throughout: the real queue database, the real post-processing
pipeline, the real Sonarr adapter and the real event bus. Only the HTTP
transport and the download itself are substituted.
"""

import json

import pytest
from conftest import FakeResponse, attach

from aniworld import events, postprocess
from aniworld.integrations import webhooks
from aniworld.integrations.sonarr import SonarrClient

SERIES = {
    "id": 7,
    "title": "Example",
    "sortTitle": "example",
    "tvdbId": 12345,
    "imdbId": "tt1234567",
    "path": "/data/TV/Example",
    "alternateTitles": [],
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    from aniworld.web import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "aniworld.db")
    monkeypatch.setattr(db_module, "ANIWORLD_CONFIG_DIR", tmp_path)
    db_module.init_queue_db()
    db_module.init_webhook_db()
    return db_module


@pytest.fixture
def roots(tmp_path, monkeypatch):
    incomplete = tmp_path / "media" / "downloads" / "aniworld" / "incomplete"
    completed = tmp_path / "media" / "downloads" / "aniworld" / "completed"
    incomplete.mkdir(parents=True)
    monkeypatch.setenv("ANIWORLD_COMPLETED_PATH", str(completed))
    monkeypatch.setenv("ANIWORLD_DOWNLOAD_PATH", str(incomplete))
    # ffprobe is not guaranteed in CI; the size floor still applies.
    monkeypatch.setattr(postprocess.shutil, "which", lambda _: None)
    return incomplete, completed


@pytest.fixture(autouse=True)
def clean_bus():
    events.clear()
    yield
    events.clear()


class Series:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Episode:
    """A downloaded episode, shaped like the real site models."""

    def __init__(self, path, root):
        self._episode_path = path
        self.selected_path = str(root)
        self.season = Series(season_number=1, are_movies=False)
        self.series = Series(title="Example", release_year=2012, imdb="tt1234567")
        self.episode_number = 1
        self.is_movie = False


def write_episode(incomplete):
    folder = incomplete / "Example (2012)" / "Season 01"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "Example S01E01.mkv"
    path.write_bytes(b"\0" * (postprocess.MIN_MEDIA_BYTES * 2))
    return path


def sonarr_handlers(completed_root, command_status="completed"):
    """Sonarr sees the same tree at /data instead of our temp path."""
    mapped = "/data/completed/Example (2012)/Season 01/Example S01E01.mkv"
    return {
        ("GET", "/api/v3/series"): FakeResponse(200, [SERIES]),
        ("GET", "/api/v3/manualimport"): FakeResponse(
            200,
            [
                {
                    "id": 1,
                    "path": mapped,
                    "name": "Example S01E01.mkv",
                    "folderName": "Season 01",
                    "seasonNumber": 1,
                    "episodes": [{"id": 999, "episodeNumber": 1}],
                    "quality": {"quality": {"id": 4, "name": "HDTV-720p"}},
                    "languages": [{"id": 4, "name": "German"}],
                    "rejections": [],
                }
            ],
        ),
        ("POST", "/api/v3/manualimport"): lambda params, json, headers: FakeResponse(
            200, json
        ),
        ("POST", "/api/v3/command"): FakeResponse(200, {"id": 42, "status": "queued"}),
        ("GET", "/api/v3/command/42"): FakeResponse(
            200, {"id": 42, "status": command_status}
        ),
    }


@pytest.fixture
def sonarr(monkeypatch, roots):
    """A wired-up Sonarr adapter, with the path map the deployment would need."""
    _, completed = roots
    monkeypatch.setenv("ANIWORLD_ARR_PATH_MAP", f"{completed}:/data/completed")

    def make(command_status="completed"):
        client = SonarrClient("http://sonarr:8989", "key")
        session = attach(client, sonarr_handlers(completed, command_status))
        monkeypatch.setattr("aniworld.integrations.get_sonarr", lambda: client)
        monkeypatch.setattr(
            "aniworld.integrations.get_client_for",
            lambda media_type: client if media_type == "series" else None,
        )
        return client, session

    return make


@pytest.fixture
def no_jellyfin(monkeypatch):
    monkeypatch.setattr(
        postprocess, "trigger_jellyfin_scan", lambda r: {"ok": False, "reason": "off"}
    )


# --------------------------------------------------------------------------- #


def test_a_download_reaches_sonarr_with_a_mapped_path(roots, sonarr, no_jellyfin):
    incomplete, completed = roots
    _client, session = sonarr()
    episode = Episode(write_episode(incomplete), incomplete)

    result = postprocess.finalize(episode, queue_id=1)

    assert result.ok is True
    assert result.imported is True
    assert result.media_type == "series"

    # The file physically moved out of incomplete...
    assert not (incomplete / "Example (2012)").exists()
    staged = completed / "Example (2012)" / "Season 01" / "Example S01E01.mkv"
    assert staged.exists()

    # ...and Sonarr was told about it in ITS namespace, not ours.
    scanned = session.calls_to("GET", "/api/v3/manualimport")[0]["params"]["folder"]
    assert scanned == "/data/completed/Example (2012)/Season 01"

    imported = session.last_call_to("POST", "/api/v3/command")["json"]["files"][0]
    assert imported["seriesId"] == 7
    assert imported["episodeIds"] == [999]
    assert imported["path"].startswith("/data/completed/")


def test_postprocess_keeps_the_v5_running_state_and_records_import_metadata(
    db, roots, sonarr, no_jellyfin
):
    from aniworld.web import worker

    incomplete, _ = roots
    sonarr()
    queue_id = db.add_to_queue(
        "Example", "https://example.com/s", ["e1"], "German Dub", "VOE"
    )
    db.set_queue_status(queue_id, "running")
    episode = Episode(write_episode(incomplete), incomplete)

    assert worker._run_postprocess({"id": queue_id}, episode) is True

    row = db.get_queue()[0]
    assert row["status"] == "running"
    assert row["media_type"] == "series"
    assert row["import_status"] == "imported"


def test_events_fire_in_order_and_reach_the_webhook_outbox(
    db, roots, sonarr, no_jellyfin, monkeypatch
):
    incomplete, _ = roots
    sonarr()
    monkeypatch.setenv("ANIWORLD_WEBHOOK_URLS", "https://hook.example/aniworld")
    events.subscribe(webhooks.on_event)

    episode = Episode(write_episode(incomplete), incomplete)
    postprocess.finalize(episode, queue_id=5)

    rows = db.get_due_webhooks()
    assert [row["event"] for row in rows] == [
        "download_completed",
        "import_completed",
    ]

    payload = json.loads(rows[1]["payload"])
    assert payload["type"] == "series"
    assert payload["title"] == "Example"
    assert payload["season"] == 1
    assert payload["episode"] == 1
    assert payload["queue_id"] == 5
    assert payload["path"].endswith("Example S01E01.mkv")


def test_a_refused_import_leaves_the_file_safe_and_the_item_completed(
    db, roots, sonarr, no_jellyfin
):
    """Sonarr does not know the series. The download must not be lost."""
    from aniworld.web import worker

    incomplete, completed = roots
    _client, session = sonarr()
    session.handlers[("GET", "/api/v3/series")] = FakeResponse(200, [])

    queue_id = db.add_to_queue(
        "Example", "https://example.com/s", ["e1"], "German Dub", "VOE"
    )
    episode = Episode(write_episode(incomplete), incomplete)

    imported = worker._run_postprocess({"id": queue_id}, episode)

    assert imported is False
    assert (completed / "Example (2012)" / "Season 01" / "Example S01E01.mkv").exists()
    assert db.get_queue()[0]["import_status"] == "series_not_in_sonarr"


def test_a_failing_arr_command_does_not_lose_the_download(roots, sonarr, no_jellyfin):
    incomplete, completed = roots
    sonarr(command_status="failed")
    episode = Episode(write_episode(incomplete), incomplete)

    result = postprocess.finalize(episode)

    assert result.ok is True, "the download itself succeeded"
    assert result.imported is False
    assert result.status == "completed"
    assert (completed / "Example (2012)" / "Season 01" / "Example S01E01.mkv").exists()


def test_a_corrupt_download_is_never_handed_to_sonarr(db, roots, sonarr, no_jellyfin):
    from aniworld.web import worker

    incomplete, completed = roots
    _client, session = sonarr()

    folder = incomplete / "Example (2012)" / "Season 01"
    folder.mkdir(parents=True)
    truncated = folder / "Example S01E01.mkv"
    truncated.write_bytes(b"<html>404</html>")

    queue_id = db.add_to_queue(
        "Example", "https://example.com/s", ["e1"], "German Dub", "VOE"
    )
    episode = Episode(truncated, incomplete)

    with pytest.raises(RuntimeError, match="verification failed"):
        worker._run_postprocess({"id": queue_id}, episode)

    assert session.calls_to("POST", "/api/v3/command") == []
    assert not completed.exists() or not any(completed.rglob("*.mkv"))
    assert truncated.exists(), "kept for inspection rather than silently deleted"


def test_an_all_failed_item_is_retried_before_being_marked_failed(db, monkeypatch):
    from aniworld.web import worker

    monkeypatch.setenv("ANIWORLD_MAX_RETRIES", "2")
    queue_id = db.add_to_queue(
        "Example", "https://example.com/s", ["e1"], "German Dub", "VOE"
    )
    db.set_queue_status(queue_id, "running")
    item = db.get_queue()[0]
    errors = [{"url": "e1", "error": "hoster down"}]

    worker._finish_queue_item(item, ["e1"], errors, 0)

    row = db.get_queue()[0]
    assert row["status"] == "queued", "first failure retries"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None

    # Second failure exhausts the budget.
    worker._finish_queue_item(db.get_queue()[0], ["e1"], errors, 0)
    assert db.get_queue()[0]["status"] == "failed"


def test_a_partial_success_is_not_retried(db):
    """Retrying would redownload the episodes that already worked."""
    from aniworld.web import worker

    queue_id = db.add_to_queue(
        "Example", "https://example.com/s", ["e1", "e2"], "German Dub", "VOE"
    )
    db.set_queue_status(queue_id, "running")
    errors = [{"url": "e2", "error": "hoster down"}]

    worker._finish_queue_item(db.get_queue()[0], ["e1", "e2"], errors, 1)

    row = db.get_queue()[0]
    assert row["status"] == "completed"
    assert row["attempts"] == 0


def test_a_fully_imported_item_ends_completed_with_import_metadata(db):
    from aniworld.web import worker

    queue_id = db.add_to_queue(
        "Example", "https://example.com/s", ["e1", "e2"], "German Dub", "VOE"
    )
    db.set_queue_status(queue_id, "running")

    worker._finish_queue_item(db.get_queue()[0], ["e1", "e2"], [], 2)

    row = db.get_queue()[0]
    assert row["status"] == "completed"
    assert row["import_status"] == "imported"


def test_a_restart_hands_active_items_back_to_the_queue(db):
    """The container restarting must not strand work."""
    running_a = db.add_to_queue(
        "A", "https://example.com/a", ["e1"], "German Dub", "VOE"
    )
    running_b = db.add_to_queue(
        "B", "https://example.com/b", ["e1"], "German Dub", "VOE"
    )
    finished = db.add_to_queue(
        "C", "https://example.com/c", ["e1"], "German Dub", "VOE"
    )
    db.set_queue_status(running_a, "running")
    db.set_queue_status(running_b, "running")
    db.set_queue_status(finished, "completed")
    db.set_queue_import_status(finished, "imported")
    db.schedule_queue_retry(running_a, 3600, "boom")
    db.set_queue_status(running_a, "running")

    db.reset_stale_running()

    rows = {row["title"]: row for row in db.get_queue()}
    assert rows["A"]["status"] == "queued"
    assert rows["A"]["next_attempt_at"] is None, "a restart is not a failed attempt"
    assert rows["B"]["status"] == "queued"
    assert rows["C"]["status"] == "completed", "finished items are left alone"
    assert rows["C"]["import_status"] == "imported"
    assert db.get_next_queued() is not None
