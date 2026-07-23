"""Video stream facts and near-dup signatures via ffprobe/ffmpeg (spec Stage 1).

``video_sig`` is (rounded duration, resolution, pHash of 3 keyframes at
10/50/90%). When ffprobe/ffmpeg are unavailable the video is still cataloged
(hash, size, metadata) with a ``no_ffprobe`` flag and a NULL signature, so
ingest never blocks; dedup later treats such videos conservatively.

Usage:
    >>> from pathlib import Path
    >>> from archive_pipeline.video import video_signature
    >>> sig = video_signature(Path("clip.mp4"))  # doctest: +SKIP
    >>> sig.duration_s  # doctest: +SKIP
    12.4
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import imagehash
from PIL import Image

_KEYFRAME_POSITIONS = (0.10, 0.50, 0.90)
_SUBPROCESS_TIMEOUT_S = 120


@cache
def ffmpeg_available() -> bool:
    """True when both ffprobe and ffmpeg are on PATH.

    Usage:
        >>> isinstance(ffmpeg_available(), bool)
        True
    """
    return shutil.which("ffprobe") is not None and shutil.which("ffmpeg") is not None


@dataclass(frozen=True)
class VideoFacts:
    """Stream facts and signature for one video; NULLs plus a flag on failure."""

    duration_s: float | None
    width: int | None
    height: int | None
    video_sig: str | None
    flag: str | None  # no_ffprobe | video_unreadable | None


def _ffprobe(path: Path) -> tuple[float, int, int] | None:
    """Return (duration_s, width, height) or None if the file is unreadable."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        info = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if not info:
        return None
    stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"), None
    )
    duration_raw = info.get("format", {}).get("duration") or (stream or {}).get("duration")
    if stream is None or duration_raw is None:
        return None
    try:
        return float(duration_raw), int(stream["width"]), int(stream["height"])
    except (KeyError, TypeError, ValueError):
        return None


def _keyframe_phash(path: Path, at_s: float) -> str | None:
    """pHash of the frame at ``at_s`` seconds, extracted via ffmpeg to a pipe."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-ss", f"{at_s:.3f}", "-i", str(path),
                "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-",
            ],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        with Image.open(io.BytesIO(result.stdout)) as frame:
            return str(imagehash.phash(frame.convert("RGB")))
    except Exception:
        return None


def extract_frame(path: Path, at_s: float = 1.0) -> Image.Image | None:
    """Grab one representative frame as a PIL image (for review thumbnails).

    Seeks ``at_s`` seconds in (avoiding a black intro frame) and falls back to
    the first frame for very short clips. Returns None if ffmpeg is missing or
    the video is unreadable.

    Usage:
        >>> frame = extract_frame(Path("clip.mp4"))  # doctest: +SKIP
        >>> frame.size  # doctest: +SKIP
        (1920, 1080)
    """
    if not ffmpeg_available():
        return None
    for seek in (at_s, 0.0):
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-ss", f"{seek:.3f}", "-i", str(path),
                    "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-",
                ],
                capture_output=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0 and result.stdout:
            try:
                frame = Image.open(io.BytesIO(result.stdout))
                frame.load()
                return frame
            except Exception:
                return None
    return None


def video_signature(path: Path) -> VideoFacts:
    """Compute stream facts and the keyframe signature for one video.

    Usage:
        >>> facts = video_signature(Path("clip.mp4"))  # doctest: +SKIP
        >>> facts.video_sig  # doctest: +SKIP
        '12s:1920x1080:ab12...:cd34...:ef56...'
    """
    if not ffmpeg_available():
        return VideoFacts(None, None, None, None, flag="no_ffprobe")
    probed = _ffprobe(path)
    if probed is None:
        return VideoFacts(None, None, None, None, flag="video_unreadable")
    duration_s, width, height = probed
    hashes = [
        _keyframe_phash(path, duration_s * fraction) or "?"
        for fraction in _KEYFRAME_POSITIONS
    ]
    sig = f"{round(duration_s)}s:{width}x{height}:{':'.join(hashes)}"
    return VideoFacts(duration_s, width, height, sig, flag=None)
