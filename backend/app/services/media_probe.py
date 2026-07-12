"""ffprobe helpers to read a file's real container/codec/size.

Used both right after download (to record what we fetched) and by the
reconciler (to detect tdarr's AV1/MKV re-encode).
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class MediaInfo:
    container: Optional[str]
    vcodec: Optional[str]
    size: Optional[int]
    duration: Optional[int]
    width: Optional[int] = None
    height: Optional[int] = None


def ffprobe(path: str | Path) -> MediaInfo:
    p = Path(path)
    size = p.stat().st_size if p.exists() else None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(p)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return MediaInfo(container=p.suffix.lstrip("."), vcodec=None,
                         size=size, duration=None, width=None, height=None)

    fmt = data.get("format", {})
    container = fmt.get("format_name")
    duration = None
    if fmt.get("duration"):
        try:
            duration = int(float(fmt["duration"]))
        except ValueError:
            duration = None

    vcodec = None
    width = None
    height = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            vcodec = stream.get("codec_name")
            width = stream.get("width")
            height = stream.get("height")
            break

    return MediaInfo(container=container, vcodec=vcodec, size=size,
                     duration=duration, width=width, height=height)
