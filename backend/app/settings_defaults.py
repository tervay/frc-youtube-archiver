"""Default values for every user-editable constant.

Every constant in the app lives here and is exposed in the Settings page. The
DB ``setting`` table stores overrides (JSON-encoded); anything not overridden
falls back to these defaults. ``env`` names an environment variable that seeds
the initial value on first boot (handy for the TBA key on Unraid).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .paths import DEFAULT_MEDIA_DIR


@dataclass(frozen=True)
class SettingSpec:
    key: str
    default: Any
    type: str  # "str" | "int" | "float" | "bool" | "password"
    label: str
    help: str = ""
    env: Optional[str] = None
    group: str = "General"


SETTINGS: list[SettingSpec] = [
    SettingSpec(
        "tba_api_key",
        "",
        "password",
        "TBA API Key",
        "Read key from thebluealliance.com/account.",
        env="TBA_API_KEY",
        group="TheBlueAlliance",
    ),
    SettingSpec(
        "scan_cron",
        "0 6 * * *",
        "str",
        "Scan schedule (cron)",
        "When to poll TBA for new videos. Standard 5-field cron.",
        group="Schedule",
    ),
    SettingSpec(
        "reconcile_cron",
        "30 * * * *",
        "str",
        "Reconcile schedule (cron)",
        "How often to re-scan the media folder for transcoded/missing files.",
        group="Schedule",
    ),
    SettingSpec(
        "media_root",
        DEFAULT_MEDIA_DIR,
        "str",
        "Media root",
        "Where downloads are written (shared with tdarr).",
        group="Storage",
    ),
    SettingSpec(
        "output_template",
        "%(title)s [%(id)s].%(ext)s",
        "str",
        "yt-dlp output template",
        "Filename template. Must keep [%(id)s] so reconciliation works.",
        group="Downloads",
    ),
    SettingSpec(
        "format_selector",
        "bestvideo*+bestaudio/best",
        "str",
        "yt-dlp format",
        "Format selector for highest quality.",
        group="Downloads",
    ),
    SettingSpec(
        "merge_container",
        "mkv",
        "str",
        "Merge container",
        "Container yt-dlp merges into (mkv accepts any codec).",
        group="Downloads",
    ),
    SettingSpec(
        "concurrent_downloads",
        2,
        "int",
        "Concurrent downloads",
        "How many videos download in parallel.",
        group="Downloads",
    ),
    SettingSpec(
        "max_retries",
        3,
        "int",
        "Max retries",
        "Retry attempts before marking a download failed.",
        group="Downloads",
    ),
    SettingSpec(
        "rate_limit",
        "5M",
        "str",
        "Rate limit",
        "Max download speed (yt-dlp --limit-rate), e.g. 5M = 5 MB/s. "
        "Blank = unlimited.",
        group="Downloads",
    ),
    SettingSpec(
        "sleep_interval_requests",
        1,
        "int",
        "Sleep between requests (s)",
        "Seconds to sleep between yt-dlp's HTTP requests during "
        "extraction (--sleep-requests). The main lever against YouTube's "
        "'this content isn't available, try again later' rate-limiting. "
        "0 = disabled.",
        group="Downloads",
    ),
    SettingSpec(
        "sleep_interval",
        5.0,
        "float",
        "Min sleep before download (s)",
        "Minimum seconds to sleep before each video download "
        "(--sleep-interval). 0 = disabled.",
        group="Downloads",
    ),
    SettingSpec(
        "max_sleep_interval",
        30.0,
        "float",
        "Max sleep before download (s)",
        "Upper bound for the randomized pre-download sleep "
        "(--max-sleep-interval). Must be >= min sleep to take effect.",
        group="Downloads",
    ),
    SettingSpec(
        "cookies_file",
        "",
        "str",
        "Cookies file",
        "Optional path to a cookies.txt (for age-restricted videos).",
        group="Downloads",
    ),
    SettingSpec(
        "ytdlp_remote_components",
        "ejs:github",
        "str",
        "yt-dlp JS solver components",
        "Required for YouTube: lets yt-dlp fetch its EJS signature/n "
        "challenge solver (needs the Deno runtime, bundled). Comma-"
        "separated; blank to disable. e.g. ejs:github",
        group="Downloads",
    ),
    SettingSpec(
        "youtube_player_client",
        "mweb,tv,web_safari",
        "str",
        "YouTube player client",
        "Comma-separated yt-dlp player clients, highest priority first. "
        "mweb (with the bundled PO-token provider) is preferred because the "
        "tv client's DASH URLs now get a mid-download HTTP 403 (SABR "
        "throttling) on was_live/post-live videos; tv/web_safari are kept "
        "as fallbacks. Blank/'default' lets yt-dlp choose.",
        group="Downloads",
    ),
    SettingSpec(
        "extra_ytdlp_args",
        "",
        "str",
        "Extra yt-dlp args",
        "Advanced: --flag=value style extra options passed to yt-dlp.",
        group="Downloads",
    ),
    SettingSpec(
        "live_buffer_days",
        1,
        "int",
        "Live buffer (days)",
        "Only consider an event's VODs once its end date is this many days past.",
        group="Discovery",
    ),
    SettingSpec(
        "log_level",
        "INFO",
        "str",
        "Log level",
        "Backend log verbosity: DEBUG | INFO | WARNING | ERROR. DEBUG "
        "adds full yt-dlp extractor/downloader output for diagnosing "
        "download failures. Applied on save (no restart needed). Logs go "
        "to the container stdout (docker logs) and /config/logs/archiver.log.",
        group="General",
    ),
    SettingSpec(
        "ganymede_enabled",
        False,
        "bool",
        "Enable Ganymede sync",
        "Track event Twitch webcasts in Ganymede (a Twitch archiver). "
        "Master switch — nothing runs unless this is on.",
        group="Ganymede",
    ),
    SettingSpec(
        "ganymede_base_url",
        "http://192.168.1.254:4000/api/v1",
        "str",
        "Ganymede API base URL",
        "Base URL of the Ganymede REST API, including /api/v1.",
        group="Ganymede",
    ),
    SettingSpec(
        "ganymede_api_key",
        "",
        "password",
        "Ganymede API key",
        "Bearer token minted in the Ganymede admin UI.",
        env="GANYMEDE_API_KEY",
        group="Ganymede",
    ),
    SettingSpec(
        "ganymede_sync_cron",
        "0 5 * * *",
        "str",
        "Ganymede sync schedule (cron)",
        "When to reconcile TBA Twitch webcasts against Ganymede's "
        "watched channels. Standard 5-field cron.",
        group="Ganymede",
    ),
    SettingSpec(
        "ganymede_resolution",
        "best",
        "str",
        "Live resolution",
        "Resolution Ganymede records for live capture.",
        group="Ganymede",
    ),
    SettingSpec(
        "ganymede_vod_resolution",
        "best",
        "str",
        "VOD resolution",
        "Resolution Ganymede records for VOD/archive downloads.",
        group="Ganymede",
    ),
    SettingSpec(
        "ganymede_watch_vod",
        True,
        "bool",
        "Archive past VODs",
        "Also archive a channel's past broadcasts (download_archives/"
        "watch_vod), not just future live streams.",
        group="Ganymede",
    ),
    SettingSpec(
        "ganymede_archive_chat",
        False,
        "bool",
        "Archive chat",
        "Also archive/render chat replay for tracked channels.",
        group="Ganymede",
    ),
]

SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}
