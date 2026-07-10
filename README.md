# FRC Archiver

Self-hosted YouTube archiver for FRC (FIRST Robotics Competition) videos. It
polls [TheBlueAlliance](https://www.thebluealliance.com/) once a day, discovers
new videos, downloads them at the highest available quality with
[yt-dlp](https://github.com/yt-dlp/yt-dlp), and serves a web dashboard to watch
progress, browse history, manage the queue, and edit every setting.

Built to run as a single Docker container on Unraid, with downloads written to a
share that [tdarr](https://tdarr.io/) later transcodes to AV1/MKV.

## What it archives

- **Event livestream VODs** — every YouTube webcast on an `Event` (0, 1, or many
  per event), scoped by the **seasons** and **districts** you track. Non-YouTube
  webcasts (Twitch, etc.) are ignored.
- **Team match videos** — every YouTube match video for the **teams** you track.

Streams that are still **live / upcoming** are never downloaded: an event's VODs
are only considered once its end date has passed (configurable buffer), and each
URL is probed with yt-dlp right before download and skipped if it reports
`is_live` / `is_upcoming` / `post_live`. Skipped streams are retried on the next
scan.

## How de-duplication survives tdarr

Every download is tracked in SQLite keyed on the **YouTube video ID** — never on
the filename. yt-dlp embeds that ID in the filename (`Title [VIDEOID].ext`), and
a periodic **reconciliation scan** walks the media folder, pulls the `[VIDEOID]`
token out of each file regardless of extension, and refreshes its on-disk state.
When tdarr re-encodes `…​.mp4` (h264) into `…​.mkv` (av1) **in place**, the row is
updated and flagged `transcoded` — and a video is never re-downloaded just
because its container/codec/extension changed. You can still force a re-download
per-video from the History page.

## Files on disk

```
/media/<year>/<event_key>/<Title> [VIDEOID].<ext>
```

Keep the `[%(id)s]` token in the output template (Settings → Downloads) or
reconciliation can't match files back to rows.

## Running

The container exposes two mount points; bind them to host paths at run time:

| Container path | Purpose |
| --- | --- |
| `/config` | SQLite DB + optional `cookies.txt` |
| `/library` | download target — bind to the share tdarr watches |

Port `8000` inside the container is the dashboard.

### Docker Compose

1. Copy `.env.example` to `.env` and set `TBA_API_KEY` (or add it later in the UI).
2. Edit `docker-compose.yml` — the `/library` volume maps `/mnt/user/frc`.
3. `docker compose up -d --build`
4. Open `http://<host>:8080`, go to **Sources**, add a team / season / district,
   then **Settings** to confirm the TBA key, then **Dashboard → Scan now**.

### Unraid (no compose)

Unraid doesn't use compose. Build the image once on the box, then run it from
the **Docker** tab template or a `docker run` command.

**1. Build the image on the Unraid host** (host paths are supplied at run time —
they are never baked into the image):

```bash
cd /boot/config/plugins/… /frc-archiver   # wherever you cloned this repo
docker build -t frc-archiver:latest .
```

**2a. Import the template** — copy `unraid-frc-archiver.xml` to
`/boot/config/plugins/dockerMan/templates-user/` and it appears under
**Add Container** with editable Port/Path/Variable fields (Library defaults to
`/mnt/user/frc`).

**2b. …or just `docker run`:**

```bash
docker run -d --name frc-archiver \
  -p 8080:8000 \
  -v /mnt/user/appdata/frc-archiver:/config \
  -v /mnt/user/frc:/library \
  -e TBA_API_KEY=your_key_here \
  -e PUID=99 -e PGID=100 \
  --restart unless-stopped \
  frc-archiver:latest
```

`PUID`/`PGID` default to `99:100` (Unraid `nobody:users`) so downloaded files are
owned correctly for tdarr to transcode in place. No authentication is built in —
keep it on your LAN or behind your own reverse proxy.

## Building & publishing the image

You can either build the image directly on the Unraid box (above) or build it on
your dev machine and push it to Docker Hub so Unraid just pulls it — usually the
tidier workflow.

### Build locally

```bash
docker build -t frc-archiver:latest .
```

Unraid runs on **linux/amd64**. If you build on an Apple Silicon / ARM machine,
target that platform explicitly (needs Buildx, included with Docker Desktop):

```bash
docker build --platform linux/amd64 -t frc-archiver:latest .
```

### Push to Docker Hub

Replace `<dockerhub-user>` with your Docker Hub username.

```bash
docker login                                              # once, enter your credentials

# Tag the local build for your Docker Hub repo:
docker tag frc-archiver:latest <dockerhub-user>/frc-archiver:latest

# Push it:
docker push <dockerhub-user>/frc-archiver:latest
```

To build and push a specific version in one step (and cover amd64 for Unraid),
use Buildx:

```bash
docker buildx build --platform linux/amd64 \
  -t <dockerhub-user>/frc-archiver:latest \
  -t <dockerhub-user>/frc-archiver:1.0.0 \
  --push .
```

### Point Unraid at the pushed image

In the template / `docker run`, set the repository to
`<dockerhub-user>/frc-archiver:latest` instead of the local `frc-archiver:latest`.
In `unraid-frc-archiver.xml` that's the `<Repository>` field. Unraid's **Check
for Updates** / **Force Update** will then pull new versions you push.

## Configuration

Everything is editable at runtime under **Settings** and stored in the DB:
TBA API key · scan schedule (cron) · reconcile schedule · media root · live
buffer (days) · yt-dlp format / output template / merge container · concurrent
downloads · rate limit · max retries · cookies file · YouTube player client ·
extra yt-dlp args.

### YouTube extraction (important)

YouTube regularly breaks older yt-dlp and requires a JavaScript runtime to solve
its `nsig`/signature challenges. The Docker image handles both:

- **Deno** (the JS runtime yt-dlp uses) is bundled in the image.
- yt-dlp is installed **unpinned**, so each rebuild pulls a current release. If
  downloads start failing, `docker compose build --pull --no-cache` to refresh it.

If a video fails with *"not available on this app"*, set **YouTube player client**
in Settings to something like `tv,web_safari` (comma-separated). For
age-restricted content, point **Cookies file** at a `cookies.txt` under `/config`.

## Development

Backend (needs Python 3.12 + ffmpeg):

```bash
cd backend
pip install -r requirements-dev.txt
ARCHIVER_CONFIG_DIR=./data ARCHIVER_MEDIA_DIR=./media \
  uvicorn app.main:app --reload      # API on :8000
pytest                               # run the test suite
```

Frontend:

```bash
cd frontend
npm install
npm run dev      # Vite on :5173, proxies /api to :8000
```

## Architecture

FastAPI serves the JSON/SSE API and the built React SPA from one port. SQLite is
the source of truth. APScheduler runs the daily scan and the reconcile scan. A
threaded worker pool runs yt-dlp jobs and streams live progress to the dashboard
over Server-Sent Events. See `PLAN` / the plan file for the full design.
```
