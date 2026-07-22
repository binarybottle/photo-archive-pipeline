"""Tests for Takeout zip staging: extraction, idempotency, crash recovery."""

import shutil
import zipfile
from pathlib import Path

import pytest

import archive_pipeline.space as space_mod
from archive_pipeline.space import SpaceError
from archive_pipeline.staging import StagingError, complete_marker, stage_takeout_zip


@pytest.fixture
def takeout_zip(tmp_path: Path) -> Path:
    zip_path = tmp_path / "takeout-20260722.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Takeout/Google Photos/Photos from 2015/IMG_1.jpg", b"jpegbytes")
        zf.writestr("Takeout/Google Photos/Photos from 2015/IMG_1.jpg.json", "{}")
    return zip_path


def test_extracts_and_marks_complete(tmp_path: Path, takeout_zip: Path) -> None:
    staging = tmp_path / "staging"
    dest = stage_takeout_zip(takeout_zip, staging, "t1", 15.0)
    assert dest == staging / "t1"
    assert (dest / "Takeout/Google Photos/Photos from 2015/IMG_1.jpg").read_bytes() == b"jpegbytes"
    assert complete_marker(staging, "t1").exists()
    assert not (dest / ".extraction-complete").exists()


def test_second_call_is_a_noop(tmp_path: Path, takeout_zip: Path) -> None:
    staging = tmp_path / "staging"
    dest = stage_takeout_zip(takeout_zip, staging, "t1", 15.0)
    sentinel = dest / "Takeout/sentinel"
    sentinel.write_text("kept", encoding="utf-8")
    assert stage_takeout_zip(takeout_zip, staging, "t1", 15.0) == dest
    assert sentinel.read_text(encoding="utf-8") == "kept"


def test_leftover_partial_is_discarded_and_redone(tmp_path: Path, takeout_zip: Path) -> None:
    staging = tmp_path / "staging"
    partial = staging / "t1.partial"
    partial.mkdir(parents=True)
    (partial / "halfway.jpg").write_bytes(b"junk")
    dest = stage_takeout_zip(takeout_zip, staging, "t1", 15.0)
    assert not partial.exists()
    assert not (dest / "halfway.jpg").exists()
    assert complete_marker(staging, "t1").exists()


def test_unmarked_existing_dir_is_an_error(tmp_path: Path, takeout_zip: Path) -> None:
    staging = tmp_path / "staging"
    (staging / "t1").mkdir(parents=True)
    with pytest.raises(StagingError, match="completion marker"):
        stage_takeout_zip(takeout_zip, staging, "t1", 15.0)


def test_not_a_zip_is_an_error(tmp_path: Path) -> None:
    bogus = tmp_path / "not.zip"
    bogus.write_text("hello", encoding="utf-8")
    with pytest.raises(StagingError, match="not a readable zip"):
        stage_takeout_zip(bogus, tmp_path / "staging", "t1", 15.0)


def test_space_preflight_blocks_extraction(
    tmp_path: Path, takeout_zip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(space_mod.shutil, "disk_usage", lambda _: usage._replace(free=1))
    staging = tmp_path / "staging"
    with pytest.raises(SpaceError):
        stage_takeout_zip(takeout_zip, staging, "t1", 15.0)
    assert not (staging / "t1").exists()
    assert not (staging / "t1.partial").exists()
