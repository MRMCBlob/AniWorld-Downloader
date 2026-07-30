"""What happens to a file after the download itself finished.

The pipeline is: verify the file is playable, move it out of the incomplete
staging area, work out whether it is a series episode or a movie, hand it to
Sonarr or Radarr for the rename, nudge Jellyfin, and publish events.

This module sits between the download core and the integrations, and is the
only thing that knows about both. ``aniworld.models`` does not import it; the
queue worker calls it once an episode is on disk.

Every step degrades rather than fails. A missing integration, an unreachable
Sonarr or a file Radarr refuses all leave the download itself intact in the
completed folder, where a later retry — or a human — can pick it up.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import events
from .logger import get_logger

logger = get_logger(__name__)

#: Anything smaller than this is a stub or an error page, not a video.
MIN_MEDIA_BYTES = 128 * 1024

VERIFY_TIMEOUT = 60


@dataclass
class FinalizeResult:
    """Outcome of post-processing a single downloaded file."""

    ok: bool = False
    path: str = None
    original_path: str = None
    media_type: str = None
    title: str = None
    season: int = None
    episode: int = None
    verified: bool = False
    imported: bool = False
    stage: str = "verify"
    error: str = None
    details: dict = field(default_factory=dict)

    @property
    def status(self):
        """Queue status this result corresponds to."""
        if not self.ok:
            return "failed"
        return "imported" if self.imported else "completed"


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def verify_media(path):
    """Check that ``path`` is a playable media file.

    A download can "succeed" and still leave a truncated or empty file behind —
    a hoster serving an error page, a stream that dropped mid-mux. Handing that
    to Sonarr means it gets imported over a good copy, so it is worth the extra
    ffprobe call.

    Returns ``(ok, detail)``. When ffprobe is unavailable the size check alone
    decides, rather than failing every download on a machine without it.
    """
    path = Path(path)
    if not path.exists():
        return False, "file does not exist"

    size = path.stat().st_size
    if size < MIN_MEDIA_BYTES:
        return False, f"file is only {size} bytes"

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        logger.debug("ffprobe not found; verifying by file size only")
        return True, f"size ok ({size} bytes), ffprobe unavailable"

    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"ffprobe failed on {path}: {exc}")
        return True, f"size ok ({size} bytes), ffprobe errored: {exc}"

    if completed.returncode != 0:
        detail = " ".join((completed.stderr or "").split())[:200]
        return False, f"ffprobe rejected the file: {detail}"

    try:
        probe = json.loads(completed.stdout or "{}")
    except ValueError:
        return True, f"size ok ({size} bytes), ffprobe output unreadable"

    duration = _as_float((probe.get("format") or {}).get("duration"))
    codec_types = {s.get("codec_type") for s in probe.get("streams") or []}

    if duration is not None and duration <= 0:
        return False, "media reports zero duration"
    if "video" not in codec_types:
        return False, f"no video stream (found: {sorted(t for t in codec_types if t)})"

    return True, f"{size} bytes, {duration or '?'}s, streams={sorted(codec_types)}"


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Staging
# --------------------------------------------------------------------------- #


def completed_root():
    """Where finished files are staged, or None when staging is disabled."""
    raw = os.getenv("ANIWORLD_COMPLETED_PATH", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    return path


def stage_file(source, incomplete_root, target_root):
    """Move ``source`` from the incomplete tree into ``target_root``.

    The path relative to ``incomplete_root`` is preserved, so the folder layout
    the naming template produced survives the move. ``shutil.move`` is used
    rather than ``os.replace`` because the two roots are frequently on
    different filesystems — that is the normal case with a network mount.
    """
    source = Path(source)
    if target_root is None:
        return source

    try:
        relative = source.relative_to(incomplete_root)
    except (ValueError, TypeError):
        # Outside the staging root (custom download path, say) — keep the file
        # name only, rather than reconstructing an absolute path underneath it.
        relative = Path(source.name)

    destination = Path(target_root) / relative
    if destination == source:
        # Staging into the same tree the download wrote to. Without this the
        # unlink below would delete the file we are about to move.
        return source
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    shutil.move(str(source), str(destination))
    _prune_empty_dirs(source.parent, incomplete_root)
    logger.debug(f"Staged {source} -> {destination}")
    return destination


def _prune_empty_dirs(directory, stop_at):
    """Remove directories left empty by the move, without escaping ``stop_at``."""
    if stop_at is None:
        return
    stop_at = Path(stop_at)
    directory = Path(directory)
    while directory != stop_at:
        try:
            directory.relative_to(stop_at)
        except ValueError:
            return
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def finalize(episode, queue_id=None, media_type_override=None, on_stage=None):
    """Run the full post-processing pipeline for one downloaded episode.

    ``on_stage(name)`` is called as the pipeline advances so the caller can
    reflect it in the queue without this module importing the database.
    """
    result = FinalizeResult()

    def stage(name):
        result.stage = name
        if on_stage:
            try:
                on_stage(name)
            except Exception as exc:
                logger.debug(f"on_stage({name}) callback failed: {exc}")

    source = _episode_path(episode)
    if source is None:
        result.error = "could not determine the downloaded file path"
        return result
    result.original_path = str(source)
    result.path = str(source)

    # -- verify ---------------------------------------------------------- #
    stage("verifying")
    ok, detail = verify_media(source)
    result.details["verify"] = detail
    if not ok:
        result.error = f"verification failed: {detail}"
        logger.warning(f"{source}: {result.error}")
        return result
    result.verified = True

    # -- classify -------------------------------------------------------- #
    from .integrations import classify

    info = classify(episode, override=media_type_override)
    result.media_type = info.media_type
    result.title = info.title
    result.season = info.season
    result.episode = info.episode
    result.details["classification"] = info.reason
    logger.debug(
        f"Classified {source.name} as {info.media_type} ({info.reason}); "
        f"title={info.title!r} season={info.season} episode={info.episode}"
    )

    # -- stage ----------------------------------------------------------- #
    stage("staging")
    target_root = completed_root()
    try:
        final_path = stage_file(source, _download_root(episode), target_root)
    except OSError as exc:
        result.error = f"could not move the file into the completed folder: {exc}"
        logger.error(result.error)
        return result
    result.path = str(final_path)

    # The download itself succeeded and the file is safe on disk. Everything
    # from here is enrichment: it may fail without making the result a failure.
    result.ok = True
    events.emit(
        events.DOWNLOAD_COMPLETED,
        type=info.media_type,
        title=info.title,
        season=info.season,
        episode=info.episode,
        path=str(final_path),
        queue_id=queue_id,
    )

    # -- import ---------------------------------------------------------- #
    stage("importing")
    import_result = import_media(final_path, info, root=target_root)
    result.details["import"] = import_result
    result.imported = bool(import_result.get("ok"))

    if result.imported:
        # Sonarr/Radarr moved the file into the library folder they picked,
        # which is rarely the folder our naming template produced. Without this
        # every import leaves an empty season/series folder behind — and with
        # staging disabled those pile up right next to the real library.
        _prune_empty_dirs(Path(final_path).parent, _download_root(episode))
        events.emit(
            events.IMPORT_COMPLETED,
            type=info.media_type,
            title=info.title,
            season=info.season,
            episode=info.episode,
            path=str(final_path),
            queue_id=queue_id,
        )
        # -- jellyfin ---------------------------------------------------- #
        stage("scanning")
        result.details["jellyfin"] = trigger_jellyfin_scan(import_result)

    stage("done")
    return result


def import_media(path, info, root=None):
    """Hand a staged file to Sonarr or Radarr.

    Returns a dict describing the outcome. Not raising is deliberate: an
    unreachable or unconfigured *arr must not turn a good download into a
    failed queue item, because the file is already safe and the import can be
    retried later.
    """
    from .integrations import IntegrationError, get_client_for

    try:
        client = get_client_for(info.media_type)
    except Exception as exc:
        return {"ok": False, "reason": "adapter_error", "detail": str(exc)}

    if client is None:
        return {
            "ok": False,
            "reason": "not_configured",
            "detail": (
                f"No {'Radarr' if info.media_type == 'movie' else 'Sonarr'} URL/API key "
                "configured; leaving the file in the completed folder."
            ),
        }

    try:
        return client.import_file(path, root=root, hints=info.hints())
    except IntegrationError as exc:
        logger.warning(f"Import of {path} failed: {exc}")
        return {"ok": False, "reason": "integration_error", "detail": str(exc)}
    except Exception as exc:
        logger.error(f"Unexpected error importing {path}: {exc}", exc_info=True)
        return {"ok": False, "reason": "unexpected_error", "detail": str(exc)}


def trigger_jellyfin_scan(import_result=None):
    """Refresh the Jellyfin library the imported file landed in.

    Best effort and never fatal: Jellyfin also scans on its own schedule, so
    the worst case is that the episode shows up a bit later.
    """
    from .integrations import get_jellyfin

    try:
        client = get_jellyfin()
        if not client.enabled:
            return {"ok": False, "reason": "not_configured"}
        # We do not know where the *arr moved the file to, so a targeted scan
        # is only possible when it told us the destination folder.
        path = (import_result or {}).get("destination_path")
        return client.refresh_for_path(path) if path else client.refresh_all()
    except Exception as exc:
        logger.info(f"Jellyfin scan skipped: {exc}")
        return {"ok": False, "reason": "error", "detail": str(exc)}


def _episode_path(episode):
    """The file the download wrote, using whichever attribute the model exposes."""
    for name in ("_episode_path", "episode_path"):
        try:
            value = getattr(episode, name, None)
        except Exception:
            value = None
        if value:
            return Path(value)
    return None


def _download_root(episode):
    """Root the episode's path was built under, for preserving the layout."""
    try:
        selected = getattr(episode, "selected_path", None)
    except Exception:
        selected = None
    if selected:
        return Path(selected)

    raw = os.getenv("ANIWORLD_DOWNLOAD_PATH", "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else Path.home() / path
    return Path.home() / "Downloads"
