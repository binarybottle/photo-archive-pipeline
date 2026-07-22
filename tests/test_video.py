"""Tests for video signatures. Signature tests need ffmpeg/ffprobe on PATH."""

import subprocess
from pathlib import Path

import pytest

import archive_pipeline.video as video_mod
from archive_pipeline.video import VideoFacts, ffmpeg_available, video_signature


def test_missing_ffmpeg_yields_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video_mod, "ffmpeg_available", lambda: False)
    facts = video_signature(tmp_path / "clip.mp4")
    assert facts == VideoFacts(None, None, None, None, flag="no_ffprobe")


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not installed")
def test_signature_of_generated_video(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", "testsrc=duration=2:size=160x120:rate=10",
            "-pix_fmt", "yuv420p", str(clip),
        ],
        check=True,
        capture_output=True,
    )
    facts = video_signature(clip)
    assert facts.flag is None
    assert facts.width == 160 and facts.height == 120
    assert facts.duration_s is not None and abs(facts.duration_s - 2.0) < 0.2
    assert facts.video_sig is not None and facts.video_sig.startswith("2s:160x120:")
    assert "?" not in facts.video_sig  # all three keyframes hashed


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not installed")
def test_unreadable_video_is_flagged(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00" * 64)
    facts = video_signature(junk)
    assert facts.flag == "video_unreadable"
    assert facts.video_sig is None
