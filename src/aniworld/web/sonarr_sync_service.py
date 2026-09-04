"""Run the Sonarr-to-AniWorld sync on a clock or from a Connect hook."""

import json
import os
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from ..logger import get_logger
from ..sonarr_sync import (
    DEFAULT_ANIWORLD_URL,
    DEFAULT_MAPPING_FILE,
    run_sync,
)
from . import db, schedule

logger = get_logger(__name__)

TICK_SECONDS = 60

_started = False
_start_lock = threading.Lock()
_run_lock = threading.Lock()
_anchor = None


def enabled():
    return os.getenv("ANIWORLD_SONARR_SYNC_ENABLED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def configured_schedule():
    return schedule.parse(os.getenv("ANIWORLD_SONARR_SYNC_CRON", "0 0 * * *"))


def _now():
    return datetime.now(timezone.utc)


def _local(moment):
    return moment.astimezone().replace(tzinfo=None)


def _utc(moment):
    return moment.astimezone(timezone.utc)


def _parse(value):
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def next_run_at():
    global _anchor
    if not enabled():
        return None
    current = _now()
    if _anchor is None:
        _anchor = current
    last_run = _parse(db.get_autosync_state().get("sonarr_sync_last_run"))
    upcoming = configured_schedule().next_run(_local(last_run or _anchor))
    return _utc(upcoming) if upcoming else None


def _options(series_ids=None, apply=True):
    return SimpleNamespace(
        apply=apply,
        aniworld_url=os.getenv("ANIWORLD_URL", DEFAULT_ANIWORLD_URL),
        mapping=os.getenv("SONARR_ANIWORLD_MAP_FILE", DEFAULT_MAPPING_FILE),
        language=os.getenv(
            "SONARR_SYNC_LANGUAGE", os.getenv("ANIWORLD_LANGUAGE", "German Dub")
        ),
        provider=os.getenv(
            "SONARR_SYNC_PROVIDER", os.getenv("ANIWORLD_PROVIDER", "VOE")
        ),
        priority=int(os.getenv("SONARR_SYNC_PRIORITY", "10")),
        max_episodes=int(os.getenv("SONARR_SYNC_MAX_EPISODES", "100")),
        include_specials=os.getenv("SONARR_SYNC_INCLUDE_SPECIALS", "0") == "1",
        series_id=list(series_ids or []),
        no_auto_match=os.getenv("SONARR_SYNC_AUTO_MATCH", "1") != "1",
    )


def run_cycle(series_ids=None, reason="scheduled", apply=True, _lock_held=False):
    if not _lock_held and not _run_lock.acquire(blocking=False):
        return {"ok": False, "reason": "already_running"}
    started = _now()
    lines = []

    def note(message):
        lines.append(str(message))
        logger.info("Sonarr sync: %s", message)

    try:
        queued = run_sync(_options(series_ids, apply=apply), note=note)
        report = {
            "ok": True,
            "reason": reason,
            "dry_run": not apply,
            "queued_episodes": queued,
            "started_at": started.isoformat(),
            "finished_at": _now().isoformat(),
            "messages": lines[-100:],
        }
    except Exception as exc:
        logger.exception("Sonarr sync cycle failed")
        report = {
            "ok": False,
            "reason": reason,
            "dry_run": not apply,
            "queued_episodes": 0,
            "started_at": started.isoformat(),
            "finished_at": _now().isoformat(),
            "error": str(exc)[:500],
            "messages": lines[-100:],
        }
    finally:
        db.set_autosync_state(
            sonarr_sync_last_run=started.isoformat(),
            sonarr_sync_last_report=json.dumps(report),
        )
        _run_lock.release()
    return report


def trigger(series_ids=None, reason="connect", apply=True):
    """Start one cycle without holding the Sonarr webhook request open."""
    if not _run_lock.acquire(blocking=False):
        return False
    try:
        threading.Thread(
            target=run_cycle,
            kwargs={
                "series_ids": series_ids,
                "reason": reason,
                "apply": apply,
                "_lock_held": True,
            },
            name="aniworld-sonarr-sync-run",
            daemon=True,
        ).start()
    except Exception:
        _run_lock.release()
        raise
    return True


def status():
    state = db.get_autosync_state()
    report = None
    try:
        report = json.loads(state.get("sonarr_sync_last_report") or "")
    except ValueError:
        pass
    upcoming = None
    schedule_error = None
    try:
        expression = configured_schedule().expression
        if enabled():
            upcoming = next_run_at()
    except schedule.ScheduleError as exc:
        expression = os.getenv("ANIWORLD_SONARR_SYNC_CRON", "0 0 * * *")
        schedule_error = str(exc)
    return {
        "enabled": enabled(),
        "running": _run_lock.locked(),
        "schedule": expression,
        "schedule_error": schedule_error,
        "next_run": upcoming.isoformat() if upcoming else None,
        "last_report": report,
    }


def _loop():
    while True:
        try:
            upcoming = next_run_at()
            if upcoming is not None and _now() >= upcoming:
                run_cycle(reason="scheduled", apply=True)
        except Exception:
            logger.exception("Sonarr sync scheduler error")
        threading.Event().wait(TICK_SECONDS)


def ensure_started():
    global _started, _anchor
    if not enabled():
        return
    with _start_lock:
        if _started:
            return
        _started = True
    _anchor = _now()
    if enabled():
        logger.info(
            "Sonarr sync scheduler enabled: %s",
            configured_schedule().expression,
        )
    threading.Thread(
        target=_loop, name="aniworld-sonarr-sync", daemon=True
    ).start()
