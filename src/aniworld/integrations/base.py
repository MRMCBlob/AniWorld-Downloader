"""Shared plumbing for the external service adapters.

Everything an adapter needs that is not specific to one service lives here:
secret loading, path translation between containers, an HTTP client with
bounded retries, and — for the two Servarr apps, whose APIs are structurally
identical — the asynchronous command protocol.

Nothing in this package may be imported from ``aniworld.models``. The download
core stays unaware that Sonarr, Radarr or Jellyfin exist; the queue worker is
what wires the two sides together.
"""

import os
import time
from pathlib import Path, PurePosixPath

import niquests

from ..logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3
DEFAULT_COMMAND_TIMEOUT = 120

# Servarr reports these when a command has stopped running.
COMMAND_TERMINAL_STATES = ("completed", "failed", "aborted", "cancelled")


class IntegrationError(Exception):
    """An external service was reachable but could not do what we asked."""


class IntegrationNotConfigured(IntegrationError):
    """The service has no URL/API key configured, so there is nothing to call."""


def read_secret(name, default=""):
    """Read a secret from ``NAME``, or from the file named by ``NAME_FILE``.

    The ``_FILE`` indirection is what Docker secrets expect: the orchestrator
    mounts the value at a path and exports the path, never the value itself.
    """
    file_var = os.getenv(f"{name}_FILE", "").strip()
    if file_var:
        try:
            return Path(file_var).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning(f"Could not read {name}_FILE ({file_var}): {exc}")
            return default
    return os.getenv(name, default).strip()


def env_flag(name, default=False):
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def parse_path_map(raw=None):
    """Parse ``ANIWORLD_ARR_PATH_MAP`` into ordered (ours, theirs) prefix pairs.

    Format: ``/media:/data`` — multiple pairs separated by commas. Longer
    prefixes are applied first so that a specific mapping wins over a broader
    one covering the same tree.
    """
    if raw is None:
        raw = os.getenv("ANIWORLD_ARR_PATH_MAP", "")
    pairs = []
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        # Prefer the separator immediately before a POSIX target. This keeps a
        # Windows drive prefix (``C:\\...:/data``) intact when the test suite or
        # a remote-control client prepares a mapping for a Linux Arr service.
        separator = chunk.rfind(":/")
        if separator > 1:
            ours, theirs = chunk[:separator], chunk[separator + 1 :]
        else:
            ours, _, theirs = chunk.partition(":")
        ours = ours.replace("\\", "/")
        theirs = theirs.replace("\\", "/")
        ours, theirs = ours.strip().rstrip("/"), theirs.strip().rstrip("/")
        if ours and theirs:
            pairs.append((ours, theirs))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return tuple(pairs)


def map_path(path, path_map=None):
    """Translate one of our container paths into the path the peer service sees.

    Sonarr and Radarr resolve the paths we hand them inside *their own*
    filesystem namespace. If they mount the same storage somewhere else, an
    unmapped path simply does not exist for them and the import fails with a
    confusing "file not found".
    """
    if path is None:
        return None
    if path_map is None:
        path_map = parse_path_map()
    text = str(path).replace("\\", "/")
    for ours, theirs in path_map:
        if text == ours:
            return theirs
        if text.startswith(ours + "/"):
            return theirs + text[len(ours) :]
    return text


def unmap_path(path, path_map=None):
    """Inverse of :func:`map_path` — turn a peer's path back into ours."""
    if path is None:
        return None
    if path_map is None:
        path_map = parse_path_map()
    text = str(path).replace("\\", "/")
    for ours, theirs in path_map:
        if text == theirs:
            return ours
        if text.startswith(theirs + "/"):
            return ours + text[len(theirs) :]
    return text


class HttpClient:
    """Small JSON-over-HTTP client with bounded retries.

    Only connection errors and 5xx responses are retried: a 4xx means we sent
    something wrong and repeating it just delays the real error.
    """

    #: Header carrying the API key. Overridden per service.
    AUTH_HEADER = "X-Api-Key"

    def __init__(self, base_url, api_key, timeout=None, retries=None):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = timeout or env_int("ANIWORLD_INTEGRATION_TIMEOUT", DEFAULT_TIMEOUT)
        self.retries = retries if retries is not None else DEFAULT_RETRIES
        self._session = None

    @property
    def configured(self):
        return bool(self.base_url and self.api_key)

    @property
    def name(self):
        return type(self).__name__.replace("Client", "")

    def auth_headers(self):
        return {self.AUTH_HEADER: self.api_key}

    def _get_session(self):
        if self._session is None:
            session = niquests.Session()
            session.headers.update(
                {"Accept": "application/json", "User-Agent": "AniWorld-Downloader"}
            )
            self._session = session
        return self._session

    def close(self):
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def request(self, method, path, params=None, json=None, expect_json=True):
        if not self.configured:
            raise IntegrationNotConfigured(f"{self.name} is not configured")

        url = f"{self.base_url}{path}"
        headers = self.auth_headers()
        last_error = None

        for attempt in range(self.retries):
            if attempt:
                delay = min(2**attempt, 10)
                logger.debug(f"{self.name}: retrying {method} {path} in {delay}s")
                time.sleep(delay)
            try:
                response = self._get_session().request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=self.timeout,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                continue
            if response.status_code == 401 or response.status_code == 403:
                raise IntegrationError(
                    f"{self.name} rejected the API key (HTTP {response.status_code})"
                )
            if response.status_code >= 400:
                raise IntegrationError(
                    f"{self.name} {method} {path} failed: HTTP "
                    f"{response.status_code} {_short_body(response)}"
                )

            if not expect_json or not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise IntegrationError(
                    f"{self.name} returned a non-JSON response for {path}: {exc}"
                ) from exc

        raise IntegrationError(
            f"{self.name} {method} {path} failed after {self.retries} attempts: {last_error}"
        )

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def ping(self):
        """Return a small status dict for /api/status. Never raises."""
        if not self.configured:
            return {"configured": False, "reachable": False}
        try:
            info = self.system_info()
        except IntegrationError as exc:
            return {"configured": True, "reachable": False, "error": str(exc)[:200]}
        return {"configured": True, "reachable": True, **info}

    def system_info(self):  # pragma: no cover - overridden by subclasses
        raise NotImplementedError


def _short_body(response):
    try:
        text = response.text or ""
    except Exception:
        return ""
    text = " ".join(text.split())
    return text[:200]


class ServarrClient(HttpClient):
    """Base for Sonarr and Radarr, which share the v3 API shape.

    Both expose the same asynchronous command endpoint, the same manual-import
    endpoint and the same API-key header; only the entity nouns differ
    (series/episodes vs. movie).
    """

    API_PREFIX = "/api/v3"

    #: Set by subclasses — used in log messages and error text.
    SERVICE = "servarr"

    def __init__(self, base_url, api_key, **kwargs):
        super().__init__(base_url, api_key, **kwargs)
        self.path_map = parse_path_map()
        self.command_timeout = env_int(
            "ANIWORLD_ARR_COMMAND_TIMEOUT", DEFAULT_COMMAND_TIMEOUT
        )

    def api(self, path):
        return f"{self.API_PREFIX}{path}"

    def map_path(self, path):
        return map_path(path, self.path_map)

    def system_info(self):
        data = self.get(self.api("/system/status")) or {}
        return {
            "version": data.get("version"),
            "app_name": data.get("appName"),
            "instance_name": data.get("instanceName"),
        }

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    def send_command(self, name, **body):
        """POST a command and return the created CommandResource."""
        payload = {"name": name}
        payload.update({k: v for k, v in body.items() if v is not None})
        result = self.post(self.api("/command"), json=payload)
        if not isinstance(result, dict) or "id" not in result:
            raise IntegrationError(f"{self.name}: command {name} returned no id")
        return result

    def get_command(self, command_id):
        return self.get(self.api(f"/command/{command_id}")) or {}

    def wait_for_command(self, command_id, timeout=None, poll_interval=1.0):
        """Block until a command reaches a terminal state.

        Returns the final CommandResource. A timeout is not treated as a
        failure by the caller's standards — the command may still succeed — so
        it is surfaced as its own status rather than an exception.
        """
        deadline = time.monotonic() + (timeout or self.command_timeout)
        last = {}
        while time.monotonic() < deadline:
            last = self.get_command(command_id)
            status = (last.get("status") or "").lower()
            if status in COMMAND_TERMINAL_STATES:
                return last
            time.sleep(poll_interval)
        last = dict(last or {})
        last["status"] = "timeout"
        return last

    def run_command(self, name, wait=True, timeout=None, **body):
        """Send a command and, by default, wait for it to finish."""
        command = self.send_command(name, **body)
        if not wait:
            return command
        return self.wait_for_command(command["id"], timeout=timeout)

    # ------------------------------------------------------------------ #
    # Manual import
    # ------------------------------------------------------------------ #

    def manual_import_candidates(self, folder):
        """GET /api/v3/manualimport for a folder, in the peer's path namespace.

        Step one of the two-step import: Servarr parses the files it finds and
        tells us what it thinks they are, including any rejections.

        Deliberately sends nothing but the folder. Both ManualImportControllers
        short-circuit to "list the files this series/movie already has" as soon
        as seriesId/movieId is present, which would silently return the wrong
        set of files instead of the ones we just downloaded.
        """
        query = {"folder": self.map_path(folder), "filterExistingFiles": "true"}
        result = self.get(self.api("/manualimport"), params=query)
        return result if isinstance(result, list) else []

    def reprocess_import_items(self, items):
        """POST /api/v3/manualimport — re-parse entries with our corrections.

        Servarr fills in quality, languages, custom formats and rejections for
        the ids we supply. Without this a file whose name its parser could not
        read would be imported as quality "Unknown".
        """
        result = self.post(self.api("/manualimport"), json=items)
        return result if isinstance(result, list) else []

    def import_mode(self):
        """Value for the command's ImportMode enum (Auto = 0, Move = 1, Copy = 2)."""
        mode = os.getenv("ANIWORLD_ARR_IMPORT_MODE", "move").strip().lower()
        return {"move": "Move", "copy": "Copy", "auto": "Auto"}.get(mode, "Move")


def relative_folder(file_path, root):
    """Folder to hand to manualimport: the file's own directory, under ``root``.

    Servarr scans a folder, not a file, so we point it at the deepest directory
    containing the file. Falling back to ``root`` keeps behaviour sane if the
    file somehow sits outside it.
    """
    raw_file = str(file_path)
    raw_root = str(root) if root else None
    # Integration APIs use Linux/container paths even when their tests or a
    # remote-control client run on Windows. pathlib.Path would rewrite those
    # slashes before the configured /media:/data mapping gets a chance.
    if raw_file.startswith("/"):
        parent = PurePosixPath(raw_file).parent
        root = PurePosixPath(raw_root) if raw_root else None
    else:
        file_path = Path(raw_file)
        parent = file_path.parent
        root = Path(raw_root) if raw_root else None
    if root is not None:
        try:
            parent.relative_to(root)
        except ValueError:
            return str(root)
    return str(parent)
