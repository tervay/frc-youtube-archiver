#!/bin/sh
# Drop privileges to PUID/PGID so downloaded files are owned by the same user as
# your Unraid share (default nobody:users = 99:100). This lets tdarr replace the
# files in place during transcoding.
set -e

PUID=${PUID:-99}
PGID=${PGID:-100}

groupmod -o -g "$PGID" archiver 2>/dev/null || groupadd -o -g "$PGID" archiver
usermod  -o -u "$PUID" -g "$PGID" archiver 2>/dev/null \
  || useradd -o -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin archiver

# /config holds the SQLite DB; make sure the app can write it. /library is only
# chowned at the top level so we never recurse over an existing (huge) share.
mkdir -p /config /library
chown -R "$PUID:$PGID" /config
chown "$PUID:$PGID" /library 2>/dev/null || true

# Start the bundled bgutil PO-token provider on 127.0.0.1:4416 so yt-dlp can
# obtain YouTube GVS PO tokens (auto-discovered at the default port). Run it as
# the archiver user; its Deno cache must be writable since our bundled Deno may
# differ from the one the server was built with and can recompile on first run.
# Failure to start must not block the app — downloads simply 403 as before.
PROVIDER_DIR=${BGUTIL_PROVIDER_DIR:-/opt/bgutil-provider}
if [ -f "$PROVIDER_DIR/src/main.ts" ]; then
  chown -R "$PUID:$PGID" "$PROVIDER_DIR/.cache" 2>/dev/null || true
  echo "Starting bgutil PO-token provider on 127.0.0.1:4416"
  gosu "$PUID:$PGID" env \
    DENO_DIR="$PROVIDER_DIR/.cache/deno" \
    DENO_NO_UPDATE_CHECK=1 \
    deno run -A "$PROVIDER_DIR/src/main.ts" &
else
  echo "WARNING: bgutil provider not found at $PROVIDER_DIR; PO tokens disabled"
fi

echo "Starting FRC Archiver as ${PUID}:${PGID}"
exec gosu "$PUID:$PGID" "$@"
