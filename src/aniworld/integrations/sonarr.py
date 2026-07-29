"""Sonarr adapter.

API v3 (``/api/v3``), which is what Sonarr v4 and v5 still serve. Authentication
is the ``X-Api-Key`` header.

The import is deliberately the two-step ManualImport flow rather than the older
``DownloadedEpisodesScan`` command: that command no longer exists in Sonarr v4+.
Step one asks Sonarr what it makes of the files in a folder, step two hands the
resolved files back as a ``ManualImport`` command. Doing it this way lets us
repair Sonarr's guess with metadata we already have — the release names we
produce carry no scene group, so Sonarr's parser frequently cannot match an
episode on its own.
"""

import os

from ..logger import get_logger
from .base import (
    IntegrationError,
    ServarrClient,
    env_flag,
    env_int,
    read_secret,
    relative_folder,
)

logger = get_logger(__name__)


class SonarrClient(ServarrClient):
    SERVICE = "sonarr"

    @classmethod
    def from_env(cls):
        return cls(os.getenv("SONARR_URL", ""), read_secret("SONARR_API_KEY"))

    # ------------------------------------------------------------------ #
    # Series lookup
    # ------------------------------------------------------------------ #

    def list_series(self):
        result = self.get(self.api("/series"))
        return result if isinstance(result, list) else []

    def lookup_series(self, term):
        result = self.get(self.api("/series/lookup"), params={"term": term})
        return result if isinstance(result, list) else []

    def find_series(self, title=None, tvdb_id=None, imdb_id=None, tmdb_id=None):
        """Find a series already added to Sonarr.

        Only the library is searched — an id that resolves through
        ``/series/lookup`` but is not in the library cannot be imported into.
        Matching is by id first because titles differ between our sources and
        TheTVDB far too often to be trusted.
        """
        library = self.list_series()

        for key, value in (
            ("tvdbId", tvdb_id),
            ("tmdbId", tmdb_id),
            ("imdbId", imdb_id),
        ):
            if not value:
                continue
            wanted = str(value).strip().lower()
            for series in library:
                if str(series.get(key) or "").strip().lower() == wanted:
                    return series

        if title:
            wanted = _normalize_title(title)
            for series in library:
                candidates = {series.get("title"), series.get("sortTitle")}
                candidates.update(
                    alt.get("title") for alt in series.get("alternateTitles") or []
                )
                if any(c and _normalize_title(c) == wanted for c in candidates):
                    return series

        return None

    def add_series(self, title=None, tvdb_id=None, imdb_id=None, tmdb_id=None):
        """Add a series to Sonarr, opt-in via ``SONARR_AUTO_ADD``.

        Off by default: silently creating library entries in someone else's
        Sonarr is a surprising side effect of downloading one episode.
        """
        if not env_flag("SONARR_AUTO_ADD", False):
            return None

        root_folder = os.getenv("SONARR_ROOT_FOLDER", "").strip()
        quality_profile = env_int("SONARR_QUALITY_PROFILE_ID", 0)
        if not root_folder or not quality_profile:
            logger.warning(
                "SONARR_AUTO_ADD is on but SONARR_ROOT_FOLDER / "
                "SONARR_QUALITY_PROFILE_ID are not set; skipping auto-add"
            )
            return None

        term = None
        for prefix, value in (("tvdb", tvdb_id), ("tmdb", tmdb_id), ("imdb", imdb_id)):
            if value:
                term = f"{prefix}:{value}"
                break
        if term is None:
            term = title
        if not term:
            return None

        matches = self.lookup_series(term)
        if not matches:
            logger.info(f"Sonarr lookup found nothing for {term!r}")
            return None

        candidate = dict(matches[0])
        candidate.update(
            {
                "rootFolderPath": root_folder,
                "qualityProfileId": quality_profile,
                "monitored": True,
                "seasonFolder": True,
                "addOptions": {
                    "searchForMissingEpisodes": False,
                    "searchForCutoffUnmetEpisodes": False,
                },
            }
        )
        created = self.post(self.api("/series"), json=candidate)
        if isinstance(created, dict) and created.get("id"):
            logger.info(f"Added series to Sonarr: {created.get('title')}")
            return created
        return None

    def episodes_for_series(self, series_id, season_number=None):
        params = {"seriesId": series_id}
        if season_number is not None:
            params["seasonNumber"] = season_number
        result = self.get(self.api("/episode"), params=params)
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    def refresh_series(self, series_id=None, wait=True):
        # RefreshSeriesCommand exposes both SeriesId and SeriesIds; the plural
        # is the real field, the singular only feeds it and loses if the two
        # arrive in the wrong order. Omitting it refreshes the whole library.
        series_ids = [series_id] if series_id else None
        return self.run_command("RefreshSeries", wait=wait, seriesIds=series_ids)

    def rescan_series(self, series_id=None, wait=True):
        # RescanSeriesCommand takes a single nullable id, not a list.
        return self.run_command("RescanSeries", wait=wait, seriesId=series_id)

    def queue(self):
        result = self.get(self.api("/queue"))
        return result if isinstance(result, dict) else {"records": []}

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #

    def import_file(self, file_path, root=None, hints=None):
        """Import one finished file, letting Sonarr rename and move it.

        ``hints`` carries what we know about the download and is used to fill
        the gaps Sonarr's own parser leaves. Returns a result dict describing
        what happened; raises only when Sonarr itself is unusable.
        """
        hints = hints or {}
        folder = relative_folder(file_path, root)
        mapped_file = self.map_path(file_path)

        series = self.find_series(
            title=hints.get("title"),
            tvdb_id=hints.get("tvdb_id"),
            imdb_id=hints.get("imdb_id"),
            tmdb_id=hints.get("tmdb_id"),
        )
        if series is None:
            series = self.add_series(
                title=hints.get("title"),
                tvdb_id=hints.get("tvdb_id"),
                imdb_id=hints.get("imdb_id"),
                tmdb_id=hints.get("tmdb_id"),
            )
        if series is None:
            return {
                "ok": False,
                "reason": "series_not_in_sonarr",
                "detail": (
                    f"{hints.get('title') or file_path} is not in Sonarr. "
                    "Add it there (or enable SONARR_AUTO_ADD) and retry the import."
                ),
            }

        candidates = self.manual_import_candidates(folder)
        candidate = _match_candidate(candidates, mapped_file)
        if candidate is None:
            return {
                "ok": False,
                "reason": "file_not_visible_to_sonarr",
                "detail": (
                    f"Sonarr did not list {mapped_file} when scanning {self.map_path(folder)}. "
                    "Check that it mounts the same storage and that "
                    "ANIWORLD_ARR_PATH_MAP matches its paths."
                ),
            }

        episode_ids = _episode_ids(candidate)
        season = candidate.get("seasonNumber")
        if season is None:
            season = hints.get("season")

        if not episode_ids:
            # Sonarr could not parse our filename — expected, since we produce
            # no scene release names. We know the season and episode number, so
            # resolve the episode id ourselves instead of leaving the file in
            # the manual-import backlog for a human.
            episode_ids = self._resolve_episode_ids(
                series["id"], season, hints.get("episode")
            )

        if not episode_ids:
            return {
                "ok": False,
                "reason": "episode_not_matched",
                "detail": (
                    f"Could not determine which episode {mapped_file} is "
                    f"(season={season}, episode={hints.get('episode')})."
                ),
            }

        candidate = self._reprocess(candidate, series["id"], season, episode_ids)

        payload = {
            "path": candidate.get("path") or mapped_file,
            "folderName": candidate.get("folderName"),
            "seriesId": series["id"],
            "episodeIds": episode_ids,
            "quality": candidate.get("quality"),
            "languages": candidate.get("languages"),
            "releaseGroup": candidate.get("releaseGroup") or "",
            "indexerFlags": candidate.get("indexerFlags") or 0,
        }
        if candidate.get("releaseType"):
            payload["releaseType"] = candidate["releaseType"]
        payload = {k: v for k, v in payload.items() if v is not None}

        command = self.run_command(
            "ManualImport", files=[payload], importMode=self.import_mode()
        )
        status = (command.get("status") or "").lower()
        if status != "completed":
            return {
                "ok": False,
                "reason": f"command_{status or 'unknown'}",
                "detail": command.get("exception") or command.get("message") or "",
                "command_id": command.get("id"),
            }

        rejections = candidate.get("rejections") or []
        return {
            "ok": True,
            "series_id": series["id"],
            "series_title": series.get("title"),
            "episode_ids": episode_ids,
            "season": season,
            "command_id": command.get("id"),
            "rejections": [r.get("reason") for r in rejections if r.get("reason")],
            # Where Sonarr put the file, in its namespace — used to scope the
            # Jellyfin scan to the affected library instead of all of them.
            "destination_path": series.get("path"),
        }

    def _reprocess(self, candidate, series_id, season, episode_ids):
        """Ask Sonarr to re-parse the entry now that it knows which episode it is.

        Best effort: if the reprocess call fails we still have the original
        candidate, and importing with a slightly worse quality guess beats not
        importing at all.
        """
        item = {
            "path": candidate.get("path"),
            "seriesId": series_id,
            "seasonNumber": season,
            "episodeIds": episode_ids,
            "quality": candidate.get("quality"),
            "languages": candidate.get("languages"),
            "releaseGroup": candidate.get("releaseGroup") or "",
            "indexerFlags": candidate.get("indexerFlags") or 0,
        }
        try:
            processed = self.reprocess_import_items(
                [{k: v for k, v in item.items() if v is not None}]
            )
        except IntegrationError as exc:
            logger.debug(f"Sonarr reprocess failed, using the original parse: {exc}")
            return candidate
        if processed and isinstance(processed[0], dict):
            merged = dict(candidate)
            merged.update({k: v for k, v in processed[0].items() if v is not None})
            return merged
        return candidate

    def _resolve_episode_ids(self, series_id, season, episode):
        if season is None or episode is None:
            return []
        try:
            episodes = self.episodes_for_series(series_id, season_number=int(season))
        except (IntegrationError, ValueError):
            return []
        wanted = str(episode)
        return [
            ep["id"]
            for ep in episodes
            if str(ep.get("episodeNumber")) == wanted and ep.get("id")
        ]


def _normalize_title(value):
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _match_candidate(candidates, mapped_file):
    """Pick the manualimport entry describing our file."""
    target = str(mapped_file)
    for candidate in candidates:
        if candidate.get("path") == target:
            return candidate
    # Sonarr may normalise separators or casing; fall back to the file name.
    name = target.rsplit("/", 1)[-1]
    for candidate in candidates:
        if (candidate.get("name") or "").strip() == name:
            return candidate
        if str(candidate.get("path") or "").rsplit("/", 1)[-1] == name:
            return candidate
    return None


def _episode_ids(candidate):
    return [ep["id"] for ep in candidate.get("episodes") or [] if ep.get("id")]
