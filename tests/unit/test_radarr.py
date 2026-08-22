"""Radarr adapter against a fake transport."""

import os

from conftest import FakeResponse, attach

from aniworld.integrations.radarr import RadarrClient

MOVIE = {
    "id": 3,
    "title": "Spirited Away",
    "originalTitle": "Sen to Chihiro no Kamikakushi",
    "year": 2001,
    "tmdbId": 129,
    "imdbId": "tt0245429",
}

CANDIDATE = {
    "id": 1,
    "path": "/data/downloads/aniworld/completed/Spirited Away (2001)/Spirited Away (2001).mkv",
    "name": "Spirited Away (2001).mkv",
    "folderName": "Spirited Away (2001)",
    "quality": {"quality": {"id": 7, "name": "Bluray-1080p"}},
    "languages": [{"id": 8, "name": "Japanese"}],
    "rejections": [],
}

FILE = (
    "/media/downloads/aniworld/completed/Spirited Away (2001)/Spirited Away (2001).mkv"
)
ROOT = "/media/downloads/aniworld/completed"


def make_client(handlers, path_map="/media:/data"):
    os.environ["ANIWORLD_ARR_PATH_MAP"] = path_map
    client = RadarrClient("http://radarr:7878", "key456")
    return client, attach(client, handlers)


def base_handlers(candidates=None, command_status="completed"):
    return {
        ("GET", "/api/v3/movie"): FakeResponse(200, [MOVIE]),
        ("GET", "/api/v3/manualimport"): FakeResponse(
            200, [CANDIDATE] if candidates is None else candidates
        ),
        ("POST", "/api/v3/manualimport"): lambda params, json, headers: FakeResponse(
            200, json
        ),
        ("POST", "/api/v3/command"): FakeResponse(200, {"id": 5, "status": "queued"}),
        ("GET", "/api/v3/command/5"): FakeResponse(
            200, {"id": 5, "status": command_status}
        ),
    }


def test_import_sends_movie_id_not_episode_ids():
    client, session = make_client(base_handlers())

    result = client.import_file(
        FILE, root=ROOT, hints={"title": "Spirited Away", "year": 2001}
    )

    assert result["ok"] is True
    imported = session.last_call_to("POST", "/api/v3/command")["json"]["files"][0]
    assert imported["movieId"] == 3
    assert "episodeIds" not in imported
    assert imported["path"].startswith("/data/")


def test_manualimport_get_never_carries_movie_id():
    client, session = make_client(base_handlers())

    client.import_file(FILE, root=ROOT, hints={"title": "Spirited Away", "year": 2001})

    params = session.calls_to("GET", "/api/v3/manualimport")[0]["params"]
    assert set(params) == {"folder", "filterExistingFiles"}


def test_year_disambiguates_movies_sharing_a_title():
    remake = dict(MOVIE, id=4, year=2024, tmdbId=999, imdbId="tt9999999")
    client, _ = make_client(
        {("GET", "/api/v3/movie"): FakeResponse(200, [MOVIE, remake])}
    )

    assert client.find_movie(title="Spirited Away", year=2024)["id"] == 4
    assert client.find_movie(title="Spirited Away", year=2001)["id"] == 3
    # Ambiguous without a year: refuse rather than import into the wrong movie.
    assert client.find_movie(title="Spirited Away") is None


def test_tmdb_id_wins_over_title():
    client, _ = make_client({("GET", "/api/v3/movie"): FakeResponse(200, [MOVIE])})

    assert client.find_movie(title="Totally Different", tmdb_id=129)["id"] == 3


def test_unknown_movie_is_reported_not_raised():
    handlers = base_handlers()
    handlers[("GET", "/api/v3/movie")] = FakeResponse(200, [])
    client, _ = make_client(handlers)

    result = client.import_file(FILE, root=ROOT, hints={"title": "Nope"})

    assert result["ok"] is False
    assert result["reason"] == "movie_not_in_radarr"


def test_copy_import_mode_is_honoured():
    os.environ["ANIWORLD_ARR_IMPORT_MODE"] = "copy"
    try:
        client, session = make_client(base_handlers())
        client.import_file(
            FILE, root=ROOT, hints={"title": "Spirited Away", "year": 2001}
        )
        assert (
            session.last_call_to("POST", "/api/v3/command")["json"]["importMode"]
            == "Copy"
        )
    finally:
        del os.environ["ANIWORLD_ARR_IMPORT_MODE"]


def test_rescan_uses_singular_movie_id_and_refresh_the_plural():
    handlers = {
        ("POST", "/api/v3/command"): FakeResponse(200, {"id": 5, "status": "queued"}),
        ("GET", "/api/v3/command/5"): FakeResponse(
            200, {"id": 5, "status": "completed"}
        ),
    }
    client, session = make_client(handlers)

    client.rescan_movie(3)
    assert session.last_call_to("POST", "/api/v3/command")["json"] == {
        "name": "RescanMovie",
        "movieId": 3,
    }

    client.refresh_movie(3)
    assert session.last_call_to("POST", "/api/v3/command")["json"] == {
        "name": "RefreshMovie",
        "movieIds": [3],
    }
