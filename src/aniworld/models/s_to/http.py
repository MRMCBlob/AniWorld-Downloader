"""Domain-fallback fetching for SerienStream.

The public domain has changed more than once. Requests are therefore retried
against a configurable list of complete base URLs, including the scheme. The
scheme matters: the preferred direct-IP endpoint is HTTP-only, while the named
backup domains use HTTPS.
"""

import os
import re
import threading
from urllib.parse import urljoin, urlsplit, urlunsplit

try:
    from ...config import GLOBAL_SESSION, logger
except ImportError:
    from aniworld.config import GLOBAL_SESSION, logger

# Kept for compatibility with callers that imported these names directly.
STO_DOMAINS = ["serienstream.to", "serienstream.cx"]
STO_IP = "186.2.175.5"

DEFAULT_STO_ENDPOINTS = (
    f"http://{STO_IP}",
    "https://serienstream.to",
    "https://serienstream.cx",
)

# Match any known serienstream host so URLs can be rewritten to the active one.
_HOST_RE = re.compile(
    r"^https?://(?:www\.)?(?:serienstream\.(?:to|cx)|s\.to|186\.2\.175\.5)"
    r"(?=[:/]|$)",
    re.IGNORECASE,
)

_active_endpoint = None
_active_lock = threading.Lock()


def _normalise_endpoint(value):
    """Return a safe origin URL, or ``None`` for an invalid value."""
    value = (value or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def sto_endpoints():
    """Configured endpoint origins in failover order.

    ``ANIWORLD_STO_ENDPOINTS`` is intentionally read for every request. A
    Dokploy environment change therefore takes effect after a container
    restart without rebuilding the image.
    """
    raw = os.getenv("ANIWORLD_STO_ENDPOINTS", "").strip()
    if not raw:
        return DEFAULT_STO_ENDPOINTS

    endpoints = []
    for value in raw.split(","):
        endpoint = _normalise_endpoint(value)
        if endpoint and endpoint not in endpoints:
            endpoints.append(endpoint)

    if endpoints:
        return tuple(endpoints)

    logger.warning(
        "ANIWORLD_STO_ENDPOINTS contains no valid HTTP(S) origins; using defaults"
    )
    return DEFAULT_STO_ENDPOINTS


def sto_base_url():
    """The currently preferred SerienStream origin, including its scheme."""
    endpoints = sto_endpoints()
    with _active_lock:
        active = _active_endpoint
    return active if active in endpoints else endpoints[0]


def _ordered_endpoints():
    endpoints = list(sto_endpoints())
    with _active_lock:
        active = _active_endpoint
    if active in endpoints:
        endpoints.remove(active)
        endpoints.insert(0, active)
    return tuple(endpoints)


def sto_candidate_urls(url):
    """Return ``url`` rewritten onto every origin in failover order."""
    path = _path_of(url)
    return tuple(f"{endpoint}{path}" for endpoint in _ordered_endpoints())


def sto_activate(url):
    """Remember the configured origin used by a successful browser solve."""
    global _active_endpoint
    parsed = urlsplit(url)
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if endpoint not in sto_endpoints():
        return False
    with _active_lock:
        _active_endpoint = endpoint
    return True


def sto_host():
    """The currently preferred SerienStream host (compatibility helper)."""
    return urlsplit(sto_base_url()).netloc


def sto_rewrite(url):
    """Rewrite any known SerienStream URL onto the active origin."""
    if not url:
        return url
    if not _HOST_RE.match(url):
        return url

    parsed = urlsplit(url)
    target = urlsplit(sto_base_url())
    return urlunsplit(
        (target.scheme, target.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def sto_url(path):
    """Build or rewrite a URL on the currently active SerienStream origin."""
    if not path:
        return path
    if _HOST_RE.match(path):
        return sto_rewrite(path)
    return urljoin(f"{sto_base_url()}/", path)


def _path_of(url):
    parsed = urlsplit(url)
    if parsed.scheme and parsed.netloc:
        return urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return url or "/"


def sto_get(url, session=None, timeout=10, **kwargs):
    """GET a SerienStream URL and remember the first endpoint that answers."""
    global _active_endpoint
    session = session or GLOBAL_SESSION
    path = _path_of(url)
    last_err = None

    # ddos-guard advertises Brotli to browser-like clients. The lightweight
    # HTTP stack in the Linux image does not include an optional Brotli decoder
    # and otherwise exposes compressed bytes through Response.text, making all
    # HTML extraction fail even though the request returned HTTP 200. Gzip and
    # deflate are decoded natively. Preserve an explicit caller override.
    headers = dict(kwargs.get("headers") or {})
    if not any(name.lower() == "accept-encoding" for name in headers):
        headers["Accept-Encoding"] = "gzip, deflate"
    kwargs["headers"] = headers

    endpoints = list(_ordered_endpoints())

    for endpoint in endpoints:
        try:
            request_url = f"{endpoint}{path}"
            resp = session.get(request_url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            with _active_lock:
                _active_endpoint = endpoint
            if endpoint != endpoints[0]:
                logger.warning(f"SerienStream switched to fallback endpoint {endpoint}")
            return resp
        except Exception as exc:
            last_err = exc

    raise RuntimeError(
        f"all SerienStream endpoints failed for {url}: {last_err}"
    ) from last_err
