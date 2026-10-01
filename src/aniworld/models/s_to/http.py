"""Domain-fallback fetching for SerienStream.

The public domain has changed more than once. Requests are therefore retried
against a configurable list of complete base URLs, including the scheme. The
scheme matters: the preferred direct-IP endpoint is HTTP-only, while the named
backup domains use HTTPS. The first endpoint that answers is remembered and
reused, and any SerienStream URL is rewritten to it so pages, redirect links
and stream resolution all stay on the same working host.
"""

import os
import threading
import warnings
from urllib.parse import urljoin, urlsplit, urlunsplit

from niquests import Session
from urllib3.exceptions import InsecureRequestWarning

try:
    from ...config import GLOBAL_SESSION, STO_DOMAINS, STO_HOST_RE, STO_IP, logger
except ImportError:
    from aniworld.config import (
        GLOBAL_SESSION,
        STO_DOMAINS,
        STO_HOST_RE,
        STO_IP,
        logger,
    )

warnings.simplefilter("ignore", InsecureRequestWarning)

# Match any known serienstream host so URLs can be rewritten to the active one.
# STO_DOMAINS and STO_IP are re-exported from config for direct-import callers.
DEFAULT_STO_ENDPOINTS = (
    f"http://{STO_IP}",
    "https://serienstream.to",
    "https://serienstream.cx",
)

_HOST_RE = STO_HOST_RE

_active_endpoint = None
_active_idx = 0
_active_lock = threading.Lock()
_state_lock = threading.Lock()
_thread_local = threading.local()


class SerienstreamResponseError(RuntimeError):
    """A serienstream host answered, but did not return usable page HTML."""


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
    global _active_endpoint, _active_idx
    parsed = urlsplit(url)
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if endpoint not in sto_endpoints():
        return False
    host = parsed.netloc
    with _active_lock:
        _active_endpoint = endpoint
    with _state_lock:
        for idx, domain in enumerate(STO_DOMAINS):
            if host == domain:
                _active_idx = idx
                break
    return True


def sto_host():
    """The currently preferred serienstream host."""
    with _state_lock:
        return STO_DOMAINS[_active_idx]


def _global_headers():
    """Copy browser-compatible headers without optional Brotli encoding."""
    headers = dict(GLOBAL_SESSION.headers)
    headers["Accept-Encoding"] = "gzip, deflate"
    return headers


def _sync_global_state(session):
    """Refresh state that the captcha solver may have changed globally."""
    try:
        session.headers.update(_global_headers())
    except Exception:
        pass
    try:
        session.cookies.update(GLOBAL_SESSION.cookies)
    except Exception:
        pass


def _safe_url(url):
    """Keep diagnostics useful without leaking signed redirect query strings."""
    try:
        parsed = urlsplit(str(url or ""))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        return "serienstream request"


def _new_session():
    """Build a niquests session owned by the current thread."""
    # Brotli support is optional. Asking only for encodings every supported
    # installation can decode keeps ``response.text`` deterministic.
    session = Session(
        resolver=["doh+cloudflare://"],
        disable_http3=True,
        multiplexed=False,
        headers=_global_headers(),
    )
    session.verify = GLOBAL_SESSION.verify
    _sync_global_state(session)
    return session


def _session():
    """Return the current thread's session and copy in fresh captcha cookies."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = _new_session()
        _thread_local.session = session
    else:
        _sync_global_state(session)
    return session


def reset_sto_session():
    """Discard only the calling thread's HTTP state after a bad response."""
    session = getattr(_thread_local, "session", None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
        delattr(_thread_local, "session")


def response_text(response, url):
    """Return validated response text or raise a descriptive error."""
    text = getattr(response, "text", None)
    if not isinstance(text, str):
        raise SerienstreamResponseError(
            f"SerienStream returned an invalid response body for {_safe_url(url)}"
        )
    return text


def _validate_response(response, requested_url):
    response.raise_for_status()
    final_url = str(getattr(response, "url", "") or requested_url)

    # /r can redirect to the selected video host. Its body belongs to another
    # provider and callers only need the final URL, so do not validate it here.
    if not _HOST_RE.match(final_url):
        return response

    text = response_text(response, requested_url)
    lower = text[:20000].lower()
    if (
        "<title>just a moment" in lower
        or "<title>attention required" in lower
        or "cdn-cgi/challenge-platform" in lower
        or "cf_chl_" in lower
    ):
        raise SerienstreamResponseError(
            f"SerienStream returned a challenge page for {_safe_url(requested_url)}"
        )
    return response


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
    """GET a serienstream URL, falling back across domains then the IP.

    Returns the response of the first host that answers; remembers it.
    """
    global _active_idx
    path = _path_of(url)
    last_err = None

    with _state_lock:
        start_idx = _active_idx

    def fetch(target, request_kwargs, *, extra_headers=None):
        nonlocal last_err
        for attempt in range(2):
            current = session or _session()
            options = dict(request_kwargs)
            headers = dict(options.pop("headers", {}) or {})
            if not any(name.lower() == "accept-encoding" for name in headers):
                headers.setdefault("Accept-Encoding", "gzip, deflate")
            if extra_headers:
                headers.update(extra_headers)
            try:
                response = current.get(
                    target,
                    timeout=timeout,
                    headers=headers,
                    **options,
                )
                return _validate_response(response, target)
            except Exception as exc:
                last_err = exc
                if session is None and attempt == 0:
                    reset_sto_session()
                    continue
                break
        return None

    # Try the configured domains, starting at the last known-good one.
    for offset in range(len(STO_DOMAINS)):
        idx = (start_idx + offset) % len(STO_DOMAINS)
        response = fetch(f"https://{STO_DOMAINS[idx]}{path}", kwargs)
        if response is not None:
            with _state_lock:
                _active_idx = idx
            return response

    # Last resort: the raw IP with a Host header.
    ip_kwargs = dict(kwargs)
    ip_kwargs["verify"] = False
    response = fetch(
        f"https://{STO_IP}{path}",
        ip_kwargs,
        extra_headers={"Host": STO_DOMAINS[0]},
    )
    if response is not None:
        return response

    raise last_err or RuntimeError(f"all serienstream hosts failed for {url}")
