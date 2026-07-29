"""Radarr adapter.

API v3 (``/api/v3``), ``X-Api-Key`` header — structurally the same as Sonarr,
with movies in place of series and episodes. The import uses the same two-step
ManualImport flow, which is simpler here because a movie file maps to exactly
one entity: there are no episode ids to resolve.
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


class RadarrClient(ServarrClient):
    SERVICE = "radarr"

    @classmethod
    def from_env(cls):
        return cls(os.getenv("RADARR_URL", ""), read_secret("RADARR_API_KEY"))

    # ------------------------------------------------------------------ #
    # Movie lookup
    # ------------------------------------------------------------------ #

    def list_movies(self):
        result = self.get(self.api("/movie"))
        return result if isinstance(result, list) else []

    def lookup_movie(self, term):
        result = self.get(self.api("/movie/lookup"), params={"term": term})
        return result if isinstance(result, list) else []

    def find_movie(self, title=None, year=None, imdb_id=None, tmdb_id=None):
        """Find a movie already added to Radarr.

        Ids win over titles. When falling back to the title, the year is used as
        a tiebreaker if we have one — remakes share titles more often than not.
        """
        library = self.list_movies()

        for key, value in (("tmdbId", tmdb_id), ("imdbId", imdb_id)):
            if not value:
                continue
            wanted = str(value).strip().lower()
            for movie in library:
                if str(movie.get(key) or "").strip().lower() == wanted:
                    return movie

        if title:
            wanted = _normalize_title(title)
            matches = [
                movie
                for movie in library
                if _normalize_title(movie.get("title") or "") == wanted
                or _normalize_title(movie.get("originalTitle") or "") == wanted
            ]
            if year:
                for movie in matches:
                    if str(movie.get("year") or "") == str(year):
                        return movie
            if len(matches) == 1:
                return matches[0]

        return None

    def add_movie(self, title=None, year=None, imdb_id=None, tmdb_id=None):
        """Add a movie to Radarr, opt-in via ``RADARR_AUTO_ADD``."""
        if not env_flag("RADARR_AUTO_ADD", False):
            return None

        root_folder = os.getenv("RADARR_ROOT_FOLDER", "").strip()
        quality_profile = env_int("RADARR_QUALITY_PROFILE_ID", 0)
        if not root_folder or not quality_profile:
            logger.warning(
                "RADARR_AUTO_ADD is on but RADARR_ROOT_FOLDER / "
                "RADARR_QUALITY_PROFILE_ID are not set; skipping auto-add"
            )
            return None

        term = None
        for prefix, value in (("tmdb", tmdb_id), ("imdb", imdb_id)):
            if value:
                term = f"{prefix}:{value}"
                break
        if term is None and title:
            term = f"{title} {year}".strip() if year else title
        if not term:
            return None

        matches = self.lookup_movie(term)
        if not matches:
            logger.info(f"Radarr lookup found nothing for {term!r}")
            return None

        candidate = dict(matches[0])
        candidate.update(
            {
                "rootFolderPath": root_folder,
                "qualityProfileId": quality_profile,
                "monitored": True,
                "addOptions": {"searchForMovie": False},
            }
        )
        created = self.post(self.api("/movie"), json=candidate)
        if isinstance(created, dict) and created.get("id"):
            logger.info(f"Added movie to Radarr: {created.get('title')}")
            return created
        return None

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    def refresh_movie(self, movie_id=None, wait=True):
        # RefreshMovieCommand takes a list; omitting it refreshes everything.
        movie_ids = [movie_id] if movie_id else None
        return self.run_command("RefreshMovie", wait=wait, movieIds=movie_ids)

    def rescan_movie(self, movie_id=None, wait=True):
        # RescanMovieCommand takes a single nullable id, not a list.
        return self.run_command("RescanMovie", wait=wait, movieId=movie_id)

    def queue(self):
        result = self.get(self.api("/queue"))
        return result if isinstance(result, dict) else {"records": []}

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #

    def import_file(self, file_path, root=None, hints=None):
        """Import one finished movie file, letting Radarr rename and move it."""
        hints = hints or {}
        folder = relative_folder(file_path, root)
        mapped_file = self.map_path(file_path)

        movie = self.find_movie(
            title=hints.get("title"),
            year=hints.get("year"),
            imdb_id=hints.get("imdb_id"),
            tmdb_id=hints.get("tmdb_id"),
        )
        if movie is None:
            movie = self.add_movie(
                title=hints.get("title"),
                year=hints.get("year"),
                imdb_id=hints.get("imdb_id"),
                tmdb_id=hints.get("tmdb_id"),
            )
        if movie is None:
            return {
                "ok": False,
                "reason": "movie_not_in_radarr",
                "detail": (
                    f"{hints.get('title') or file_path} is not in Radarr. "
                    "Add it there (or enable RADARR_AUTO_ADD) and retry the import."
                ),
            }

        candidates = self.manual_import_candidates(folder)
        candidate = _match_candidate(candidates, mapped_file)
        if candidate is None:
            return {
                "ok": False,
                "reason": "file_not_visible_to_radarr",
                "detail": (
                    f"Radarr did not list {mapped_file} when scanning {self.map_path(folder)}. "
                    "Check that it mounts the same storage and that "
                    "ANIWORLD_ARR_PATH_MAP matches its paths."
                ),
            }

        candidate = self._reprocess(candidate, movie["id"])

        payload = {
            "path": candidate.get("path") or mapped_file,
            "folderName": candidate.get("folderName"),
            "movieId": movie["id"],
            "quality": candidate.get("quality"),
            "languages": candidate.get("languages"),
            "releaseGroup": candidate.get("releaseGroup") or "",
            "indexerFlags": candidate.get("indexerFlags") or 0,
        }
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
            "movie_id": movie["id"],
            "movie_title": movie.get("title"),
            "command_id": command.get("id"),
            "rejections": [r.get("reason") for r in rejections if r.get("reason")],
        }

    def _reprocess(self, candidate, movie_id):
        """Best-effort re-parse so quality and languages are not left Unknown."""
        item = {
            "path": candidate.get("path"),
            "movieId": movie_id,
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
            logger.debug(f"Radarr reprocess failed, using the original parse: {exc}")
            return candidate
        if processed and isinstance(processed[0], dict):
            merged = dict(candidate)
            merged.update({k: v for k, v in processed[0].items() if v is not None})
            return merged
        return candidate


def _normalize_title(value):
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _match_candidate(candidates, mapped_file):
    target = str(mapped_file)
    for candidate in candidates:
        if candidate.get("path") == target:
            return candidate
    name = target.rsplit("/", 1)[-1]
    for candidate in candidates:
        if (candidate.get("name") or "").strip() == name:
            return candidate
        if str(candidate.get("path") or "").rsplit("/", 1)[-1] == name:
            return candidate
    return None
