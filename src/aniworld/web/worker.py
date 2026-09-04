"""Background download worker.

One global thread processes the queue a single item at a time, so downloads
never fight over ffmpeg or the captcha browser.
"""

import json
import os
import threading
import time

from .. import events, postprocess
from ..logger import get_logger
from ..models.common import common as _common
from ..providers import resolve_provider
from . import db, paths
from .media import mangafire_format

logger = get_logger(__name__)

_started = False
_start_lock = threading.Lock()
_worker_thread = None

# How long to wait before looking at the queue again when it is empty.
IDLE_SECONDS = 3


def ensure_started():
    """Start the worker thread once per process."""
    global _started, _worker_thread
    with _start_lock:
        if _started:
            return
        _started = True
    db.reset_stale_running()
    _worker_thread = threading.Thread(
        target=_run, name="aniworld-queue", daemon=True
    )
    _worker_thread.start()


def is_running():
    """Whether the long-lived queue worker is alive."""
    return bool(_started and _worker_thread and _worker_thread.is_alive())


def _stall_timeout():
    """Seconds without progress before an unattended download is aborted."""
    try:
        return max(0, int(os.environ.get("ANIWORLD_STALL_TIMEOUT", "") or 900))
    except ValueError:
        return 900


def _postprocess_enabled():
    """Enable the homelab pipeline when staging or an *arr service is configured."""
    override = os.environ.get("ANIWORLD_POSTPROCESS_ENABLED")
    if override is not None:
        return override.strip() == "1"
    return bool(
        os.environ.get("ANIWORLD_COMPLETED_PATH", "").strip()
        or os.environ.get("SONARR_URL", "").strip()
        or os.environ.get("RADARR_URL", "").strip()
    )


class _StallWatchdog:
    """Ask the download core to unwind when its progress stops changing."""

    POLL_INTERVAL = 5

    def __init__(self, queue_id, timeout):
        self.queue_id = queue_id
        self.timeout = timeout
        self._stop = threading.Event()
        self._thread = None
        self.tripped = False

    def __enter__(self):
        if self.timeout > 0:
            self._thread = threading.Thread(
                target=self._run, name=f"stall-watchdog-{self.queue_id}", daemon=True
            )
            self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.POLL_INTERVAL + 1)
        _common.clear_abort(self.queue_id)
        return False

    def _run(self):
        last_seen = None
        last_change = time.monotonic()
        while not self._stop.wait(self.POLL_INTERVAL):
            snapshot = _common.get_ffmpeg_progress()
            marker = (snapshot.get("percent"), snapshot.get("time"))
            if marker != last_seen:
                last_seen = marker
                last_change = time.monotonic()
                continue
            if time.monotonic() - last_change > self.timeout:
                logger.warning(
                    "Queue item %s made no progress for %ss; aborting",
                    self.queue_id,
                    self.timeout,
                )
                self.tripped = True
                _common.request_abort(self.queue_id)
                return


def _claim_next():
    """Take the next queued item and mark it running, or None if busy/empty."""
    if db.get_running():
        return None
    item = db.get_next_queued()
    if not item:
        return None
    try:
        db.set_queue_status(item["id"], "running")
    except Exception:
        logger.exception("Could not mark queue item %s as running", item["id"])
        return None
    return item


def _run():
    while True:
        item = None
        try:
            item = _claim_next()
            if not item:
                time.sleep(IDLE_SECONDS)
                continue
            _process(item)
        except Exception:
            logger.exception("Queue worker error")
            if item:
                try:
                    db.set_queue_status(item["id"], "failed")
                except Exception:
                    pass
            time.sleep(IDLE_SECONDS)


def _episode_request(entry):
    """Normalise a queued entry into (url, extra episode kwargs)."""
    if not isinstance(entry, dict):
        return str(entry), {}

    url = (entry.get("url") or "").strip()
    extra = {}
    if entry.get("selected_pages") is not None:
        extra["selected_pages"] = entry["selected_pages"]
    if entry.get("series_url"):
        extra["_series_url"] = entry["series_url"]
    for key in ("target_path", "sonarr_series_id", "sonarr_episode_id"):
        if entry.get(key) is not None:
            extra[key] = entry[key]
    extra["_format"] = entry.get("mangafire_format", mangafire_format())
    return url, extra


def _build_episode(url, extra, item, selected_path):
    provider = resolve_provider(url)
    kwargs = {
        "url": url,
        "selected_language": item["language"],
        "selected_provider": item["provider"],
    }

    series = None
    if provider.name == "MangaFire":
        series_url = extra.get("_series_url") or url.rsplit("/chapter/", 1)[0]
        try:
            series = provider.series_cls(url=series_url)
        except Exception:
            series = None
        kwargs["format"] = extra["_format"]

    # MegaKino episodes build their own series context internally
    if series is not None and provider.name != "MegaKino":
        kwargs["series"] = series
    if "selected_pages" in extra:
        kwargs["selected_pages"] = extra["selected_pages"]
    direct_target = extra.get("target_path")
    if direct_target:
        if provider.name not in ("AniWorld", "SerienStream"):
            raise ValueError(
                "direct target paths currently support AniWorld and SerienStream only"
            )
        kwargs["selected_path"] = direct_target
        kwargs["direct_target"] = True
    elif selected_path:
        kwargs["selected_path"] = selected_path

    return provider, provider.episode_cls(**kwargs)


def _captcha_hint(provider, error):
    """Kinox guards downloads with a captcha every visitor gets.

    Attach the title page so the UI can offer a "solve it, then retry" button.
    """
    try:
        from ..models.kinox.series import KINOX_CAPTCHA_MARKER, kinox_captcha_page_url

        if provider and provider.name == "Kinox" and KINOX_CAPTCHA_MARKER in str(error):
            return kinox_captcha_page_url
    except Exception:
        pass
    return None


def _process(item):
    from ..playwright import captcha

    queue_id = item["id"]
    entries = json.loads(item["episodes"])
    selected_path = paths.target_path(item["language"], item.get("custom_path_id"))
    errors = []
    imported_count = 0

    for index, entry in enumerate(entries):
        url, extra = _episode_request(entry)
        provider = None
        try:
            db.update_queue_progress(queue_id, index, url)
            provider, episode = _build_episode(url, extra, item, selected_path)
            # Tells the captcha module to stream its browser into this queue item
            captcha._local.queue_id = queue_id
            _common.set_current_job(queue_id)
            events.emit(
                events.DOWNLOAD_STARTED,
                type=item.get("media_type"),
                title=item.get("title"),
                path=url,
                queue_id=queue_id,
            )
            try:
                with _StallWatchdog(queue_id, _stall_timeout()) as watchdog:
                    episode.download()
                if watchdog.tripped:
                    raise TimeoutError(
                        f"no progress for {_stall_timeout()}s; download aborted"
                    )
                if extra.get("target_path"):
                    _verify_direct_download(item, episode, extra)
                    imported_count += 1
                # MangaFire produces pages/archives, not video files that Sonarr,
                # Radarr or ffprobe can handle.
                elif (
                    getattr(provider, "name", "") != "MangaFire"
                    and _postprocess_enabled()
                    and _run_postprocess(item, episode)
                ):
                    imported_count += 1
            finally:
                captcha._local.queue_id = None
                _common.set_current_job(None)
        except Exception as exc:
            captcha._local.queue_id = None
            _common.set_current_job(None)
            # A force cancel kills the download on purpose. Whatever it raised
            # on the way down is the cancel, not a failure worth showing.
            if db.cancel_flags(queue_id)[1]:
                logger.info("Download of %s stopped by force cancel", url)
            else:
                logger.error("Download failed for %s: %s", url, exc)
                failure = {"url": url, "error": str(exc)}
                page_url = _captcha_hint(provider, exc)
                if page_url:
                    failure["captcha_url"] = page_url(url)
                errors.append(failure)
                db.update_queue_errors(queue_id, errors)
                events.emit(
                    events.DOWNLOAD_FAILED,
                    type=item.get("media_type"),
                    title=item.get("title"),
                    path=url,
                    queue_id=queue_id,
                    error=str(exc)[:500],
                )

        cancelled, forced = db.cancel_flags(queue_id)
        if cancelled:
            logger.info(
                "Download %s for queue item %s",
                "force cancelled" if forced else "cancelled",
                queue_id,
            )
            # a forced stop killed this episode part way, it does not count
            done = index if forced else index + 1
            db.update_queue_progress(queue_id, done, "")
            # asking to stop after the last episode still leaves everything on disk
            everything_done = not forced and done >= len(entries) and not errors
            db.set_queue_status(
                queue_id, "completed" if everything_done else "cancelled"
            )
            if everything_done and item.get("source") == "discord":
                _notify_discord(item)
            return

    db.update_queue_progress(queue_id, len(entries), "")
    _finish_queue_item(item, entries, errors, imported_count)


def _finish_queue_item(item, entries, errors, imported_count):
    """Record retry/import metadata and the final v5 queue state."""
    queue_id = item["id"]
    all_failed = bool(errors) and len(errors) == len(entries)
    if all_failed:
        last_error = errors[-1].get("error")
        delay = db.retry_backoff_seconds((item.get("attempts") or 0) + 1)
        if db.schedule_queue_retry(queue_id, delay, error=last_error):
            logger.warning("Queue item %s failed; retrying in %ss", queue_id, delay)
            return
        db.set_queue_status(queue_id, "failed", last_error=last_error)
        return

    direct_ids = {
        entry.get("sonarr_series_id")
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("target_path")
        and entry.get("sonarr_series_id")
    }
    if direct_ids and imported_count:
        rescan_ok = _rescan_direct_downloads(queue_id, direct_ids)
        if not rescan_ok:
            db.set_queue_import_status(queue_id, "rescan_failed")
        elif imported_count == len(entries):
            db.set_queue_import_status(queue_id, "imported")
        else:
            db.set_queue_import_status(queue_id, "partial")
    elif entries:
        if imported_count == len(entries):
            db.set_queue_import_status(queue_id, "imported")
        elif imported_count:
            db.set_queue_import_status(queue_id, "partial")
    db.set_queue_status(
        queue_id,
        "completed",
        last_error=errors[-1].get("error") if errors else None,
    )

    if item.get("source") == "discord":
        _notify_discord(item)


def _verify_direct_download(item, episode, entry):
    """Verify a file written straight into Sonarr's library tree."""
    path = postprocess._episode_path(episode)
    if path is None:
        raise RuntimeError("could not determine the direct download path")
    ok, detail = postprocess.verify_media(path)
    if not ok:
        raise RuntimeError(f"verification failed: {detail}")

    season = episode_number = None
    try:
        season = getattr(getattr(episode, "season", None), "season_number", None)
        episode_number = getattr(episode, "episode_number", None)
    except Exception:
        pass
    events.emit(
        events.DOWNLOAD_COMPLETED,
        type="series",
        title=item.get("title"),
        season=season,
        episode=episode_number,
        path=str(path),
        queue_id=item.get("id"),
        extra={"sonarr_episode_id": entry.get("sonarr_episode_id")},
    )


def _rescan_direct_downloads(queue_id, series_ids):
    """Tell Sonarr to index files that are already in their final folders."""
    try:
        from ..integrations import get_sonarr

        client = get_sonarr()
        if not client.configured:
            logger.warning("Queue item %s cannot rescan Sonarr: not configured", queue_id)
            return False
        for series_id in sorted(series_ids):
            result = client.rescan_series(series_id)
            if (result.get("status") or "").lower() != "completed":
                logger.warning(
                    "Sonarr rescan for series %s ended as %s",
                    series_id,
                    result.get("status") or "unknown",
                )
                return False
        return True
    except Exception as exc:
        logger.warning("Queue item %s could not rescan Sonarr: %s", queue_id, exc)
        return False


def _run_postprocess(item, episode):
    """Verify, stage and import one finished media file."""
    queue_id = item["id"]
    try:
        result = postprocess.finalize(
            episode,
            queue_id=queue_id,
            media_type_override=item.get("media_type"),
        )
    except Exception as exc:
        logger.exception("Post-processing failed for queue item %s", queue_id)
        raise RuntimeError(f"post-processing failed: {exc}") from exc

    if result.media_type:
        db.set_queue_media_type(queue_id, result.media_type)
    if not result.ok:
        raise RuntimeError(result.error or "post-processing failed")

    detail = result.details.get("import") or {}
    db.set_queue_import_status(
        queue_id,
        "imported" if result.imported else (detail.get("reason") or "pending"),
    )
    if not result.imported:
        logger.info(
            "Queue item %s left %s in the completed folder (%s)",
            queue_id,
            result.path,
            detail.get("reason") or "import pending",
        )
    return result.imported


def _notify_discord(item):
    try:
        from .discord_bot import notify_completed

        media_type = "movie" if int(item.get("total_episodes") or 1) <= 1 else "series"
        notify_completed(
            item.get("title") or "Unknown",
            media_type,
            item.get("language") or "",
            item.get("discord_user_id"),
        )
    except Exception as exc:
        logger.info("Discord completion notice skipped: %s", exc)
