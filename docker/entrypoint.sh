#!/bin/sh
# Container entrypoint.
#
# Two jobs, both about surviving as a long-running service:
#
#   1. Start Xvfb, because the captcha solver drives a headed Chromium and
#      there is no display in a container.
#   2. Stay PID 1 and forward signals. The old `sh -c "... & exec aniworld"`
#      form left the shell as PID 1 with SIGTERM going nowhere useful, so
#      `docker stop` killed the app outright and any in-flight ffmpeg mux was
#      left as a half-written file. Here SIGTERM is passed on and we wait for
#      the app to unwind.

set -eu

: "${ANIWORLD_WEB_PORT:=8080}"
: "${DISPLAY:=:99}"
: "${ANIWORLD_XVFB_RESOLUTION:=1280x720x24}"

export DISPLAY

xvfb_pid=""
app_pid=""

log() {
    echo "[entrypoint] $*" >&2
}

start_xvfb() {
    if [ "${ANIWORLD_NO_XVFB:-0}" = "1" ]; then
        log "Xvfb disabled via ANIWORLD_NO_XVFB"
        return
    fi
    Xvfb "$DISPLAY" -screen 0 "$ANIWORLD_XVFB_RESOLUTION" -nolisten tcp &
    xvfb_pid=$!

    # Wait for the display socket instead of sleeping a fixed second: on a busy
    # host Xvfb can take longer, and Chromium fails hard if it starts first.
    socket="/tmp/.X11-unix/X$(echo "$DISPLAY" | tr -d ':')"
    i=0
    while [ ! -e "$socket" ] && [ "$i" -lt 50 ]; do
        i=$((i + 1))
        sleep 0.1
    done
    if [ ! -e "$socket" ]; then
        log "warning: Xvfb did not come up within 5s, continuing anyway"
    fi
}

# shellcheck disable=SC2317  # reached via trap, which shellcheck cannot see
shutdown() {
    log "signal received, stopping"
    if [ -n "$app_pid" ] && kill -0 "$app_pid" 2>/dev/null; then
        kill -TERM "$app_pid" 2>/dev/null || true
        wait "$app_pid" 2>/dev/null || true
    fi
    if [ -n "$xvfb_pid" ] && kill -0 "$xvfb_pid" 2>/dev/null; then
        kill -TERM "$xvfb_pid" 2>/dev/null || true
    fi
    exit 0
}

trap shutdown TERM INT

# Create the staging directories we own. Library folders are left alone: the
# layout under the media mount belongs to Sonarr, Radarr and Jellyfin, and
# inventing folders there would be a surprise.
#
# Failures are ignored on purpose — a read-only or not-yet-mounted share should
# surface as a clear error from the app, not as an entrypoint crash loop.
for dir in \
    "${ANIWORLD_INSTALL_FOLDER:-/config}" \
    "${ANIWORLD_DOWNLOAD_PATH:-}" \
    "${ANIWORLD_COMPLETED_PATH:-}"
do
    if [ -n "$dir" ]; then
        mkdir -p "$dir" 2>/dev/null || log "warning: could not create $dir"
    fi
done

start_xvfb

if [ "$#" -gt 0 ]; then
    "$@" &
else
    aniworld --web-ui --web-expose --no-browser --web-port "$ANIWORLD_WEB_PORT" &
fi
app_pid=$!

# `wait` returns as soon as a signal arrives, whether or not the child died, so
# loop until the app is really gone and keep its real exit code.
exit_code=0
while kill -0 "$app_pid" 2>/dev/null; do
    wait "$app_pid" && exit_code=0 || exit_code=$?
done
exit "$exit_code"
