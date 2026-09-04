"""Operational status and homelab integration endpoints."""

import os
import shutil
import time
from pathlib import Path

from flask import current_app, jsonify, request

from ...config import ANIWORLD_CONFIG_DIR
from ...logger import get_logger
from .. import apikeys, db, worker
from ..version import get_version

logger = get_logger(__name__)
_STARTED_AT = time.time()


def register(bp):
    bp.add_url_rule("/status", view_func=status)
    bp.add_url_rule("/sonarr/scan", view_func=sonarr_scan, methods=["POST"])
    bp.add_url_rule("/sonarr/sync", view_func=sonarr_sync_status, methods=["GET"])
    bp.add_url_rule("/sonarr/sync", view_func=sonarr_sync, methods=["POST"])
    bp.add_url_rule("/radarr/scan", view_func=radarr_scan, methods=["POST"])
    bp.add_url_rule("/jellyfin/scan", view_func=jellyfin_scan, methods=["POST"])
    bp.add_url_rule("/logs", view_func=logs)
    bp.add_url_rule("/retry/<int:queue_id>", view_func=retry, methods=["POST"])
    bp.add_url_rule("/cancel/<int:queue_id>", view_func=cancel, methods=["POST"])
    bp.add_url_rule("/pause/<int:queue_id>", view_func=pause, methods=["POST"])
    bp.add_url_rule("/resume/<int:queue_id>", view_func=resume, methods=["POST"])
    bp.add_url_rule(
        "/queue/<int:queue_id>/priority", view_func=set_priority, methods=["POST"]
    )


def _queue_summary():
    counts = db.queue_counts()
    by_status = {
        name: counts.get(name, 0) for name in db.QUEUE_STATUSES if counts.get(name, 0)
    }
    current = db.get_running()
    if current:
        current = {
            key: current.get(key)
            for key in ("id", "title", "current_episode", "total_episodes", "current_url")
        }
    return {
        "total": counts.get("all", 0),
        "by_status": by_status,
        "active": counts.get("active", 0),
        "current": current,
    }


def _authenticated():
    if not current_app.config.get("AUTH_ENABLED", False):
        return True
    if apikeys.current() is not None:
        return True
    from ..auth import get_current_user

    return get_current_user() is not None


def status():
    """Public, low-cost service health; authenticated calls get deployment detail."""
    payload = {
        "status": "ok",
        "version": get_version(),
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
        "worker_running": worker.is_running(),
        "queue": _queue_summary(),
    }
    if request.args.get("details") == "0" or not _authenticated():
        return jsonify(payload)

    from ...integrations import integration_status
    from ...integrations.webhooks import configured_urls
    from ..api_auth import api_key_enabled

    payload["integrations"] = integration_status()
    payload["paths"] = _path_summary()
    payload["api_key_required"] = api_key_enabled()
    payload["webhooks"] = {
        "configured": len(configured_urls()),
        **db.get_webhook_stats(),
    }
    return jsonify(payload)


def _path_summary():
    from ...postprocess import completed_root

    values = {
        "config": ANIWORLD_CONFIG_DIR,
        "incomplete": os.environ.get("ANIWORLD_DOWNLOAD_PATH") or None,
        "completed": completed_root(),
    }
    result = {}
    for name, raw in values.items():
        if not raw:
            result[name] = None
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.home() / path
        entry = {"path": str(path), "exists": path.exists()}
        if entry["exists"]:
            try:
                usage = shutil.disk_usage(path)
                entry.update(free_bytes=usage.free, total_bytes=usage.total)
            except OSError as exc:
                entry["error"] = str(exc)[:120]
        result[name] = entry
    return result


def _arr_scan(service):
    from ...integrations import IntegrationError, get_radarr, get_sonarr

    data = request.get_json(silent=True) or {}
    client = get_sonarr() if service == "sonarr" else get_radarr()
    if not client.configured:
        return jsonify({"error": f"{service.title()} is not configured"}), 503

    id_key = "series_id" if service == "sonarr" else "movie_id"
    raw_id = data.get(id_key)
    try:
        target_id = int(raw_id) if raw_id not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": f"{id_key} must be an integer"}), 400

    try:
        if service == "sonarr":
            refresh = client.refresh_series(target_id)
            rescan = client.rescan_series(target_id)
        else:
            refresh = client.refresh_movie(target_id)
            rescan = client.rescan_movie(target_id)
    except IntegrationError as exc:
        return jsonify({"error": str(exc)[:300]}), 502
    except Exception as exc:
        logger.exception("%s scan failed", service)
        return jsonify({"error": str(exc)[:300]}), 500

    return jsonify(
        {
            "ok": True,
            id_key: target_id,
            "refresh": {"id": refresh.get("id"), "status": refresh.get("status")},
            "rescan": {"id": rescan.get("id"), "status": rescan.get("status")},
        }
    )


def sonarr_scan():
    return _arr_scan("sonarr")


def sonarr_sync_status():
    from .. import sonarr_sync_service

    return jsonify(sonarr_sync_service.status())


def sonarr_sync():
    from .. import sonarr_sync_service

    data = request.get_json(silent=True) or {}
    raw_id = data.get("series_id")
    try:
        series_ids = [int(raw_id)] if raw_id not in (None, "") else []
    except (TypeError, ValueError):
        return jsonify({"error": "series_id must be an integer"}), 400
    dry_run = data.get("dry_run") is True
    if not sonarr_sync_service.trigger(
        series_ids=series_ids,
        reason="connect" if series_ids else "api",
        apply=not dry_run,
    ):
        return jsonify({"error": "A Sonarr sync is already running"}), 409
    return jsonify({"ok": True, "started": True, "dry_run": dry_run}), 202


def radarr_scan():
    return _arr_scan("radarr")


def jellyfin_scan():
    from ...integrations import IntegrationError, get_jellyfin

    client = get_jellyfin()
    if not client.configured:
        return jsonify({"error": "Jellyfin is not configured"}), 503
    data = request.get_json(silent=True) or {}
    try:
        path = (data.get("path") or "").strip()
        result = client.refresh_for_path(path) if path else client.refresh_all()
    except IntegrationError as exc:
        return jsonify({"error": str(exc)[:300]}), 502
    except Exception as exc:
        logger.exception("Jellyfin scan failed")
        return jsonify({"error": str(exc)[:300]}), 500
    return jsonify({"ok": True, **result})


def logs():
    from ...logger import get_log_file_path

    try:
        lines = max(1, min(int(request.args.get("lines", 200)), 2000))
    except (TypeError, ValueError):
        lines = 200
    path = get_log_file_path()
    if not path or not os.path.exists(path):
        return jsonify({"path": None, "lines": []})
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            tail = handle.readlines()[-lines:]
    except OSError as exc:
        return jsonify({"error": str(exc)[:200]}), 500
    return jsonify({"path": str(path), "lines": [line.rstrip() for line in tail]})


def _result(ok, error=None):
    return (jsonify({"ok": True}), 200) if ok else (jsonify({"error": error}), 400)


def retry(queue_id):
    return _result(db.requeue_item(queue_id), "Item not found or not retryable")


def cancel(queue_id):
    return _result(*db.cancel_queue_item(queue_id))


def pause(queue_id):
    return _result(*db.pause_queue_item(queue_id))


def resume(queue_id):
    return _result(*db.resume_queue_item(queue_id))


def set_priority(queue_id):
    data = request.get_json(silent=True) or {}
    try:
        priority = int(data.get("priority"))
    except (TypeError, ValueError):
        return jsonify({"error": "priority must be an integer"}), 400
    if not db.set_queue_priority(queue_id, priority):
        return jsonify({"error": "Item not found"}), 404
    return jsonify({"ok": True, "priority": priority})
