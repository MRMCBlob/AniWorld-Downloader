"""Container healthcheck.

Exits 0 when the service is genuinely working, non-zero otherwise, so Docker
can restart a process that is up but no longer doing anything.

"HTTP 200" is not the interesting question here. The failure that actually
happens in a long-running deployment is a dead queue worker: the web UI keeps
answering, the queue keeps accepting items, and nothing ever downloads. So the
check reads worker_running out of /api/status and fails on that too.

Uses only the standard library — the image has no curl, and adding one just for
this would be a whole package for a request Python can already make.
"""

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 10


def main():
    port = os.environ.get("ANIWORLD_WEB_PORT", "8080")
    url = f"http://127.0.0.1:{port}/api/status"

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
