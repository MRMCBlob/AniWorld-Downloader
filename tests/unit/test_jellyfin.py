"""Jellyfin adapter against a fake transport."""

from conftest import FakeResponse, attach

from aniworld.integrations.jellyfin import JellyfinClient

LIBRARIES = [
    {"Name": "Movies", "ItemId": "aaa", "Locations": ["/data/Movies"]},
    {"Name": "Shows", "ItemId": "bbb", "Locations": ["/data/TV", "/data/Anime"]},
]


def make_client(handlers):
    client = JellyfinClient("http://jellyfin:8096", "jkey")
    return client, attach(client, handlers)


def test_auth_uses_the_mediabrowser_authorization_header():
    client, session = make_client(
        {("GET", "/System/Info"): FakeResponse(200, {"Version": "10.11"})}
    )

    client.system_info()

    headers = session.calls[0]["headers"]
    assert headers["Authorization"] == 'MediaBrowser Token="jkey"'
    assert headers["X-Emby-Token"] == "jkey"


def test_refresh_for_path_targets_the_matching_library():
    client, session = make_client(
        {
            ("GET", "/Library/VirtualFolders"): FakeResponse(200, LIBRARIES),
            ("POST", "/Items/bbb/Refresh"): FakeResponse(204),
        }
    )

    result = client.refresh_for_path("/data/Anime/Some Show/Season 01")

    assert result == {"ok": True, "scope": "item", "item_id": "bbb"}
    assert session.calls_to("POST", "/Items/bbb/Refresh")


def test_refresh_for_path_falls_back_to_a_full_scan():
    client, session = make_client(
        {
            ("GET", "/Library/VirtualFolders"): FakeResponse(200, LIBRARIES),
            ("POST", "/Library/Refresh"): FakeResponse(204),
        }
    )

    result = client.refresh_for_path("/somewhere/else/file.mkv")

    assert result == {"ok": True, "scope": "all"}
    assert session.calls_to("POST", "/Library/Refresh")


def test_longest_matching_location_wins():
    libraries = [
        {"Name": "All", "ItemId": "root", "Locations": ["/data"]},
        {"Name": "Anime", "ItemId": "anime", "Locations": ["/data/Anime"]},
    ]
    client, _ = make_client(
        {("GET", "/Library/VirtualFolders"): FakeResponse(200, libraries)}
    )

    assert client.find_library_for_path("/data/Anime/Show")["ItemId"] == "anime"
    assert client.find_library_for_path("/data/Other/Show")["ItemId"] == "root"


def test_a_location_prefix_does_not_match_a_sibling_directory():
    libraries = [{"Name": "TV", "ItemId": "tv", "Locations": ["/data/TV"]}]
    client, _ = make_client(
        {("GET", "/Library/VirtualFolders"): FakeResponse(200, libraries)}
    )

    assert client.find_library_for_path("/data/TVShows/Some Show") is None


def test_unreachable_server_is_reported_not_raised():
    client, _ = make_client({})  # every request 404s

    status = client.ping()

    assert status["configured"] is True
    assert status["reachable"] is False
    assert "error" in status


def test_unconfigured_client_reports_cleanly():
    client = JellyfinClient("", "")

    assert client.ping() == {"configured": False, "reachable": False}
    assert client.enabled is False
