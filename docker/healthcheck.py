"""Container healthcheck.

Exits 0 when the service is genuinely working, non-zero otherwise. The
entrypoint watches this result and exits after repeated failures, allowing the
container restart policy to recover the service (Docker itself does not
restart a container merely because its health state changed).

"HTTP 200" is not the interesting question here. The failure that actually
happens in a long-running deployment is a dead queue worker: the web UI keeps
answering, the queue keeps accepting items, and nothing ever downloads. So the
check reads worker_running out of /api/status and fails on that too.

Uses only the standard library — the image has no curl, and adding one just for
this would be a whole package for a request Python can already make.
"""

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT = 10


def storage_error():
    """Return a storage error string, or ``None`` when configured paths work."""
    configured = {
        os.environ.get("ANIWORLD_INSTALL_FOLDER", "").strip(),
        os.environ.get("ANIWORLD_DOWNLOAD_PATH", "").strip(),
        os.environ.get("ANIWORLD_COMPLETED_PATH", "").strip(),
    }
    for raw in sorted(path for path in configured if path):
        path = Path(raw)
        if not path.is_dir():
            return f"storage directory is missing: {path}"
        if not os.access(path, os.W_OK | os.X_OK):
            return f"storage directory is not writable: {path}"
        try:
            shutil.disk_usage(path)
        except OSError as exc:
            return f"storage directory is unavailable: {path}: {exc}"

    sentinel = os.environ.get("ANIWORLD_STORAGE_SENTINEL", "").strip()
    if sentinel and not Path(sentinel).is_file():
        return f"storage sentinel is missing: {sentinel}"
    return None


def main():
    error = storage_error()
    if error:
        print(f"unhealthy: {error}")
        return 1

    port = os.environ.get("ANIWORLD_WEB_PORT", "8080")
    # Keep the frequent probe cheap: authenticated/default status calls also
    # ping Sonarr, Radarr and Jellyfin for the dashboard.
    url = f"http://127.0.0.1:{port}/api/status?details=0"

    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            if response.status != 200:
                print(f"unhealthy: {url} returned HTTP {response.status}")
                return 1
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"unhealthy: cannot reach {url}: {exc.reason}")
        return 1
    except (OSError, ValueError) as exc:
        print(f"unhealthy: {url} unusable: {exc}")
        return 1

    if payload.get("status") != "ok":
        print(f"unhealthy: status={payload.get('status')!r}")
        return 1

    if payload.get("worker_running") is False:
        print("unhealthy: the download queue worker is not running")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
