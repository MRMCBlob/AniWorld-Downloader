"""Queue Sonarr's missing episodes from supported streaming sites.

This command is intentionally a one-shot job.  Cron, a systemd timer or the
orchestrator decides when "nightly" is; the downloader's normal queue remains
the single place that serialises downloads and retries failures.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import niquests

from .integrations import SonarrClient, read_secret, unmap_path


DEFAULT_ANIWORLD_URL = "http://127.0.0.1:8080"
DEFAULT_MAPPING_FILE = "/config/sonarr-aniworld-map.json"
DEFAULT_SYNC_SITES = ("aniworld", "sto")
SUPPORTED_SYNC_SITES = frozenset(DEFAULT_SYNC_SITES)
ACTIVE_QUEUE_STATES = {"queued", "running", "paused"}


class SyncError(RuntimeError):
    """A configuration or API error that should fail the cron invocation."""


class DownloaderClient:
    """The small part of AniWorld Downloader's JSON API used by this job."""

    def __init__(self, base_url, api_key="", timeout=30):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.timeout = timeout
        self._session = niquests.Session()
        self._session.headers.update(
            {"Accept": "application/json", "User-Agent": "AniWorld-Sonarr-Sync"}
        )

    def close(self):
        self._session.close()

    def request(self, method, path, params=None, payload=None):
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        try:
            response = self._session.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise SyncError(f"AniWorld Downloader is unreachable: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = (response.json() or {}).get("error")
            except (ValueError, AttributeError):
                detail = " ".join((response.text or "").split())[:200]
            raise SyncError(
                f"AniWorld Downloader {method} {path} failed with HTTP "
                f"{response.status_code}: {detail or 'no detail'}"
            )
        try:
            return response.json() if response.content else {}
        except ValueError as exc:
            raise SyncError(
                f"AniWorld Downloader returned non-JSON for {method} {path}"
            ) from exc

    def search(self, title, site="aniworld"):
        body = self.request(
            "POST", "/api/search", payload={"site": site, "keyword": title}
        )
        return body.get("results") or []

    def seasons(self, series_url):
        body = self.request("GET", "/api/seasons", params={"url": series_url})
        return body.get("seasons") or []

    def episodes(self, season_url, series_url):
        body = self.request(
            "GET",
            "/api/episodes",
            params={"url": season_url, "series_url": series_url},
        )
        return body.get("episodes") or []

    def queue(self):
        return self.request("GET", "/api/queue").get("items") or []

    def queue_download(self, payload):
        return self.request("POST", "/api/download", payload=payload)


def normalize_title(value):
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def normalize_sync_sites(value):
    """Return the safe, ordered set of sites the Sonarr matcher may use."""
    values = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    sites = tuple(
        dict.fromkeys(str(site).strip().casefold() for site in values if str(site).strip())
    )
    if not sites:
        sites = DEFAULT_SYNC_SITES
    unsupported = [site for site in sites if site not in SUPPORTED_SYNC_SITES]
    if unsupported:
        raise SyncError(
            "SONARR_SYNC_SITES contains unsupported site(s): "
            + ", ".join(unsupported)
        )
    return sites


def series_aliases(series):
    values = [series.get("title"), series.get("sortTitle"), series.get("originalTitle")]
    values.extend(
        entry.get("title")
        for entry in series.get("alternateTitles") or []
        if isinstance(entry, dict)
    )
    return tuple(dict.fromkeys(str(value).strip() for value in values if value))


def load_mappings(path):
    mapping_path = Path(path).expanduser()
    if not mapping_path.exists():
        return {}
    try:
        raw = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncError(f"Cannot read mapping file {mapping_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SyncError(f"Mapping file {mapping_path} must contain a JSON object")

    mappings = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            value = value.get("url")
        if value:
            mappings[str(key).strip().casefold()] = str(value).strip()
    return mappings


def configured_series_url(series, mappings):
    keys = [
        f"sonarr:{series.get('id')}",
        f"tvdb:{series.get('tvdbId')}",
        f"imdb:{series.get('imdbId')}",
    ]
    keys.extend(series_aliases(series))
    for key in keys:
        if key and key.casefold() in mappings:
            return mappings[key.casefold()]

    aliases = {normalize_title(value) for value in series_aliases(series)}
    for key, value in mappings.items():
        if normalize_title(key) in aliases:
            return value
    return None


def resolve_series_url(
    series,
    mappings,
    downloader,
    auto_match=True,
    sites=DEFAULT_SYNC_SITES,
):
    configured = configured_series_url(series, mappings)
    if configured:
        return configured, "mapping"
    if not auto_match:
        return None, "no mapping"

    aliases = series_aliases(series)
    normalised_aliases = {normalize_title(alias) for alias in aliases}
    searched_sites = normalize_sync_sites(sites)
    for site in searched_sites:
        exact = {}
        for alias in aliases:
            for result in downloader.search(alias, site=site):
                if normalize_title(result.get("title")) in normalised_aliases:
                    url = (result.get("url") or "").strip()
                    if url:
                        exact[url] = result.get("title") or alias
            if len(exact) == 1:
                # An exact hit on the canonical title is enough; avoid doing many
                # identical site searches for large alternate-title lists.
                return next(iter(exact)), f"exact title ({site})"
            if len(exact) > 1:
                return None, f"ambiguous exact matches on {site}"
    return None, f"no exact title match on {', '.join(searched_sites)}"


def parse_airdate(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def missing_episodes(series, episodes, now=None, include_specials=False):
    """Only episodes Sonarr monitors, has not got, and which have aired."""
    if not series.get("monitored", True):
        return []
    now = now or datetime.now(timezone.utc)
    monitored_seasons = {
        int(item.get("seasonNumber")): item.get("monitored", True)
        for item in series.get("seasons") or []
        if item.get("seasonNumber") is not None
    }
    missing = []
    for episode in episodes:
        try:
            season = int(episode.get("seasonNumber"))
            number = int(episode.get("episodeNumber"))
        except (TypeError, ValueError):
            continue
        if season == 0 and not include_specials:
            continue
        if not episode.get("monitored", True) or not monitored_seasons.get(season, True):
            continue
        if episode.get("hasFile") or int(episode.get("episodeFileId") or 0) > 0:
            continue
        aired_at = parse_airdate(episode.get("airDateUtc") or episode.get("airDate"))
        if aired_at is None or aired_at > now:
            continue
        if season < 0 or number <= 0:
            continue
        missing.append(episode)
    return sorted(
        missing,
        key=lambda item: (
            int(item.get("seasonNumber") or 0),
            int(item.get("episodeNumber") or 0),
        ),
    )


_SEASON_TOKEN = re.compile(r"\{season(?::(0+))?\}", re.IGNORECASE)


def render_season_folder(template, season_number):
    template = str(template or "Season {season:00}").strip()

    def replace(match):
        width = len(match.group(1) or "")
        return str(int(season_number)).zfill(width) if width else str(int(season_number))

    rendered = _SEASON_TOKEN.sub(replace, template)
    if not rendered or "{" in rendered or "}" in rendered:
        return f"Season {int(season_number):02d}"
    return rendered.replace("/", "-").replace("\\", "-")


def known_season_directories(series, episode_files):
    """Map seasons to the directory Sonarr already uses, in our namespace."""
    root = unmap_path(series.get("path") or "").rstrip("/")
    candidates = {}
    for episode_file in episode_files:
        try:
            season = int(episode_file.get("seasonNumber"))
        except (TypeError, ValueError):
            continue
        file_path = episode_file.get("path")
        if file_path:
            directory = posixpath.dirname(unmap_path(file_path))
        else:
            relative = str(episode_file.get("relativePath") or "").replace("\\", "/")
            directory = posixpath.join(root, posixpath.dirname(relative))
        candidates.setdefault(season, []).append(posixpath.normpath(directory))
    return {
        season: Counter(directories).most_common(1)[0][0]
        for season, directories in candidates.items()
        if directories
    }


def target_directory(series, season_number, known, naming):
    if season_number in known:
        return known[season_number]
    root = unmap_path(series.get("path") or "").rstrip("/")
    if not root:
        raise SyncError(f"Sonarr series {series.get('title')} has no path")
    if not series.get("seasonFolder", True):
        return root
    folder = render_season_folder(naming.get("seasonFolderFormat"), season_number)
    return posixpath.join(root, folder)


def active_sonarr_episode_ids(queue_items):
    ids = set()
    for item in queue_items:
        if item.get("status") not in ACTIVE_QUEUE_STATES:
            continue
        entries = item.get("episodes") or []
        if isinstance(entries, str):
            try:
                entries = json.loads(entries)
            except ValueError:
                continue
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("sonarr_episode_id"):
                continue
            try:
                ids.add(int(entry["sonarr_episode_id"]))
            except (TypeError, ValueError):
                pass
    return ids


def _episode_index(downloader, series_url, wanted_seasons):
    seasons = {
        int(item["season_number"]): item
        for item in downloader.seasons(series_url)
        if item.get("season_number") is not None and not item.get("are_movies")
    }
    indexed = {}
    for season_number in sorted(wanted_seasons):
        season = seasons.get(season_number)
        if not season:
            continue
        indexed[season_number] = {
            int(item["episode_number"]): item
            for item in downloader.episodes(season["url"], series_url)
            if item.get("episode_number") is not None
        }
    return indexed


def build_plan(
    sonarr,
    downloader,
    mappings,
    *,
    language,
    include_specials=False,
    series_ids=None,
    auto_match=True,
    sites=DEFAULT_SYNC_SITES,
    now=None,
    note=print,
):
    """Return queue payload fragments without changing either application."""
    selected_ids = {int(value) for value in series_ids or []}
    active_ids = active_sonarr_episode_ids(downloader.queue())
    naming = sonarr.naming_config()
    wanted_by_series = {}
    for episode in sonarr.wanted_missing():
        try:
            series_id = int(episode.get("seriesId") or 0)
        except (TypeError, ValueError):
            continue
        if series_id:
            wanted_by_series.setdefault(series_id, []).append(episode)
    plan = []

    for series in sonarr.list_series():
        series_id = int(series.get("id") or 0)
        if not series_id or (selected_ids and series_id not in selected_ids):
            continue
        missing = missing_episodes(
            series,
            wanted_by_series.get(series_id, []),
            now=now,
            include_specials=include_specials,
        )
        missing = [ep for ep in missing if int(ep.get("id") or 0) not in active_ids]
        if not missing:
            continue

        series_url, match_reason = resolve_series_url(
            series,
            mappings,
            downloader,
            auto_match=auto_match,
            sites=sites,
        )
        if not series_url:
            note(
                f"SKIP {series.get('title')}: {match_reason}; add "
                f"\"sonarr:{series_id}\" to the mapping file"
            )
            continue

        wanted_seasons = {int(ep["seasonNumber"]) for ep in missing}
        try:
            available_episodes = _episode_index(
                downloader, series_url, wanted_seasons
            )
            known = known_season_directories(
                series, sonarr.episode_files(series_id)
            )
        except Exception as exc:
            note(f"SKIP {series.get('title')}: cannot inspect episodes: {exc}")
            continue

        entries = []
        for episode in missing:
            season_number = int(episode["seasonNumber"])
            episode_number = int(episode["episodeNumber"])
            source = available_episodes.get(season_number, {}).get(episode_number)
            if not source:
                note(
                    f"SKIP {series.get('title')} S{season_number:02d}E{episode_number:02d}: "
                    "not available on the matched site"
                )
                continue
            available = source.get("available_languages") or []
            if available and language not in available:
                note(
                    f"SKIP {series.get('title')} S{season_number:02d}E{episode_number:02d}: "
                    f"{language} is unavailable"
                )
                continue
            entries.append(
                {
                    "url": source["url"],
                    "target_path": target_directory(
                        series, season_number, known, naming
                    ),
                    "sonarr_series_id": series_id,
                    "sonarr_episode_id": int(episode["id"]),
                }
            )
        if entries:
            plan.append(
                {
                    "series": series,
                    "series_url": series_url,
                    "match_reason": match_reason,
                    "episodes": entries,
                }
            )
    return plan


def run_sync(args, sonarr=None, downloader=None, note=print):
    own_sonarr = sonarr is None
    own_downloader = downloader is None
    sonarr = sonarr or SonarrClient.from_env()
    try:
        if not sonarr.configured:
            raise SyncError("SONARR_URL and SONARR_API_KEY must be configured")
        downloader = downloader or DownloaderClient(
            args.aniworld_url, read_secret("ANIWORLD_API_KEY")
        )
        mappings = load_mappings(args.mapping)
        sites = normalize_sync_sites(args.sites)
        plan = build_plan(
            sonarr,
            downloader,
            mappings,
            language=args.language,
            include_specials=args.include_specials,
            series_ids=args.series_id,
            auto_match=not args.no_auto_match,
            sites=sites,
            note=note,
        )

        remaining = max(0, args.max_episodes)
        queued = 0
        affected_series = 0
        for item in plan:
            episodes = (
                item["episodes"][:remaining]
                if args.max_episodes
                else item["episodes"]
            )
            if not episodes:
                break
            series = item["series"]
            targets = sorted({entry["target_path"] for entry in episodes})
            note(
                f"{'QUEUE' if args.apply else 'DRY-RUN'} {series.get('title')}: "
                f"{len(episodes)} episode(s) -> {', '.join(targets)} "
                f"[{item['match_reason']}]"
            )
            if args.apply:
                result = downloader.queue_download(
                    {
                        "title": series.get("title") or "Unknown",
                        "series_url": item["series_url"],
                        "episodes": episodes,
                        "language": args.language,
                        "provider": args.provider,
                        "priority": args.priority,
                        "media_type": "series",
                    }
                )
                note(f"  queue id: {result.get('queue_id')}")
            queued += len(episodes)
            affected_series += 1
            if args.max_episodes:
                remaining -= len(episodes)

        note(
            f"Sonarr sync finished: {queued} episode(s) "
            f"{'queued' if args.apply else 'would be queued'} across "
            f"{affected_series} series"
        )
        return queued
    finally:
        if own_downloader and downloader is not None:
            downloader.close()
        if own_sonarr:
            sonarr.close()


def parser():
    result = argparse.ArgumentParser(
        description=(
            "Read missing monitored episodes from Sonarr and queue matching "
            "episodes from AniWorld or SerienStream directly into Sonarr's "
            "library folders."
        )
    )
    result.add_argument(
        "--apply",
        action="store_true",
        help="actually queue downloads (without this flag the command is a dry-run)",
    )
    result.add_argument(
        "--aniworld-url",
        default=os.getenv("ANIWORLD_URL", DEFAULT_ANIWORLD_URL),
        help="AniWorld Downloader web URL",
    )
    result.add_argument(
        "--mapping",
        default=os.getenv("SONARR_ANIWORLD_MAP_FILE", DEFAULT_MAPPING_FILE),
        help="JSON file for explicit Sonarr-to-AniWorld title mappings",
    )
    result.add_argument(
        "--sites",
        default=os.getenv("SONARR_SYNC_SITES", ",".join(DEFAULT_SYNC_SITES)),
        help="comma-separated search order (supported: aniworld,sto)",
    )
    result.add_argument(
        "--language",
        default=os.getenv("SONARR_SYNC_LANGUAGE", os.getenv("ANIWORLD_LANGUAGE", "German Dub")),
    )
    result.add_argument(
        "--provider",
        default=os.getenv("SONARR_SYNC_PROVIDER", os.getenv("ANIWORLD_PROVIDER", "VOE")),
    )
    result.add_argument(
        "--priority", type=int, default=int(os.getenv("SONARR_SYNC_PRIORITY", "10"))
    )
    result.add_argument(
        "--max-episodes",
        type=int,
        default=int(os.getenv("SONARR_SYNC_MAX_EPISODES", "100")),
        help="maximum queued per run; 0 means unlimited",
    )
    result.add_argument("--include-specials", action="store_true")
    result.add_argument(
        "--series-id",
        type=int,
        action="append",
        default=[],
        help="limit the run to one Sonarr series id (repeatable)",
    )
    result.add_argument(
        "--no-auto-match",
        action="store_true",
        help="only use entries from the mapping file",
    )
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        run_sync(args)
    except Exception as exc:
        # The broad catch is deliberate at the process boundary: cron needs a
        # non-zero exit and one concise line even for an unexpected API shape.
        print(f"Sonarr sync failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the entry point
    raise SystemExit(main())
