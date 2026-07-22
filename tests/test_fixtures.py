"""Tests for the fixture generator v0: determinism, pathologies, safety."""

import json
import shutil
from pathlib import Path

import pytest

from archive_pipeline.fixtures.generator import MANIFEST_NAME, generate_corpus

HAS_EXIFTOOL = shutil.which("exiftool") is not None


def test_deterministic_across_runs(tmp_path: Path) -> None:
    """Same seed -> byte-identical corpus (spec section 9: fixtures are seeded)."""
    m1 = generate_corpus(tmp_path / "a", seed=42)
    m2 = generate_corpus(tmp_path / "b", seed=42)
    assert m1.files == m2.files


def test_different_seeds_differ(tmp_path: Path) -> None:
    m1 = generate_corpus(tmp_path / "a", seed=1)
    m2 = generate_corpus(tmp_path / "b", seed=2)
    assert m1.files.keys() == m2.files.keys()
    assert m1.files != m2.files


def test_expected_pathologies_present(tmp_path: Path) -> None:
    dest = tmp_path / "corpus"
    manifest = generate_corpus(dest, seed=0)
    files = manifest.files
    photos = "TAKEOUT/Google Photos/Photos from 2015"

    # Exact duplicates share a hash across folder contexts.
    assert files["LOCAL/1998/beach_001.jpg"] == files["LOCAL/topical/vacations/beach_001.jpg"]
    assert (
        files[f"{photos}/IMG_2015_001.jpg"]
        == files["TAKEOUT/Google Photos/Vacation 2015/IMG_2015_001.jpg"]
    )
    # Sidecar pathologies: truncation, (n) numbering, -edited pair, album metadata.
    long_sidecars = [
        f for f in files if f.startswith(f"{photos}/a_very_long") and f.endswith(".json")
    ]
    assert len(long_sidecars) == 1
    assert len(Path(long_sidecars[0]).name) == 46 + len(".json")
    assert f"{photos}/IMG_2015_002.jpg(1).json" in files
    assert f"{photos}/IMG_2015_002(1).jpg" in files
    assert f"{photos}/IMG_2015_003-edited.jpg" in files
    assert "TAKEOUT/Google Photos/Vacation 2015/metadata.json" in files
    # Degenerate files: zero-byte, truncated, non-media.
    assert (dest / "LOCAL/misc/empty.jpg").stat().st_size == 0
    broken = (dest / "LOCAL/misc/broken.jpg").stat().st_size
    assert 0 < broken < (dest / "LOCAL/1998/beach_002.jpg").stat().st_size
    assert "LOCAL/misc/notes.txt" in files
    # Scanner batch present.
    assert {f"LOCAL/scans/scan00{n}.png" for n in (1, 2, 3)} <= files.keys()


def test_manifest_written_and_consistent(tmp_path: Path) -> None:
    dest = tmp_path / "corpus"
    manifest = generate_corpus(dest, seed=7)
    on_disk = json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert on_disk["seed"] == 7
    assert on_disk["files"] == manifest.files
    assert MANIFEST_NAME not in manifest.files


def test_refuses_nonempty_destination(tmp_path: Path) -> None:
    dest = tmp_path / "corpus"
    dest.mkdir()
    (dest / "precious.txt").write_text("do not clobber", encoding="utf-8")
    with pytest.raises(FileExistsError):
        generate_corpus(dest, seed=0)
    assert (dest / "precious.txt").read_text(encoding="utf-8") == "do not clobber"


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool not installed")
def test_exif_embedded_when_exiftool_available(tmp_path: Path) -> None:
    import subprocess

    dest = tmp_path / "corpus"
    manifest = generate_corpus(dest, seed=0)
    assert manifest.exif_written
    out = subprocess.run(
        ["exiftool", "-s", "-s", "-s", "-DateTimeOriginal", str(dest / "LOCAL/1998/beach_001.jpg")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "1998:07:12 14:33:05"
