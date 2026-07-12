# FRC YouTube Archiver

Self-hosted Docker app that archives FRC (FIRST Robotics) YouTube videos on Justin's Unraid NAS.
Daily scans TheBlueAlliance (TBA) API v3 for new videos, downloads highest quality via yt-dlp,
and serves a React dashboard. No auth (LAN only).

## Deployment (Unraid)

- Image: `tervay/frc-archiver:latest` on Docker Hub (user `tervay`). Build locally, push, then
  **Justin pulls on the NAS himself** — never pull/deploy to the NAS unless explicitly told.
- Runs **without docker-compose**: `VOLUME`s baked into the Dockerfile; template XML at
  `unraid-frc-archiver.xml` (on NAS: `/boot/config/plugins/dockerMan/templates-user/`). NAS uses
  **port 2713**.
- Path mapping: host `/mnt/user/frc/` → container `/library/`. Config volume `/config`.
- Files must be owned by PUID/PGID (default 99:100 = Unraid nobody:users) so **tdarr can transcode
  in place**. `entrypoint.sh` creates the user and `exec gosu`s. Set via PUID/PGID env.

## The dedup + transcode model (core invariant)

- Dedup keys on the **stable YouTube video ID** (`Video.youtube_id`, UNIQUE) in SQLite — never on
  filenames. A scan re-run enqueues zero new jobs; only `skipped_live` or `force_redownload` rows
  re-queue.
- tdarr transcodes downloaded files to **AV1/MKV in place**, changing container/codec/extension.
  The **reconciler** walks the media root, extracts `[VIDEOID]` from filenames
  (`\[([A-Za-z0-9_-]{11})\](?=\.[^.]+$)`), ffprobes, and updates `current_*/transcoded/present`
  without re-downloading. Output template **must keep `[%(id)s]`** or reconciliation breaks.
- Files: `/library/<year>/<event_key>/<Title> [VIDEOID].<ext>`. **Team-source videos**
  (`Video.source_type == SourceType.match`) instead nest under the discovering team:
  `/library/<team>/<year>/<event_key>/...` (e.g. `frc2713/2026/2026necmp/...`). The team is the
  **first** key in the comma-separated `Video.team_keys` (the source that discovered it first).
  Built in one place — `_dest_dir()` in `services/worker.py`; season/district/manual videos keep
  the flat layout. A match shared by two team sources (e.g. add `frc2713`, later add `frc2791`)
  stays under the first team and is **not** re-downloaded — `youtube_id` dedup enqueues zero new
  jobs, and the reconciler's recursive `rglob` finds files at any depth (layout-agnostic). Existing
  files are never moved; only newly downloaded / `force_redownload`'d videos adopt the new path.

## Two discovery sources (Scanner)

1. **Event VODs** — `Event.webcasts[]` where `type == "youtube"`, scoped by `source` rows of kind
   `season` (year) or `district`. Gated on `_event_has_ended` (end_date past `live_buffer_days`)
   so live/in-progress streams are skipped. Second guard: yt-dlp probe `live_status` in
   is_live/is_upcoming/post_live → mark `skipped_live`.
2. **Team match videos** — `match.videos[]` type `youtube` for `source` kind `team`.

Non-YouTube webcasts (Twitch etc.) are ignored.

## YouTube extraction chain (all FOUR required — hard-won)

YouTube downloads fail without every piece; these are the correct settings defaults:
1. **cookies** — `/config/cookies.txt` (`cookies_file` setting). Fixes "Sign in to confirm you're
   not a bot".
2. **EJS solver** — `ytdlp_remote_components = "ejs:github"` fetches the signature/n-challenge
   solver. **Needs the Deno runtime** (bundled via `COPY --from=denoland/deno:bin`). Without it,
   only storyboards / "requested format not available".
3. **Player client** — `youtube_player_client = "mweb,tv,web_safari"` (mweb **first**). The
   default/web clients need a po_token and return **HTTP 403** on media
   (`default`/`tv_embedded`/`android_vr` → 403; `ios` → no formats). The `tv` client's DASH URLs
   now also get a **mid-download 403 (~20MB in)** on was_live/post-live videos (SABR throttling),
   so it must not be first — yt-dlp resolves a shared format id (e.g. 303) to the first-listed
   client. `mweb` + a GVS PO token (piece 4) downloads cleanly; tv/web_safari stay as fallbacks.
4. **GVS PO token** — YouTube now also requires a *GVS PO token* for `web_safari`/`mweb` DASH
   formats (and post-live/`was_live` streams); without it the media URLs 403 even after format
   selection succeeds. Provided by the bundled **bgutil-ytdlp-pot-provider** (`==1.3.1`): the
   Deno server is copied from `brainicism/bgutil-ytdlp-pot-provider:1.3.1-deno` into
   `/opt/bgutil-provider` and started by `entrypoint.sh` on `127.0.0.1:4416`; the matching pip
   plugin is yt-dlp-auto-discovered (no settings/extractor-args needed). Keep the plugin version
   pinned to the server version. node-canvas ships its own graphics libs, so no extra apt deps.

yt-dlp is **unpinned** (`yt-dlp>=2025.6.30`) so rebuilds get current releases.

**Retry hygiene:** on failure the worker (`_handle_failure` → `_cleanup_partials`) deletes the
video's `.part`/`.ytdl`/`.part-Frag*` before requeueing, so a stale partial isn't *resumed*
against a freshly-signed URL (which kept the 403 permanent across all attempts).

## Stack / layout

- Backend: Python 3.12, FastAPI + Uvicorn, SQLModel over SQLite, APScheduler, httpx (TBA client
  with ETag/304 caching), yt-dlp as a library in a ThreadPoolExecutor, ffmpeg/ffprobe.
- Frontend: React + TS + Vite + react-router-dom, plain CSS dark theme (no Tailwind/shadcn), custom
  fetch wrapper, SSE (`EventSource`) for live progress.
- Key files:
  - `backend/app/models.py` — Video (dedup source of truth), DownloadJob, Source, Setting,
    TbaCache, ScanRun.
  - `backend/app/settings_defaults.py` — every user-editable constant + defaults (see chain above).
  - `backend/app/services/{scanner,worker,ytdlp_runner,reconciler}.py`.
  - `backend/app/api/{videos,queue,sources,settings,actions,stats}.py`.
  - `frontend/src/pages/{Dashboard,History,Queue,Sources,Settings,Logs}.tsx`.

## Conventions / gotchas

- **Always run `black backend/` before building/committing backend changes** (e.g.
  `mise exec -- uv run --python 3.12 python -m black backend/`, or `mise exec -- uvx black backend/`
  if black isn't installed). Python code must stay black-formatted.
- **Timestamps are UTC.** Backend stores naive-UTC; frontend `parseDate` appends "Z" before
  parsing or everything shows "just now" / wrong local time.
- Active queue orders by `DownloadJob.id` ascending = download order (downloading first, then
  next-up).
- Download size: yt-dlp reports **per-stream** totals (last hook = audio, ~357MB). `_combined_total`
  sums `requested_formats` filesizes so Size shows true combined (~11.7GB); self-corrects via
  ffprobe on completion. `_make_hook` tracks cumulative bytes across streams for continuous 0-100%.
- Merge/post-processing phase shows as a badge (status ends with "…"), speed/eta hidden then.
- Bulk retry: `POST /queue/retry-failed` (Queue page "Retry all failed" button) requeues all
  `Video.status == failed`.
- On startup `recover_interrupted_jobs()` resets running→pending (mid-download at restart).
- Tests: `backend/tests/` — run with pytest. Scanner client is injectable for fixture tests.
  Deps live in `backend/pyproject.toml` (a **uv** project: runtime deps + a `dev` group; no
  `uv.lock` is committed so rebuilds re-resolve and keep `yt-dlp` current). Run tests from
  `backend/` — `uv run` auto-syncs the deps incl. the `dev` group (Python **3.12** — 3.14 has no
  prebuilt wheels for the pinned `pydantic-core`):
  `mise exec -- uv run --python 3.12 python -m pytest`.
- **Runtimes are mise-managed.** Root `mise.toml` pins `python`/`node`/`uv` (npm ships with node)
  for both local dev and the **Docker image** — the Dockerfile installs those same versions via
  mise instead of `FROM python:/node:` base tags, so there's one source of truth and no image tags
  to bump. Deno is the exception (copied from `denoland/deno:bin`). mise tools land under
  `/opt/mise` (world-readable so the dropped PUID/PGID user can exec the interpreter the venv
  points at).
- **Test isolation gotcha:** `app/paths.py` reads `ARCHIVER_CONFIG_DIR` into a module-level
  `CONFIG_DIR` **at import time** (default `/config`). The `temp_env` fixture sets the env var, but
  if `app.paths` was already imported by an earlier non-`temp_env` test, `CONFIG_DIR` stays frozen
  to `/config`. Running the **full suite** outside Docker then fails `temp_env` tests with
  `PermissionError: '/config'` (each test file passes in isolation). In Docker/Unraid `/config`
  exists and is writable so it passes. Real fix (not yet done) would be reading `CONFIG_DIR` lazily
  or reloading `app.paths` in the fixture.
