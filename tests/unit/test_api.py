"""REST API surface: status, the flat aliases, scans and API-key auth."""

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from aniworld.web import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "aniworld.db")
    monkeypatch.setattr(db_module, "ANIWORLD_CONFIG_DIR", tmp_path)
    return db_module


@pytest.fixture
def client(isolated_db, monkeypatch):
    """An app with auth off, so routes are reachable without a session."""
    from aniworld.web import app as app_module

    monkeypatch.setattr(app_module, "_ensure_queue_worker", lambda: None)
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def queue_one(isolated_db, **kwargs):
    return isolated_db.add_to_queue(
        "Show", "https://example.com/s", ["e1"], "German Dub", "VOE", **kwargs
    )


# --------------------------------------------------------------------------- #
# /api/status
# --------------------------------------------------------------------------- #


def test_status_reports_the_basics(client):
    payload = client.get("/api/status").get_json()

    assert payload["status"] == "ok"
    assert "version" in payload
    assert "worker_running" in payload
    assert payload["queue"]["total"] == 0


def test_status_counts_the_queue_by_state(client, isolated_db):
    a = queue_one(isolated_db)
    b = queue_one(isolated_db)
    isolated_db.set_queue_status(a, "running")
    isolated_db.set_queue_status(b, "completed")
    isolated_db.set_queue_import_status(b, "imported")

    queue = client.get("/api/status").get_json()["queue"]

    assert queue["total"] == 2
    assert queue["by_status"] == {"completed": 1, "running": 1}
    assert queue["active"] == 1


def test_status_never_echoes_the_api_key(client, monkeypatch):
    monkeypatch.setenv("ANIWORLD_API_KEY", "super-secret")

    body = client.get("/api/status").get_data(as_text=True)

    assert "super-secret" not in body


def test_status_includes_integrations_when_auth_is_off(client):
    payload = client.get("/api/status").get_json()

    assert set(payload["integrations"]) == {"sonarr", "radarr", "jellyfin"}
    assert payload["integrations"]["sonarr"]["configured"] is False


# --------------------------------------------------------------------------- #
# Queue actions
# --------------------------------------------------------------------------- #


def test_flat_aliases_hit_the_same_handlers(client, isolated_db):
    queue_id = queue_one(isolated_db)
    isolated_db.set_queue_status(queue_id, "failed")

    assert client.post(f"/api/retry/{queue_id}").status_code == 200
    assert isolated_db.get_queue()[0]["status"] == "queued"

    isolated_db.set_queue_status(queue_id, "running")
    assert client.post(f"/api/cancel/{queue_id}").status_code == 200
    row = isolated_db.get_queue()[0]
    assert row["status"] == "running"
    assert row["cancel_requested"] == 1


def test_pause_and_resume_aliases(client, isolated_db):
    queue_id = queue_one(isolated_db)

    assert client.post(f"/api/pause/{queue_id}").status_code == 200
    assert isolated_db.get_queue()[0]["status"] == "paused"

    assert client.post(f"/api/resume/{queue_id}").status_code == 200
    assert isolated_db.get_queue()[0]["status"] == "queued"


def test_cancelling_a_queued_item_is_supported(client, isolated_db):
    queue_id = queue_one(isolated_db)

    response = client.post(f"/api/cancel/{queue_id}")

    assert response.status_code == 200
    assert isolated_db.get_queue()[0]["status"] == "cancelled"


def test_priority_can_be_changed(client, isolated_db):
    queue_id = queue_one(isolated_db)

    response = client.post(f"/api/queue/{queue_id}/priority", json={"priority": 5})

    assert response.status_code == 200
    assert isolated_db.get_queue()[0]["priority"] == 5


def test_priority_rejects_a_non_integer(client, isolated_db):
    queue_id = queue_one(isolated_db)

    response = client.post(f"/api/queue/{queue_id}/priority", json={"priority": "high"})

    assert response.status_code == 400


def test_download_accepts_priority_and_media_type(client, isolated_db):
    response = client.post(
        "/api/download",
        json={
            "title": "Movie",
            "series_url": "https://example.com/m",
            "episodes": ["e1"],
            "language": "German Dub",
            "provider": "VOE",
            "priority": 3,
            "media_type": "movie",
        },
    )

    assert response.status_code == 200
    row = isolated_db.get_queue()[0]
    assert row["priority"] == 3
    assert row["media_type"] == "movie"


def test_download_rejects_an_unknown_media_type(client):
    response = client.post(
        "/api/download",
        json={
            "title": "X",
            "series_url": "https://example.com/x",
            "episodes": ["e1"],
            "language": "German Dub",
            "provider": "VOE",
            "media_type": "documentary",
        },
    )

    assert response.status_code == 400


def test_direct_download_requires_an_explicit_allowed_root(client, monkeypatch):
    monkeypatch.delenv("ANIWORLD_DIRECT_DOWNLOAD_ROOTS", raising=False)

    response = client.post(
        "/api/download",
        json={
            "episodes": [
                {
                    "url": "https://aniworld.to/anime/stream/show/staffel-1/episode-1",
                    "target_path": "/media/anime/Show/Season 01",
                    "sonarr_series_id": 7,
                }
            ]
        },
    )

    assert response.status_code == 400
    assert "ANIWORLD_DIRECT_DOWNLOAD_ROOTS" in response.get_json()["error"]


def test_direct_download_is_limited_to_the_allowed_tree(
    client, isolated_db, monkeypatch, tmp_path
):
    allowed = tmp_path / "anime"
    monkeypatch.setenv("ANIWORLD_DIRECT_DOWNLOAD_ROOTS", str(allowed))
    target = allowed / "Show" / "Season 01"

    response = client.post(
        "/api/download",
        json={
            "title": "Show",
            "media_type": "series",
            "episodes": [
                {
                    "url": "https://aniworld.to/anime/stream/show/staffel-1/episode-1",
                    "target_path": str(target),
                    "sonarr_series_id": "7",
                    "sonarr_episode_id": "42",
                }
            ],
        },
    )

    assert response.status_code == 200
    row = isolated_db.get_queue_item(response.get_json()["queue_id"])
    assert row["source"] == "sonarr"
    entry = __import__("json").loads(row["episodes"])[0]
    assert entry["target_path"] == str(target.resolve())
    assert entry["sonarr_series_id"] == 7
    assert entry["sonarr_episode_id"] == 42

    escaped = client.post(
        "/api/download",
        json={
            "episodes": [
                {
                    "url": "https://aniworld.to/anime/stream/show/staffel-1/episode-2",
                    "target_path": str(tmp_path / "outside"),
                }
            ]
        },
    )
    assert escaped.status_code == 400
    assert "outside the allowed roots" in escaped.get_json()["error"]


# --------------------------------------------------------------------------- #
# Scans
# --------------------------------------------------------------------------- #


def test_scan_endpoints_report_an_unconfigured_service(client):
    for path in ("/api/sonarr/scan", "/api/radarr/scan", "/api/jellyfin/scan"):
        response = client.post(path, json={})
        assert response.status_code == 503, path
        assert "not configured" in response.get_json()["error"]


def test_sonarr_scan_runs_refresh_and_rescan(client, monkeypatch):
    from aniworld import integrations

    calls = []

    class FakeSonarr:
        configured = True

        def refresh_series(self, series_id=None):
            calls.append(("refresh", series_id))
            return {"id": 1, "status": "completed"}

        def rescan_series(self, series_id=None):
            calls.append(("rescan", series_id))
            return {"id": 2, "status": "completed"}

    monkeypatch.setattr(integrations, "get_sonarr", lambda: FakeSonarr())

    response = client.post("/api/sonarr/scan", json={"series_id": 7})

    assert response.status_code == 200
    assert calls == [("refresh", 7), ("rescan", 7)]
    assert response.get_json()["refresh"]["status"] == "completed"


def test_scan_without_an_id_targets_the_whole_library(client, monkeypatch):
    from aniworld import integrations

    seen = []

    class FakeRadarr:
        configured = True

        def refresh_movie(self, movie_id=None):
            seen.append(movie_id)
            return {"id": 1, "status": "completed"}

        def rescan_movie(self, movie_id=None):
            seen.append(movie_id)
            return {"id": 2, "status": "completed"}

    monkeypatch.setattr(integrations, "get_radarr", lambda: FakeRadarr())

    assert client.post("/api/radarr/scan", json={}).status_code == 200
    assert seen == [None, None]


def test_scan_rejects_a_non_integer_id(client, monkeypatch):
    from aniworld import integrations

    class FakeSonarr:
        configured = True

    monkeypatch.setattr(integrations, "get_sonarr", lambda: FakeSonarr())

    response = client.post("/api/sonarr/scan", json={"series_id": "seven"})

    assert response.status_code == 400


def test_an_unreachable_service_becomes_a_502(client, monkeypatch):
    from aniworld import integrations
    from aniworld.integrations.base import IntegrationError

    class FakeSonarr:
        configured = True

        def refresh_series(self, series_id=None):
            raise IntegrationError("connection refused")

    monkeypatch.setattr(integrations, "get_sonarr", lambda: FakeSonarr())

    response = client.post("/api/sonarr/scan", json={})

    assert response.status_code == 502
    assert "connection refused" in response.get_json()["error"]


def test_sonarr_sync_endpoint_starts_one_series(client, monkeypatch):
    from aniworld.web import sonarr_sync_service

    calls = []
    monkeypatch.setattr(
        sonarr_sync_service,
        "trigger",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    response = client.post("/api/sonarr/sync", json={"series_id": 7})

    assert response.status_code == 202
    assert calls == [{"series_ids": [7], "reason": "connect", "apply": True}]


def test_sonarr_sync_endpoint_supports_a_dry_run(client, monkeypatch):
    from aniworld.web import sonarr_sync_service

    calls = []
    monkeypatch.setattr(
        sonarr_sync_service,
        "trigger",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    response = client.post("/api/sonarr/sync", json={"dry_run": True})

    assert response.status_code == 202
    assert calls == [{"series_ids": [], "reason": "api", "apply": False}]


def test_sonarr_sync_endpoint_rejects_bad_or_overlapping_requests(
    client, monkeypatch
):
    from aniworld.web import sonarr_sync_service

    assert (
        client.post("/api/sonarr/sync", json={"series_id": "seven"}).status_code
        == 400
    )
    monkeypatch.setattr(sonarr_sync_service, "trigger", lambda **kwargs: False)
    assert client.post("/api/sonarr/sync", json={}).status_code == 409


def test_sonarr_sync_status_is_exposed(client, monkeypatch):
    from aniworld.web import sonarr_sync_service

    monkeypatch.setattr(
        sonarr_sync_service,
        "status",
        lambda: {"enabled": True, "running": False, "next_run": "midnight"},
    )

    response = client.get("/api/sonarr/sync")

    assert response.status_code == 200
    assert response.get_json()["next_run"] == "midnight"


# --------------------------------------------------------------------------- #
# API-key auth
# --------------------------------------------------------------------------- #


def test_api_key_is_compared_safely_and_rejects_a_wrong_key(monkeypatch):
    from flask import Flask

    from aniworld.web import api_auth

    monkeypatch.setenv("ANIWORLD_API_KEY", "correct")
    app = Flask(__name__)

    with app.test_request_context(headers={"X-Api-Key": "correct"}):
        assert api_auth.has_valid_api_key() is True
    with app.test_request_context(headers={"X-Api-Key": "wrong"}):
        assert api_auth.has_valid_api_key() is False
    with app.test_request_context():
        assert api_auth.has_valid_api_key() is False


def test_no_configured_key_means_no_key_authentication(monkeypatch):
    from flask import Flask

    from aniworld.web import api_auth

    monkeypatch.delenv("ANIWORLD_API_KEY", raising=False)
    app = Flask(__name__)

    with app.test_request_context(headers={"X-Api-Key": ""}):
        assert api_auth.has_valid_api_key() is False
    # An unset key must never read as "everyone is authenticated".
    with app.test_request_context(headers={"X-Api-Key": "anything"}):
        assert api_auth.has_valid_api_key() is False


def test_the_key_may_also_arrive_as_a_query_parameter(monkeypatch):
    from flask import Flask

    from aniworld.web import api_auth

    monkeypatch.setenv("ANIWORLD_API_KEY", "correct")
    app = Flask(__name__)

    with app.test_request_context("/api/status?apikey=correct"):
        assert api_auth.has_valid_api_key() is True


def test_a_key_can_come_from_a_docker_secret_file(monkeypatch, tmp_path):
    from flask import Flask

    from aniworld.web import api_auth

    secret = tmp_path / "api_key"
    secret.write_text("file-key\n")
    monkeypatch.delenv("ANIWORLD_API_KEY", raising=False)
    monkeypatch.setenv("ANIWORLD_API_KEY_FILE", str(secret))
    app = Flask(__name__)

    with app.test_request_context(headers={"X-Api-Key": "file-key"}):
        assert api_auth.has_valid_api_key() is True


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #


def test_log_tail_returns_the_last_lines(client, monkeypatch, tmp_path):
    log = tmp_path / "aniworld.log"
    log.write_text("\n".join(f"line {i}" for i in range(500)))
    monkeypatch.setattr("aniworld.logger.get_log_file_path", lambda: str(log))

    payload = client.get("/api/logs?lines=10").get_json()

    assert len(payload["lines"]) == 10
    assert payload["lines"][-1] == "line 499"


def test_log_tail_copes_with_a_missing_file(client, monkeypatch):
    monkeypatch.setattr("aniworld.logger.get_log_file_path", lambda: None)

    assert client.get("/api/logs").get_json() == {"path": None, "lines": []}
