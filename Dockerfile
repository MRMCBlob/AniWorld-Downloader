# ==========================================
# Stage 1: Build virtual env and dependencies
# ==========================================
FROM python:3.13-slim AS builder

WORKDIR /build

# Support proxy CA certificate and HTTP/HTTPS proxies if they are set in the environment
ARG PROXY_CA_CERT_B64
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG TARGETARCH

# Setup HTTPS sources for Debian, trust the proxy certificate if provided, and install upx-ucl (with caching)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && \
    if [ -n "$PROXY_CA_CERT_B64" ]; then \
        echo "Trusting custom proxy CA certificate..." && \
        echo "$PROXY_CA_CERT_B64" | base64 -d > /usr/local/share/ca-certificates/proxy-ca.crt && \
        update-ca-certificates; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends --option=Apt::Retries=3 upx-ucl



# Create a virtual environment for the app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# Upgrade pip using cache
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip

# Pre-install patchright and Chromium to cache this huge layer independently of the project's dependencies
# This runs BEFORE copying any project files so that UPX compression is permanently cached.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/root/.cache/ms-playwright \
    pip install patchright && \
    python -m patchright install chromium && \
    rm -rf /ms-playwright/ffmpeg-* && \
    find /ms-playwright -name "*.pak*" | grep -vE "(resources|chrome_100|chrome_200|de|en-US|en-GB)\.pak" | xargs -r rm -f && \
    headless_dir=$(ls -d /ms-playwright/chromium_headless_shell-* | head -n 1) && \
    chrome_dir=$(ls -d /ms-playwright/chromium-* | grep -v headless_shell | head -n 1) && \
    rm -rf "$headless_dir" && \
    ln -s "$(basename "$chrome_dir")" "$headless_dir" && \
    chrome_inner_dir=$(ls -d "$chrome_dir"/chrome-* | head -n 1) && \
    ln -s chrome "$chrome_inner_dir/headless_shell" && \
    arch=$(dpkg --print-architecture) && \
    if [ "$arch" = "amd64" ]; then \
        upx -9 /opt/venv/lib/python3.13/site-packages/patchright/driver/node; \
        upx -9 "$chrome_inner_dir/chrome"; \
    fi && \
    chmod -R a+rX /ms-playwright && \
    rm -rf /tmp/*

# Copy packaging metadata first to maximize caching
COPY pyproject.toml README.md LICENSE MANIFEST.in /build/

# Install dependencies (including optional dependencies like discord/sso)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .[all]

# Copy the application source code
COPY src/ /build/src/

# Install the application package into the virtual env (using cache)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .[all]

# Clean up python bytecodes and packaging tools in builder venv
RUN find /opt/venv -type d -name "__pycache__" -exec rm -rf {} + && \
    pip uninstall -y pip setuptools wheel


# ==========================================
# Stage 2: Final minimal runner image
# ==========================================
FROM python:3.13-slim AS runner

WORKDIR /app

# Support proxy CA certificate and HTTP/HTTPS proxies if they are set in the environment
ARG PROXY_CA_CERT_B64
ARG http_proxy
ARG https_proxy
ARG no_proxy

# Setup HTTPS sources for Debian and trust the proxy certificate if provided (with cache)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && \
    if [ -n "$PROXY_CA_CERT_B64" ]; then \
        echo "Trusting custom proxy CA certificate..." && \
        echo "$PROXY_CA_CERT_B64" | base64 -d > /usr/local/share/ca-certificates/proxy-ca.crt && \
        update-ca-certificates; \
    fi

# Create unprivileged user
RUN adduser --disabled-password --gecos "" aniworld \
    && mkdir -p /config /media/downloads/aniworld/incomplete /media/downloads/aniworld/completed \
    && chown -R aniworld:aniworld /app /home/aniworld /config /media

# Install minimal system dependencies (xvfb and core Chromium shared libraries) (with cache)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends --option=Apt::Retries=3 \
    xvfb \
    ffmpeg \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxext6

# Copy virtual env, playwright browsers, and compressed static ffmpeg/ffprobe from builder stage
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /ms-playwright /ms-playwright

# Entrypoint and healthcheck scripts
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod +x /usr/local/bin/entrypoint.sh

# Environments
#
# The defaults describe the homelab layout documented in docs/DOCKER.md: the
# whole media mount at /media, config on its own volume at /config. Config is
# deliberately not under /media — SQLite over a network mount has unreliable
# file locking, and the queue database lives there.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANIWORLD_INSTALL_FOLDER=/config \
    ANIWORLD_LOG_DIR=/config/logs \
    ANIWORLD_DOWNLOAD_PATH=/media/downloads/aniworld/incomplete \
    ANIWORLD_COMPLETED_PATH=/media/downloads/aniworld/completed \
    ANIWORLD_WEB_PORT=8080 \
    ANIWORLD_DOCKER=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

USER aniworld

EXPOSE 8080

VOLUME ["/config"]

# start-period is generous: the first boot resolves dependencies and warms up
# the Chromium profile, which takes well over a minute on a small VM.
HEALTHCHECK --interval=30s --timeout=15s --start-period=120s --retries=3 \
    CMD ["python", "/usr/local/bin/healthcheck.py"]

# Inherited by the final stage
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]


# ==========================================
# Stage 3: Squashed final runner
# ==========================================
FROM scratch AS final

# Copy the entire root filesystem from the runner stage to squash all layers
COPY --from=runner / /

# Redeclare all necessary metadata since scratch starts empty — a squashed
# stage inherits no ENV, no VOLUME, no HEALTHCHECK and no ENTRYPOINT. Anything
# added to the runner stage above must be repeated here or it silently
# disappears from the published image.
ENV PATH="/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANIWORLD_INSTALL_FOLDER=/config \
    ANIWORLD_LOG_DIR=/config/logs \
    ANIWORLD_DOWNLOAD_PATH=/media/downloads/aniworld/incomplete \
    ANIWORLD_COMPLETED_PATH=/media/downloads/aniworld/completed \
    ANIWORLD_WEB_PORT=8080 \
    ANIWORLD_DOCKER=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

USER aniworld

EXPOSE 8080

VOLUME ["/config"]

HEALTHCHECK --interval=30s --timeout=15s --start-period=120s --retries=3 \
    CMD ["python", "/usr/local/bin/healthcheck.py"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]