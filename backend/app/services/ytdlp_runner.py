"""yt-dlp wrapped as a library: a live-status probe and a download call.

Both take a plain options dict built from settings by the worker. The download
call returns the final on-disk path so the worker can ffprobe and record it.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp

from ..logging_config import YtdlpLogger
from ..paths import CONFIG_DIR

log = logging.getLogger("archiver.ytdlp")
# yt-dlp's own extractor/downloader output, routed through our logging so it
# lands in the same stdout/file logs (verbose only when we're at DEBUG).
_ytdlp_log = YtdlpLogger(logging.getLogger("archiver.ytdlp.yt_dlp"))

LIVE_STATUSES = {"is_live", "is_upcoming", "post_live"}

# Persist yt-dlp's cache (incl. the downloaded EJS solver) so it isn't re-fetched
# on every job and survives restarts.
_CACHE_DIR = str(CONFIG_DIR / "yt-dlp-cache")


@dataclass
class ProbeResult:
    is_live: bool
    live_status: Optional[str]
    title: str
    duration: Optional[int]


def _base_opts(settings: dict[str, Any]) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": settings["format_selector"],
        "merge_output_format": settings["merge_container"],
        # yt-dlp's own per-download retries (within a single run); honor the
        # user setting rather than a hardcoded value.
        "retries": int(_num(settings.get("max_retries")) or 3),
        "ignoreerrors": False,
    }
    if settings.get("rate_limit"):
        opts["ratelimit"] = _parse_rate(settings["rate_limit"])
    # Randomized pre-download sleep (yt-dlp only randomizes when max >= min).
    sleep_min = _num(settings.get("sleep_interval"))
    sleep_max = _num(settings.get("max_sleep_interval"))
    if sleep_min:
        opts["sleep_interval"] = sleep_min
    if sleep_max and sleep_max >= (sleep_min or 0):
        opts["max_sleep_interval"] = sleep_max
    _apply_common(opts, settings)
    return opts


def _configure_verbosity(opts: dict[str, Any]) -> None:
    """Route yt-dlp's logging into ours; go verbose when we're at DEBUG.

    Attaching the logger even when quiet still captures yt-dlp's warnings and
    errors (the useful bits for diagnosing failures); DEBUG additionally emits
    the full extractor/format-selection/HTTP trace.
    """
    opts["logger"] = _ytdlp_log
    if log.isEnabledFor(logging.DEBUG):
        opts["verbose"] = True
        opts["quiet"] = False
        opts["no_warnings"] = False


def _apply_common(opts: dict[str, Any], settings: dict[str, Any]) -> None:
    """Options shared by both the probe and the download call."""
    opts["cachedir"] = _CACHE_DIR
    _configure_verbosity(opts)
    # Sleep between the many HTTP requests yt-dlp makes while extracting info.
    # Applied to the probe too, since that also hits YouTube's extractor.
    req_sleep = _num(settings.get("sleep_interval_requests"))
    if req_sleep:
        opts["sleep_interval_requests"] = int(req_sleep)
    if settings.get("cookies_file"):
        opts["cookiefile"] = settings["cookies_file"]
    extractor_args = _extractor_args(settings)
    if extractor_args:
        opts["extractor_args"] = extractor_args
    components = (settings.get("ytdlp_remote_components") or "").strip()
    if components:
        # Required for YouTube: enables yt-dlp's EJS signature/n challenge solver.
        opts["remote_components"] = [c.strip() for c in components.split(",")
                                     if c.strip()]


def _extractor_args(settings: dict[str, Any]) -> dict[str, Any]:
    """Build yt-dlp extractor_args, e.g. a preferred YouTube player client."""
    client = (settings.get("youtube_player_client") or "").strip()
    if client and client.lower() != "default":
        clients = [c.strip() for c in client.split(",") if c.strip()]
        return {"youtube": {"player_client": clients}}
    return {}


def _loggable_opts(opts: dict[str, Any]) -> dict[str, Any]:
    """A trimmed copy of the yt-dlp opts safe/useful to log (no hook objects)."""
    skip = {"logger", "progress_hooks", "postprocessor_hooks"}
    return {k: v for k, v in opts.items() if k not in skip}


def _num(value: Any) -> Optional[float]:
    """Coerce a setting to a positive number, or None if blank/invalid/<=0."""
    if value in (None, ""):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _parse_rate(value: str) -> Optional[int]:
    value = value.strip().upper()
    mult = 1
    if value.endswith("K"):
        mult, value = 1024, value[:-1]
    elif value.endswith("M"):
        mult, value = 1024 * 1024, value[:-1]
    elif value.endswith("G"):
        mult, value = 1024 * 1024 * 1024, value[:-1]
    try:
        return int(float(value) * mult)
    except ValueError:
        return None


def probe(url: str, settings: dict[str, Any]) -> ProbeResult:
    opts: dict[str, Any] = {"quiet": True, "no_warnings": True,
                            "skip_download": True}
    _apply_common(opts, settings)
    log.debug("probe %s opts=%s", url, _loggable_opts(opts))
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    live_status = info.get("live_status")
    is_live = bool(info.get("is_live")) or live_status in LIVE_STATUSES
    return ProbeResult(
        is_live=is_live,
        live_status=live_status,
        title=info.get("title", ""),
        duration=info.get("duration"),
    )


def download(url: str, dest_dir: Path, settings: dict[str, Any],
             progress_hook: Callable[[dict], None],
             postprocessor_hook: Optional[Callable[[dict], None]] = None,
             ) -> dict[str, Any]:
    """Download to ``dest_dir`` and return a summary dict.

    Keys: ``filepath``, ``title``, ``ext``, ``vcodec``, ``duration``.

    ``postprocessor_hook`` (optional) receives yt-dlp's post-processing events
    so callers can surface the merge/mux phase, which emits no download progress.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = _base_opts(settings)
    opts["outtmpl"] = str(dest_dir / settings["output_template"])
    opts["progress_hooks"] = [progress_hook]
    if postprocessor_hook is not None:
        opts["postprocessor_hooks"] = [postprocessor_hook]

    for token in shlex.split(settings.get("extra_ytdlp_args", "") or ""):
        # Only simple --flag=value style is supported for extra args.
        if token.startswith("--") and "=" in token:
            k, v = token[2:].split("=", 1)
            opts[k.replace("-", "_")] = v

    log.info("yt-dlp download %s -> %s", url, opts["outtmpl"])
    log.debug("download opts=%s", _loggable_opts(opts))
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    requested = (info.get("requested_downloads") or [{}])[0]
    filepath = requested.get("filepath") or info.get("_filename")
    log.debug("yt-dlp finished %s: filepath=%s format_id=%s ext=%s",
              url, filepath, info.get("format_id"), info.get("ext"))
    return {
        "filepath": filepath,
        "title": info.get("title", ""),
        "ext": Path(filepath).suffix.lstrip(".") if filepath else None,
        "vcodec": info.get("vcodec"),
        "duration": info.get("duration"),
    }
