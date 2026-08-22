"""Jellyfin adapter.

Jellyfin authenticates with the ``Authorization`` header rather than a custom
one. The modern form is ``MediaBrowser Token="<key>"``; ``X-Emby-Token`` is kept
as a fallback header because older servers still accept it and it costs nothing
to send both.

Only two things are needed here: kick a library scan after an import, and
report reachability for the healthcheck. Everything else Jellyfin does is out
of scope — Sonarr and Radarr own the files.
"""

import os

from ..logger import get_logger
from .base import HttpClient, IntegrationError, env_flag, read_secret

logger = get_logger(__name__)


class JellyfinClient(HttpClient):
    #: Jellyfin reads the key from Authorization, not from a bespoke header.
    AUTH_HEADER = "Authorization"

    @classmethod
    def from_env(cls):
        return cls(os.getenv("JELLYFIN_URL", ""), read_secret("JELLYFIN_API_KEY"))

    @property
    def name(self):
        return "Jellyfin"

    @property
    def enabled(self):
        return self.configured and env_flag("JELLYFIN_SCAN_ENABLED", True)

    def auth_headers(self):
        return {
            "Authorization": f'MediaBrowser Token="{self.api_key}"',
            "X-Emby-Token": self.api_key,
        }

    def system_info(self):
        data = self.get("/System/Info") or {}
        return {
            "version": data.get("Version"),
            "server_name": data.get("ServerName"),
        }

    def virtual_folders(self):
        """The configured libraries, each with its on-disk Locations."""
        result = self.get("/Library/VirtualFolders")
        return result if isinstance(result, list) else []

    def refresh_all(self):
        """POST /Library/Refresh — scan every library.

        Takes no parameters and returns 204, so there is no body to parse.
        """
        self.post("/Library/Refresh", expect_json=False)
        return {"ok": True, "scope": "all"}

    def refresh_item(self, item_id, metadata_refresh_mode="Default"):
        self.post(
            f"/Items/{item_id}/Refresh",
            params={
                "metadataRefreshMode": metadata_refresh_mode,
                "imageRefreshMode": metadata_refresh_mode,
                "replaceAllMetadata": "false",
                "replaceAllImages": "false",
            },
            expect_json=False,
        )
        return {"ok": True, "scope": "item", "item_id": item_id}

    def find_library_for_path(self, path):
        """The library whose Locations contain ``path``, if any.

        ``path`` is the location as *Jellyfin* sees it. When Jellyfin mounts the
        storage elsewhere than we do, no library will match and the caller falls
        back to a full scan — correct, just slower.
        """
        if not path:
            return None
        target = str(path).rstrip("/")
        best = None
        best_len = -1
        for folder in self.virtual_folders():
            for location in folder.get("Locations") or []:
                location = str(location).rstrip("/")
                if not location:
                    continue
                if (
                    target == location or target.startswith(location + "/")
                ) and len(location) > best_len:
                    best, best_len = folder, len(location)
        return best

    def refresh_for_path(self, path):
        """Scan just the library containing ``path``, falling back to all of them.

        A targeted scan matters on a large library over network storage, where
        a full scan can run for many minutes and would be triggered after every
        single episode.
        """
        try:
            folder = self.find_library_for_path(path)
        except IntegrationError as exc:
            logger.debug(f"Jellyfin library lookup failed: {exc}")
            folder = None

        if folder and folder.get("ItemId"):
            logger.debug(
                f"Refreshing Jellyfin library {folder.get('Name')!r} for {path}"
            )
            return self.refresh_item(folder["ItemId"])
        return self.refresh_all()
