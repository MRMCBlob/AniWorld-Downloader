"""The nightly Sonarr plan is built without contacting either real service."""

from datetime import datetime, timedelta, timezone

from aniworld import sonarr_sync
from aniworld.web import sonarr_sync_service


NOW = datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc)


def test_missing_episodes_only_keeps_monitored_aired_gaps():
    series = {
        "monitored": True,
        "seasons": [
            {"seasonNumber": 0, "monitored": True},
            {"seasonNumber": 1, "monitored": True},
            {"seasonNumber": 2, "monitored": False},
        ],
    }
    episodes = [
        _sonarr_episode(1, 1, 1, hasFile=True),
        _sonarr_episode(2, 1, 2),
        _sonarr_episode(3, 1, 3, monitored=False),
        _sonarr_episode(4, 1, 4, airDateUtc=NOW + timedelta(days=1)),
        _sonarr_episode(5, 2, 1),
        _sonarr_episode(6, 0, 1),
    ]

    assert [item["id"] for item in sonarr_sync.missing_episodes(series, episodes, NOW)] == [2]


def test_season_folder_uses_sonarr_format_and_existing_directories(monkeypatch):
    monkeypatch.setenv("ANIWORLD_ARR_PATH_MAP", "/media:/data")
    series = {"title": "Show", "path": "/data/Anime/Show", "seasonFolder": True}
    known = sonarr_sync.known_season_directories(
        series,
        [
            {
                "seasonNumber": 1,
                "path": "/data/Anime/Show/Staffel 01/Show S01E01.mkv",
            }
        ],
    )

    assert known == {1: "/media/Anime/Show/Staffel 01"}
    assert (
        sonarr_sync.target_directory(
            series, 1, known, {"seasonFolderFormat": "Season {season:00}"}
        )
        == "/media/Anime/Show/Staffel 01"
    )
    assert (
        sonarr_sync.target_directory(
            series, 2, known, {"seasonFolderFormat": "Season {season:00}"}
        )
        == "/media/Anime/Show/Season 02"
    )


def test_build_plan_maps_a_missing_episode_to_its_final_folder(monkeypatch):
    monkeypatch.setenv("ANIWORLD_ARR_PATH_MAP", "/media:/data")
    series = {
        "id": 7,
        "title": "Frieren",
        "sortTitle": "frieren",
        "tvdbId": 123,
        "path": "/data/Anime/Frieren",
        "monitored": True,
        "seasonFolder": True,
        "seasons": [{"seasonNumber": 1, "monitored": True}],
    }

    class Sonarr:
        def list_series(self):
            return [series]

        def naming_config(self):
            return {"seasonFolderFormat": "Season {season:00}"}

        def wanted_missing(self):
            return [_sonarr_episode(42, 1, 2, seriesId=7)]

        def episode_files(self, series_id):
            return []

    class Downloader:
        def queue(self):
            return []

        def search(self, title, site="aniworld"):
            return [
                {
                    "title": "Frieren",
                    "url": "https://aniworld.to/anime/stream/frieren",
                }
            ]

        def seasons(self, series_url):
            return [
                {
                    "season_number": 1,
                    "url": f"{series_url}/staffel-1",
                    "are_movies": False,
                }
            ]

        def episodes(self, season_url, series_url):
            return [
                {
                    "episode_number": 2,
                    "url": f"{season_url}/episode-2",
                    "available_languages": ["German Dub"],
                }
            ]

    plan = sonarr_sync.build_plan(
        Sonarr(), Downloader(), {}, language="German Dub", now=NOW
    )

    assert len(plan) == 1
    assert plan[0]["episodes"] == [
        {
            "url": "https://aniworld.to/anime/stream/frieren/staffel-1/episode-2",
            "target_path": "/media/Anime/Frieren/Season 01",
            "sonarr_series_id": 7,
            "sonarr_episode_id": 42,
        }
    ]


def test_exact_title_falls_back_from_aniworld_to_serienstream():
    calls = []

    class Downloader:
        def search(self, title, site="aniworld"):
            calls.append((title, site))
            if site == "sto":
                return [
                    {
                        "title": "The Mentalist",
                        "url": "https://serienstream.to/serie/the-mentalist",
                    }
                ]
            return []

    url, reason = sonarr_sync.resolve_series_url(
        {"id": 52, "title": "The Mentalist"},
        {},
        Downloader(),
    )

    assert url == "https://serienstream.to/serie/the-mentalist"
    assert reason == "exact title (sto)"
    assert calls == [("The Mentalist", "aniworld"), ("The Mentalist", "sto")]


def test_sync_sites_reject_unsupported_direct_download_sources():
    assert sonarr_sync.normalize_sync_sites("aniworld,sto,aniworld") == (
        "aniworld",
        "sto",
    )

    try:
        sonarr_sync.normalize_sync_sites("aniworld,kinox")
    except sonarr_sync.SyncError as exc:
        assert "kinox" in str(exc)
    else:
        raise AssertionError("unsupported site was accepted")


def test_an_active_queue_entry_is_not_planned_again():
    entry = {"sonarr_episode_id": 42}
    assert sonarr_sync.active_sonarr_episode_ids(
        [{"status": "running", "episodes": __import__("json").dumps([entry])}]
    ) == {42}


def test_invalid_scheduler_cron_is_reported_without_breaking_status(monkeypatch):
    monkeypatch.setenv("ANIWORLD_SONARR_SYNC_ENABLED", "1")
    monkeypatch.setenv("ANIWORLD_SONARR_SYNC_CRON", "not a cron")

    status = sonarr_sync_service.status()

    assert status["enabled"] is True
    assert status["next_run"] is None
    assert status["schedule_error"]


def _sonarr_episode(identifier, season, number, **overrides):
    item = {
        "id": identifier,
        "seasonNumber": season,
        "episodeNumber": number,
        "monitored": True,
        "hasFile": False,
        "episodeFileId": 0,
        "airDateUtc": NOW - timedelta(days=1),
    }
    item.update(overrides)
    return item
