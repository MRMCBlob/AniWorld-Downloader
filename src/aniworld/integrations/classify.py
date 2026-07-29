"""Decide whether a finished download is a series episode or a movie.

The site models are not a common interface: each one grew its own attribute
names, and the same information sits on the episode for one site and on the
season or series for another. Reading them defensively here keeps that mess out
of the adapters, and keeps the models free of any knowledge about Sonarr and
Radarr.

Everything is best effort — a metadata property may hit the network, may return
None, or may raise on a page that no longer parses. None of that is worth
failing a download over, so every read is guarded.
"""

import os
import re
from dataclasses import dataclass, field

from ..logger import get_logger

logger = get_logger(__name__)

SERIES = "series"
MOVIE = "movie"

IMDB_RE = re.compile(r"tt\d{6,}")


@dataclass
class MediaInfo:
    """What we know about a finished download, in adapter-friendly form."""

    media_type: str = SERIES
    title: str = ""
    year: int = None
    season: int = None
    episode: int = None
    imdb_id: str = None
    tmdb_id: str = None
    tvdb_id: str = None
    #: Why the classification came out the way it did, for logs and the UI.
    reason: str = ""
    extra: dict = field(default_factory=dict)

    def hints(self):
        """The subset the Sonarr/Radarr adapters consume."""
        return {
            "title": self.title,
            "year": self.year,
            "season": self.season,
            "episode": self.episode,
            "imdb_id": self.imdb_id,
            "tmdb_id": self.tmdb_id,
            "tvdb_id": self.tvdb_id,
        }


def _safe(obj, name):
    """Read an attribute that may be a lazy property doing network I/O."""
    if obj is None:
        return None
    try:
        value = getattr(obj, name, None)
    except Exception as exc:
        logger.debug(f"Reading {type(obj).__name__}.{name} failed: {exc}")
        return None
    return value


def _first(sources, *names):
    """First non-empty value of any ``names`` across ``sources``, in order."""
    for source in sources:
        for name in names:
            value = _safe(source, name)
            if value not in (None, "", [], {}):
                return value
    return None


def _as_int(value):
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clean_imdb(value):
    if not value:
        return None
    match = IMDB_RE.search(str(value))
    return match.group(0) if match else None


def _default_media_type():
    value = os.getenv("ANIWORLD_DEFAULT_MEDIA_TYPE", SERIES).strip().lower()
    return MOVIE if value == MOVIE else SERIES


def classify(episode, override=None):
    """Build a :class:`MediaInfo` for a downloaded episode object.

    ``override`` wins over detection — the queue item may carry an explicit
    media type the user chose in the UI.
    """
    season_obj = _safe(episode, "season")
    series_obj = _safe(episode, "series") or _safe(season_obj, "series")
    sources = (episode, season_obj, series_obj)

    season = _as_int(_first((episode, season_obj), "season_number"))
    episode_number = _as_int(_safe(episode, "episode_number"))

    title = _first(sources, "title_cleaned", "title") or ""
    year = _as_int(_extract_year(_first(sources, "release_year")))

    imdb_id = _clean_imdb(_first(sources, "imdb", "imdb_id"))
    tmdb_id = _first(sources, "tmdb_id", "tmdb")
    tvdb_id = _first(sources, "tvdb_id", "tvdb")

    media_type, reason = _detect_type(episode, season_obj, season, override)

    info = MediaInfo(
        media_type=media_type,
        title=str(title).strip(),
        year=year,
        season=season,
        episode=episode_number,
        imdb_id=imdb_id,
        tmdb_id=str(tmdb_id) if tmdb_id else None,
        tvdb_id=str(tvdb_id) if tvdb_id else None,
        reason=reason,
    )

    if info.media_type == SERIES and info.season is None:
        # A series file with no season is unimportable. Season 1 is the only
        # sane guess and matches how the naming template already renders these.
        info.season = 1
        info.reason += "; assumed season 1"

    return info


def _detect_type(episode, season_obj, season, override):
    if override:
        wanted = str(override).strip().lower()
        if wanted in (SERIES, MOVIE):
            return wanted, "explicit override"

    is_movie = _safe(episode, "is_movie")
    if is_movie is True:
        return MOVIE, "episode.is_movie"
    are_movies = _safe(season_obj, "are_movies")
    if are_movies is True:
        return MOVIE, "season.are_movies"

    media_type = _safe(episode, "media_type")
    if isinstance(media_type, str) and media_type.strip().lower() == MOVIE:
        return MOVIE, "episode.media_type"

    if is_movie is False or are_movies is False or season is not None:
        return SERIES, "season information present"

    return _default_media_type(), "ANIWORLD_DEFAULT_MEDIA_TYPE"


def _extract_year(value):
    """Pull a four-digit year out of whatever the model returned.

    ``release_year`` is a range like "2012-2018" on some sites and a plain int
    on others; Radarr wants the first year either way.
    """
    if value is None:
        return None
    match = re.search(r"(19|20)\d{2}", str(value))
    return match.group(0) if match else None
