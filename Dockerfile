# Runtimes (python, node, uv) are provisioned by mise from the repo-root
# mise.toml in every stage, so there are no base-image version tags to keep in
# sync with local dev — mise.toml is the single source of truth. Deno is the one
# exception: it's copied from the official image (not in the user-facing set).

# --- Stage 1: build the React SPA -------------------------------------------
FROM debian:bookworm-slim AS frontend
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# mise installs node (npm ships with it); shims put node/npm on PATH so npm's
# `env node` child processes (tsc, vite) resolve too.
ENV MISE_DATA_DIR=/opt/mise \
    MISE_CONFIG_DIR=/opt/mise \
    MISE_CACHE_DIR=/opt/mise/cache \
    MISE_NOT_FOUND_AUTO_INSTALL=false \
    PATH="/opt/mise/shims:$PATH"
RUN curl -fsSL https://mise.run | MISE_INSTALL_PATH=/usr/local/bin/mise sh
WORKDIR /frontend
COPY mise.toml ./
RUN mise trust && mise install node && mise reshim
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- Stage 1b: bgutil PO-token provider -------------------------------------
# Prebuilt Deno server that mints YouTube GVS PO tokens. We bundle its /app
# (server sources + node_modules + Deno cache) and run it in-container so a
# single container needs no sidecar. node-canvas ships its own graphics libs;
# its only extra system deps (libresolv, libuuid) are already in debian:slim.
FROM brainicism/bgutil-ytdlp-pot-provider:1.3.1-deno AS potprovider

# --- Stage 2: python runtime ------------------------------------------------
FROM debian:bookworm-slim
ENV PYTHONUNBUFFERED=1 \
    ARCHIVER_CONFIG_DIR=/config \
    ARCHIVER_MEDIA_DIR=/library \
    ARCHIVER_STATIC_DIR=/app/static

# ffmpeg/ffprobe are required by yt-dlp (merge) and the reconciler (codec probe).
# gosu drops root to PUID/PGID at startup (see entrypoint.sh). curl bootstraps
# mise; ca-certificates for the runtime downloads it makes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl gosu \
    && rm -rf /var/lib/apt/lists/*

# mise provisions python + uv (precompiled python, so no build toolchain). Tools
# live under /opt/mise, made world-readable below so the dropped PUID/PGID user
# can execute the interpreter the app's venv points at.
ENV MISE_DATA_DIR=/opt/mise \
    MISE_CONFIG_DIR=/opt/mise \
    MISE_CACHE_DIR=/opt/mise/cache \
    MISE_NOT_FOUND_AUTO_INSTALL=false \
    MISE_PYTHON_COMPILE=0 \
    PATH="/opt/mise/shims:$PATH"
RUN curl -fsSL https://mise.run | MISE_INSTALL_PATH=/usr/local/bin/mise sh

# Deno is the JS runtime yt-dlp uses to solve YouTube's nsig/signature
# challenges (and to run the bundled PO-token provider below); without it
# YouTube extraction breaks on many videos.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Bundle the bgutil PO-token provider server (started by entrypoint.sh on
# 127.0.0.1:4416) so yt-dlp can obtain GVS PO tokens without a sidecar.
COPY --from=potprovider /app /opt/bgutil-provider
ENV BGUTIL_PROVIDER_DIR=/opt/bgutil-provider

WORKDIR /app
COPY mise.toml ./
RUN mise trust && mise install python uv && mise reshim

# uv resolves & installs the backend deps from pyproject.toml into /app/.venv,
# using mise's python explicitly (never uv's own download). No uv.lock is
# committed on purpose, so each build re-resolves and yt-dlp lands on a current
# release (see pyproject). --no-dev skips the test-only group; the venv goes on
# PATH so the CMD (and gosu in entrypoint.sh) find uvicorn.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:/opt/mise/shims:$PATH"
COPY backend/pyproject.toml ./
RUN uv sync --no-dev --python "$(mise which python)" \
    && chmod -R go+rX /opt/mise /app/.venv

COPY backend/app ./app
COPY --from=frontend /frontend/dist ./static
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config", "/library"]
EXPOSE 8000

# PUID/PGID default to Unraid's nobody:users; override with -e on `docker run`.
ENV PUID=99 PGID=100
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
