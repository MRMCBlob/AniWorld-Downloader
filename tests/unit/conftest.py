"""Shared fixtures for the offline unit tests.

Nothing here touches the network. The integration adapters are exercised
against a fake transport that records the requests it received, so the tests
assert on the exact payloads Sonarr, Radarr and Jellyfin would see.
"""

import os

import pytest


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None or text else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Stands in for niquests.Session.

    Handlers are looked up by ``(METHOD, path)``. A handler is either a
    FakeResponse or a callable receiving the keyword arguments of the request.
    """

    def __init__(self, handlers=None):
        self.handlers = dict(handlers or {})
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.split("://", 1)[-1]
        path = path[path.index("/") :] if "/" in path else "/"
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json": json,
                "headers": headers or {},
            }
        )
        handler = self.handlers.get((method, path))
        if handler is None:
            return FakeResponse(404, text=f"no handler for {method} {path}")
        if callable(handler):
            return handler(params=params, json=json, headers=headers)
        return handler

    def close(self):
        pass

    def calls_to(self, method, path):
        return [c for c in self.calls if c["method"] == method and c["path"] == path]

    def last_call_to(self, method, path):
        calls = self.calls_to(method, path)
        return calls[-1] if calls else None


def attach(client, handlers):
    """Give a client a FakeSession and return it for assertions."""
    session = FakeSession(handlers)
    client._session = session
    # Retries would multiply the recorded calls and slow the suite down; the
    # retry behaviour has its own test.
    client.retries = 1
    return session


@pytest.fixture(autouse=True)
def clean_integration_env(monkeypatch):
    """Stop a developer's real .env from leaking into the assertions."""
    for name in (
        "ANIWORLD_ARR_PATH_MAP",
        "ANIWORLD_ARR_IMPORT_MODE",
        "ANIWORLD_ARR_COMMAND_TIMEOUT",
        "ANIWORLD_DEFAULT_MEDIA_TYPE",
        "SONARR_AUTO_ADD",
        "SONARR_ROOT_FOLDER",
        "SONARR_QUALITY_PROFILE_ID",
        "RADARR_AUTO_ADD",
        "RADARR_ROOT_FOLDER",
        "RADARR_QUALITY_PROFILE_ID",
        "JELLYFIN_SCAN_ENABLED",
        "ANIWORLD_WEBHOOK_URLS",
        "ANIWORLD_WEBHOOK_SECRET",
        "ANIWORLD_COMPLETED_PATH",
        "ANIWORLD_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    os.environ.setdefault("ANIWORLD_DEBUG_MODE", "0")
