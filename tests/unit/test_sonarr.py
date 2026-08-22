"""Sonarr adapter against a fake transport.

These assert on the exact wire format, because the parts that are easy to get
wrong are invisible until a real import silently does the wrong thing: the GET
that must not carry a seriesId, the ManualImport command body, and the episode
id we resolve ourselves when Sonarr's parser gives up.
"""

import pytest
from conftest import FakeResponse, attach

from aniworld.integrations.base import IntegrationError
from aniworld.integrations.sonarr import SonarrClient

SERIES = {
    "id": 7,
    "title": "Highschool DxD",
    "sortTitle": "highschool dxd",
    "tvdbId": 252083,
    "imdbId": "tt2230051",
    "alternateTitles": [{"title": "High School DxD"}],
}

CANDIDATE = {
    "id": 1,
    "path": "/data/downloads/aniworld/completed/Highschool DxD/Season 01/Highschool DxD S01E01.mkv",
    "name": "Highschool DxD S01E01.mkv",
    "folderName": "Season 01",
    "seasonNumber": 1,
    "episodes": [{"id": 4242, "episodeNumber": 1}],
    "quality": {"quality": {"id": 4, "name": "HDTV-720p"}},
    "languages": [{"id": 4, "name": "German"}],
    "rejections": [],
}


def make_client(handlers, path_map="/media:/data"):
    import os

    os.environ["ANIWORLD_ARR_PATH_MAP"] = path_map
    client = SonarrClient("http://sonarr:8989", "key123")
    session = attach(client, handlers)
    return client, session


def base_handlers(candidates=None, command_status="completed"):
    return {
        ("GET", "/api/v3/series"): FakeResponse(200, [SERIES]),
        ("GET", "/api/v3/manualimport"): FakeResponse(
            200, [CANDIDATE] if candidates is None else candidates
        ),
        ("POST", "/api/v3/manualimport"): lambda params, json, headers: FakeResponse(
            200, json
        ),
        ("POST", "/api/v3/command"): FakeResponse(200, {"id": 99, "status": "queued"}),
        ("GET", "/api/v3/command/99"): FakeResponse(
            200, {"id": 99, "status": command_status}
        ),
    }


def test_import_sends_manualimport_command_with_mapped_path():
    client, session = make_client(base_handlers())

    result = client.import_file(
        "/media/downloads/aniworld/completed/Highschool DxD/Season 01/Highschool DxD S01E01.mkv",
        root="/media/downloads/aniworld/completed",
        hints={"title": "Highschool DxD", "season": 1, "episode": 1},
    )

    assert result["ok"] is True
    assert result["episode_ids"] == [4242]

    body = session.last_call_to("POST", "/api/v3/command")["json"]
    assert body["name"] == "ManualImport"
    assert body["importMode"] == "Move"
    assert len(body["files"]) == 1
    imported = body["files"][0]
    assert imported["seriesId"] == 7
    assert imported["episodeIds"] == [4242]
    assert imported["path"].startswith("/data/")


def test_manualimport_get_never_carries_series_id():
    """Sonarr ignores `folder` when seriesId is present and returns the wrong files."""
    client, session = make_client(base_handlers())

    client.import_file(
        "/media/downloads/aniworld/completed/Highschool DxD/Season 01/Highschool DxD S01E01.mkv",
        root="/media/downloads/aniworld/completed",
        hints={"title": "Highschool DxD", "season": 1, "episode": 1},
    )

    params = session.calls_to("GET", "/api/v3/manualimport")[0]["params"]
    assert set(params) == {"folder", "filterExistingFiles"}
    assert (
        params["folder"]
        == "/data/downloads/aniworld/completed/Highschool DxD/Season 01"
    )


def test_episode_id_resolved_when_sonarr_cannot_parse_the_name():
    unparsed = dict(CANDIDATE, episodes=[], seasonNumber=None)
    handlers = base_handlers(candidates=[unparsed])
    handlers[("GET", "/api/v3/episode")] = FakeResponse(
        200, [{"id": 111, "episodeNumber": 1}, {"id": 112, "episodeNumber": 2}]
    )
    client, session = make_client(handlers)

    result = client.import_file(
        "/media/downloads/aniworld/completed/Highschool DxD/Season 01/Highschool DxD S01E01.mkv",
        root="/media/downloads/aniworld/completed",
        hints={"title": "Highschool DxD", "season": 1, "episode": 1},
    )

    assert result["ok"] is True
    assert result["episode_ids"] == [111]
    lookup = session.last_call_to("GET", "/api/v3/episode")["params"]
    assert lookup == {"seriesId": 7, "seasonNumber": 1}


def test_unknown_series_is_reported_not_raised():
    handlers = base_handlers()
    handlers[("GET", "/api/v3/series")] = FakeResponse(200, [])
    client, _ = make_client(handlers)

    result = client.import_file(
        "/media/x/f.mkv",
        root="/media/x",
        hints={"title": "Nope", "season": 1, "episode": 1},
    )

    assert result["ok"] is False
    assert result["reason"] == "series_not_in_sonarr"


def test_file_missing_from_sonarrs_view_is_reported():
    handlers = base_handlers(candidates=[])
    client, _ = make_client(handlers)

    result = client.import_file(
        "/media/downloads/aniworld/completed/Highschool DxD/Season 01/Highschool DxD S01E01.mkv",
        root="/media/downloads/aniworld/completed",
        hints={"title": "Highschool DxD", "season": 1, "episode": 1},
    )

    assert result["ok"] is False
    assert result["reason"] == "file_not_visible_to_sonarr"
    assert "ANIWORLD_ARR_PATH_MAP" in result["detail"]


def test_failed_command_is_reported():
    client, _ = make_client(base_handlers(command_status="failed"))

    result = client.import_file(
        "/media/downloads/aniworld/completed/Highschool DxD/Season 01/Highschool DxD S01E01.mkv",
        root="/media/downloads/aniworld/completed",
        hints={"title": "Highschool DxD", "season": 1, "episode": 1},
    )

    assert result["ok"] is False
    assert result["reason"] == "command_failed"


def test_find_series_matches_by_tvdb_id_over_title():
    other = dict(
        SERIES,
        id=9,
        title="Something Else",
        sortTitle="something else",
        tvdbId=1,
        alternateTitles=[],
    )
    client, _ = make_client(
        {("GET", "/api/v3/series"): FakeResponse(200, [other, SERIES])}
    )

    assert client.find_series(tvdb_id=252083)["id"] == 7
    assert client.find_series(title="High School DxD")["id"] == 7
    assert client.find_series(title="unknown show") is None


def test_refresh_uses_the_plural_series_ids_field():
    client, session = make_client(
        {
            ("POST", "/api/v3/command"): FakeResponse(
                200, {"id": 1, "status": "queued"}
            ),
            ("GET", "/api/v3/command/1"): FakeResponse(
                200, {"id": 1, "status": "completed"}
            ),
        }
    )

    client.refresh_series(7)

    body = session.last_call_to("POST", "/api/v3/command")["json"]
    assert body == {"name": "RefreshSeries", "seriesIds": [7]}


def test_rescan_uses_the_singular_series_id_field():
    client, session = make_client(
        {
            ("POST", "/api/v3/command"): FakeResponse(
                200, {"id": 1, "status": "queued"}
            ),
            ("GET", "/api/v3/command/1"): FakeResponse(
                200, {"id": 1, "status": "completed"}
            ),
        }
    )

    client.rescan_series(7)

    assert session.last_call_to("POST", "/api/v3/command")["json"] == {
        "name": "RescanSeries",
        "seriesId": 7,
    }


def test_api_key_is_sent_as_header():
    client, session = make_client({("GET", "/api/v3/series"): FakeResponse(200, [])})

    client.list_series()

    assert session.calls[0]["headers"]["X-Api-Key"] == "key123"


def test_bad_api_key_raises_immediately():
    client, session = make_client(
        {("GET", "/api/v3/series"): FakeResponse(401, text="nope")}
    )

    with pytest.raises(IntegrationError, match="rejected the API key"):
        client.list_series()

    assert len(session.calls) == 1, "a 401 must not be retried"
