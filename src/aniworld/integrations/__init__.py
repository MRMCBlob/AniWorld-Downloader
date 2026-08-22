"""Adapters for the external services in a media homelab.

The download core never imports this package. Wiring happens one level up, in
the queue worker, so that Sonarr, Radarr and Jellyfin remain optional: with
nothing configured the downloader behaves exactly as it did before.

Clients are built from the environment and cached, because they hold an HTTP
session and are read on every finished episode. :func:`reset_clients` drops the
cache after a settings change.
"""

import threading

from ..logger import get_logger
from .base import (
    IntegrationError,
    IntegrationNotConfigured,
    map_path,
    parse_path_map,
    read_secret,
    unmap_path,
)
from .classify import MOVIE, SERIES, MediaInfo, classify
from .jellyfin import JellyfinClient
from .radarr import RadarrClient
from .sonarr import SonarrClient

logger = get_logger(__name__)

__all__ = [
    "MOVIE",
    "SERIES",
    "IntegrationError",
    "IntegrationNotConfigured",
    "JellyfinClient",
    "MediaInfo",
    "RadarrClient",
    "SonarrClient",
    "classify",
    "get_jellyfin",
    "get_radarr",
    "get_sonarr",
    "integration_status",
    "map_path",
    "parse_path_map",
    "read_secret",
    "reset_clients",
    "unmap_path",
]

_clients = {}
_lock = threading.Lock()


def _get(key, factory):
    with _lock:
        client = _clients.get(key)
        if client is None:
            client = factory()
            _clients[key] = client
        return client


def get_sonarr():
    return _get("sonarr", SonarrClient.from_env)


def get_radarr():
    return _get("radarr", RadarrClient.from_env)


def get_jellyfin():
    return _get("jellyfin", JellyfinClient.from_env)


def reset_clients():
    """Drop cached clients so the next call re-reads the environment."""
    with _lock:
        for client in _clients.values():
            try:
                client.close()
            except Exception:
                pass
        _clients.clear()


def get_client_for(media_type):
    """The adapter responsible for a media type, or None when unconfigured."""
    client = get_radarr() if media_type == MOVIE else get_sonarr()
    return client if client.configured else None


def integration_status():
    """Reachability of every integration, for /api/status. Never raises."""
    return {
        "sonarr": get_sonarr().ping(),
        "radarr": get_radarr().ping(),
        "jellyfin": get_jellyfin().ping(),
    }
