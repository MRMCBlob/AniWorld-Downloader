#!/bin/sh
# Sonarr Settings -> Connect -> Custom Script.
# Recommended trigger: On Series Add.  The nightly run is scheduled by
# AniWorld Downloader itself; Sonarr Connect has no time-based trigger.

set -eu

: "${ANIWORLD_URL:?Set ANIWORLD_URL in the Sonarr container (for example http://aniworld:8080)}"

event_type="${sonarr_eventtype:-Unknown}"
base_url="${ANIWORLD_URL%/}"

if ! command -v curl >/dev/null 2>&1; then
    echo "AniWorld hook needs curl in the Sonarr container" >&2
    exit 1
fi

# Sonarr's Test button must not queue anything.
if [ "$event_type" = "Test" ]; then
    curl --fail --silent --show-error "$base_url/api/status?details=0" >/dev/null
    echo "AniWorld Downloader is reachable"
    exit 0
fi

# Avoid duplicate grabs: On Grab fires while Sonarr's own download still has
# no file, so using it would request the same episode from AniWorld as well.
if [ "$event_type" != "SeriesAdd" ]; then
    echo "AniWorld hook ignored Sonarr event: $event_type"
    exit 0
fi

series_id="${sonarr_series_id:-}"
case "$series_id" in
    ''|*[!0-9]*)
        echo "Sonarr did not provide a numeric sonarr_series_id" >&2
        exit 1
        ;;
esac

payload="{\"series_id\":$series_id}"
if [ -n "${ANIWORLD_API_KEY:-}" ]; then
    curl --fail --silent --show-error \
        -H "X-API-Key: $ANIWORLD_API_KEY" \
        -H "Content-Type: application/json" \
        --data "$payload" \
        "$base_url/api/sonarr/sync"
else
    curl --fail --silent --show-error \
        -H "Content-Type: application/json" \
        --data "$payload" \
        "$base_url/api/sonarr/sync"
fi
echo
