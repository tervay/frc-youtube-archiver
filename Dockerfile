# --- Stage 1: build the React SPA -------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- Stage 1b: bgutil PO-token provider -------------------------------------
# Prebuilt Deno server that mints YouTube GVS PO tokens. We bundle its /app
# (server sources + node_modules + Deno cache) and run it in-container so a
# single container needs no sidecar. node-canvas ships its own graphics libs;
# its only extra system deps (libresolv, libuuid) are already in python:slim.
FROM brainicism/bgutil-ytdlp-pot-provider:1.3.1-deno AS potprovider

# --- Stage 2: python runtime ------------------------------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    ARCHIVER_CONFIG_DIR=/config \
    ARCHIVER_MEDIA_DIR=/library \
    ARCHIVER_STATIC_DIR=/app/static

# ffmpeg/ffprobe are required by yt-dlp (merge) and the reconciler (codec probe).
# gosu drops root to PUID/PGID at startup (see entrypoint.sh).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

# Deno is the JS runtime yt-dlp uses to solve YouTube's nsig/signature
# challenges (and to run the bundled PO-token provider below); without it
# YouTube extraction breaks on many videos.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Bundle the bgutil PO-token provider server (started by entrypoint.sh on
# 127.0.0.1:4416) so yt-dlp can obtain GVS PO tokens without a sidecar.
COPY --from=potprovider /app /opt/bgutil-provider
ENV BGUTIL_PROVIDER_DIR=/opt/bgutil-provider

WORKDIR /app
COPY backend/requirements.txt ./
# The client plugin (auto-discovered by yt-dlp) must match the provider server.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "bgutil-ytdlp-pot-provider==1.3.1"

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
