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
: "${ANIWORLD_HEALTH_WATCHDOG:=1}"
: "${ANIWORLD_HEALTH_START_PERIOD:=120}"
: "${ANIWORLD_HEALTH_INTERVAL:=30}"
: "${ANIWORLD_HEALTH_RETRIES:=3}"

export DISPLAY

xvfb_pid=""
app_pid=""
watchdog_pid=""

log() {
    echo "[entrypoint] $*" >&2
}

start_xvfb() {
    if [ "${ANIWORLD_NO_XVFB:-0}" = "1" ]; then
        log "Xvfb disabled via ANIWORLD_NO_XVFB"
        return
    fi

    # Xvfb refuses to create this directory itself when it is not root — the
    # check is hardcoded, and it prints
    #   _XSERVTransmkdir: ERROR: euid != 0, directory /tmp/.X11-unix will not be created
    # and then has nowhere to put its socket. We run unprivileged, so create it
    # here. Done at runtime rather than only in the image because /tmp is a
    # tmpfs in some setups, which would wipe anything baked in.
    mkdir -p /tmp/.X11-unix 2>/dev/null || true
    chmod 1777 /tmp/.X11-unix 2>/dev/null || true

    # Wait for the display socket instead of sleeping a fixed second: on a busy
    # host Xvfb can take longer, and Chromium fails hard if it starts first.
    display_number="$(echo "$DISPLAY" | sed 's/^://; s/\..*$//')"
    socket="/tmp/.X11-unix/X$display_number"
    lock="/tmp/.X${display_number}-lock"

    # Docker preserves the container filesystem across a restart. Xvfb itself
    # is gone at that point, but its socket and lock file can remain under
    # /tmp; starting against those stale files fails with "Server is already
    # active" and silently leaves captcha Chromium without a display. This
    # entrypoint owns this display number, so clear only its two runtime files.
    rm -f "$socket" "$lock"

    Xvfb "$DISPLAY" -screen 0 "$ANIWORLD_XVFB_RESOLUTION" -nolisten tcp &
    xvfb_pid=$!

    i=0
    while [ ! -e "$socket" ] && [ "$i" -lt 50 ]; do
        i=$((i + 1))
        sleep 0.1
    done
    if [ ! -e "$socket" ]; then
        log "warning: no X socket at $socket after 5s — captcha solving will"
        log "         likely fail because Chromium cannot reach the display"
    fi
}

# shellcheck disable=SC2317  # reached via trap, which shellcheck cannot see
shutdown() {
    log "signal received, stopping"
    if [ -n "$watchdog_pid" ] && kill -0 "$watchdog_pid" 2>/dev/null; then
        kill -TERM "$watchdog_pid" 2>/dev/null || true
    fi
    if [ -n "$app_pid" ] && kill -0 "$app_pid" 2>/dev/null; then
        kill -TERM "$app_pid" 2>/dev/null || true
        wait "$app_pid" 2>/dev/null || true
    fi
    if [ -n "$xvfb_pid" ] && kill -0 "$xvfb_pid" 2>/dev/null; then
        kill -TERM "$xvfb_pid" 2>/dev/null || true
    fi
    exit 0
}

start_health_watchdog() {
    if [ "$ANIWORLD_HEALTH_WATCHDOG" != "1" ]; then
        log "health watchdog disabled via ANIWORLD_HEALTH_WATCHDOG"
        return
    fi

    case "$ANIWORLD_HEALTH_START_PERIOD" in
        ''|*[!0-9]*)
            log "invalid ANIWORLD_HEALTH_START_PERIOD; using 120"
            ANIWORLD_HEALTH_START_PERIOD=120
            ;;
    esac
    case "$ANIWORLD_HEALTH_INTERVAL" in
        ''|*[!0-9]*)
            log "invalid ANIWORLD_HEALTH_INTERVAL; using 30"
            ANIWORLD_HEALTH_INTERVAL=30
            ;;
    esac
    if [ "$ANIWORLD_HEALTH_INTERVAL" -lt 1 ]; then
        ANIWORLD_HEALTH_INTERVAL=30
    fi
    case "$ANIWORLD_HEALTH_RETRIES" in
        ''|*[!0-9]*)
            log "invalid ANIWORLD_HEALTH_RETRIES; using 3"
            ANIWORLD_HEALTH_RETRIES=3
            ;;
    esac
    if [ "$ANIWORLD_HEALTH_RETRIES" -lt 1 ]; then
        ANIWORLD_HEALTH_RETRIES=3
    fi

    # Docker records an unhealthy state but does not apply restart policies to
    # it. Mirror the Compose healthcheck cadence here and terminate the app
    # after repeated failures; PID 1 then exits and `restart: unless-stopped`
    # can actually recover the container.
    (
        sleep "$ANIWORLD_HEALTH_START_PERIOD"
        failures=0
        while kill -0 "$app_pid" 2>/dev/null; do
            if python /usr/local/bin/healthcheck.py >/dev/null 2>&1; then
                failures=0
            else
                failures=$((failures + 1))
                log "healthcheck failure $failures/$ANIWORLD_HEALTH_RETRIES"
                if [ "$failures" -ge "$ANIWORLD_HEALTH_RETRIES" ]; then
                    python /usr/local/bin/healthcheck.py || true
                    log "healthcheck failure limit reached; restarting container"
                    kill -TERM "$app_pid" 2>/dev/null || true
                    return
                fi
            fi
            sleep "$ANIWORLD_HEALTH_INTERVAL"
        done
    ) &
    watchdog_pid=$!
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

# A sentinel on the remote media filesystem prevents an absent SSHFS/NFS
# mount from looking like a valid empty host directory. It is opt-in so normal
# local-volume deployments require no migration.
if [ -n "${ANIWORLD_STORAGE_SENTINEL:-}" ] && [ ! -f "$ANIWORLD_STORAGE_SENTINEL" ]; then
    log "error: storage sentinel is missing: $ANIWORLD_STORAGE_SENTINEL"
    exit 1
fi

start_xvfb

if [ "$#" -gt 0 ]; then
    "$@" &
else
    aniworld --web-ui --web-expose --no-browser --web-port "$ANIWORLD_WEB_PORT" &
fi
app_pid=$!
start_health_watchdog

# `wait` returns as soon as a signal arrives, whether or not the child died, so
# loop until the app is really gone and keep its real exit code.
exit_code=0
while kill -0 "$app_pid" 2>/dev/null; do
    wait "$app_pid" && exit_code=0 || exit_code=$?
done

if [ -n "$watchdog_pid" ] && kill -0 "$watchdog_pid" 2>/dev/null; then
    kill -TERM "$watchdog_pid" 2>/dev/null || true
fi
if [ -n "$xvfb_pid" ] && kill -0 "$xvfb_pid" 2>/dev/null; then
    kill -TERM "$xvfb_pid" 2>/dev/null || true
fi
exit "$exit_code"
