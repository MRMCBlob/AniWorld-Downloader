"""API-key authentication for machine callers.

The web UI authenticates with a session cookie, which nothing outside a browser
can produce. Sonarr webhooks, cron jobs and shell scripts need a way in that
does not involve a login form, so when ``ANIWORLD_API_KEY`` is set a request
may present it instead of a session.

The key is only ever compared with a constant-time function, and it is never
echoed back — not in ``/api/status``, not in an error message.
"""

import hmac

from flask import request

from ..integrations.base import read_secret

#: Header clients send. The ``apikey`` query parameter is accepted as well,
#: mirroring what Sonarr and Radarr allow, for callers that cannot set headers.
API_KEY_HEADER = "X-Api-Key"
API_KEY_PARAM = "apikey"


def configured_api_key():
    return read_secret("ANIWORLD_API_KEY")


def api_key_enabled():
    return bool(configured_api_key())


def presented_api_key():
    key = request.headers.get(API_KEY_HEADER)
    if key:
        return key.strip()
    return (request.args.get(API_KEY_PARAM) or "").strip()


def has_valid_api_key():
    """Whether this request carries the configured API key.

    False when no key is configured: an unset key must not turn into "no
    authentication required".
    """
    expected = configured_api_key()
    if not expected:
        return False
    presented = presented_api_key()
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)
