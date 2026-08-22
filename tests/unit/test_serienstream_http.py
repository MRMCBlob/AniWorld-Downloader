"""Offline tests for SerienStream endpoint failover and URL consistency."""

from urllib.parse import urlsplit

import pytest

from aniworld.models.s_to import http
from aniworld.models.s_to import series as series_module
from aniworld.models.s_to.series import SerienstreamSeries
from aniworld.search import query_s_to


class Response:
    def __init__(self, url, status=200):
        self.url = url
        self.status_code = status
        self.text = "ok"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    def __init__(self, failing=()):
        self.failing = set(failing)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        parsed = urlsplit(url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}"
        if endpoint in self.failing:
            raise OSError(f"unreachable: {endpoint}")
        return Response(url)


@pytest.fixture(autouse=True)
def reset_endpoint(monkeypatch):
    monkeypatch.delenv("ANIWORLD_STO_ENDPOINTS", raising=False)
    monkeypatch.setattr(http, "_active_endpoint", None)


def test_direct_ip_is_primary_and_serienstream_to_is_remembered_as_backup():
    session = Session(failing={"http://186.2.175.5"})

    response = http.sto_get("https://s.to/serie/example", session=session)

    assert response.url == "https://serienstream.to/serie/example"
    assert [url for url, _ in session.calls] == [
        "http://186.2.175.5/serie/example",
        "https://serienstream.to/serie/example",
    ]

    session.calls.clear()
    session.failing.clear()
    http.sto_get("https://serienstream.to/serie/next", session=session)
    assert session.calls[0][0] == "https://serienstream.to/serie/next"


def test_direct_ip_primary_uses_http_not_https():
    session = Session()

    response = http.sto_get("https://serienstream.to/serie/example", session=session)

    assert response.url == "http://186.2.175.5/serie/example"
    assert session.calls[0][0].startswith("http://186.2.175.5/")
    assert "verify" not in session.calls[0][1]
    assert session.calls[0][1]["headers"]["Accept-Encoding"] == "gzip, deflate"


def test_caller_can_override_accept_encoding():
    session = Session()

    http.sto_get(
        "https://serienstream.to/serie/example",
        session=session,
        headers={"accept-encoding": "identity"},
    )

    assert session.calls[0][1]["headers"] == {"accept-encoding": "identity"}


def test_cx_remains_the_last_fallback():
    session = Session(failing=set(http.DEFAULT_STO_ENDPOINTS[:2]))

    response = http.sto_get("https://serienstream.to/serie/example", session=session)

    assert response.url == "https://serienstream.cx/serie/example"


def test_active_origin_rewrites_both_scheme_and_host(monkeypatch):
    monkeypatch.setattr(http, "_active_endpoint", "http://186.2.175.5")

    assert (
        http.sto_rewrite("https://s.to/serie/example?season=1#episodes")
        == "http://186.2.175.5/serie/example?season=1#episodes"
    )
    assert http.sto_url("/r?t=token") == "http://186.2.175.5/r?t=token"


def test_endpoints_can_be_changed_without_rebuilding(monkeypatch):
    monkeypatch.setenv(
        "ANIWORLD_STO_ENDPOINTS",
        "not-a-url, https://sto.internal.example/, https://sto.internal.example",
    )

    assert http.sto_endpoints() == ("https://sto.internal.example",)


def test_serienstream_models_store_one_consistent_origin(monkeypatch):
    monkeypatch.setattr(http, "_active_endpoint", "https://serienstream.cx")

    series = SerienstreamSeries("https://s.to/serie/example")

    assert series.url == "https://serienstream.cx/serie/example"


def test_model_tracks_an_endpoint_selected_during_its_first_fetch(monkeypatch):
    class Page:
        text = "<html></html>"

    def switch_to_fallback(url):
        http._active_endpoint = "https://serienstream.cx"
        return Page()

    monkeypatch.setattr(series_module, "sto_get", switch_to_fallback)
    series = SerienstreamSeries("https://serienstream.to/serie/example")

    assert series._html == "<html></html>"
    assert series.url == "https://serienstream.cx/serie/example"


def test_search_uses_the_live_json_api_headers(monkeypatch):
    seen = {}

    class SearchResponse:
        def json(self):
            return {
                "shows": [{"name": "Reacher", "url": "/serie/reacher"}],
                "people": [],
                "genres": [],
            }

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return SearchResponse()

    monkeypatch.setattr(http, "sto_get", fake_get)

    assert query_s_to("reacher") == [
        {"title": "Reacher", "link": "/serie/reacher"}
    ]
    assert seen["url"].endswith("/api/search/suggest")
    assert seen["params"] == {"term": "reacher"}
    assert seen["headers"]["Accept"] == "application/json"
    assert seen["headers"]["X-Requested-With"] == "XMLHttpRequest"
