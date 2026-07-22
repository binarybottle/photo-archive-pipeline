"""Golden ingest tests over the fixture corpus, plus resumability and INV-1."""

import json
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest

from archive_pipeline.catalog import open_catalog
from archive_pipeline.config import load_config
from archive_pipeline.fixtures.generator import generate_corpus
from archive_pipeline.ingest import IngestError, IngestSummary, ingest_source, walk_source
from archive_pipeline.runs import record_run
from archive_pipeline.workingtree import init_working_tree

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("corpus") / "v0"
    generate_corpus(dest, seed=0)
    return dest


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    """An initialized working tree (gate open, sequential workers) + catalog."""
    wt, _ = init_working_tree(tmp_path / "worktree")
    config = wt.config_path
    text = config.read_text(encoding="utf-8")
    text = text.replace("confirmed = false", "confirmed = true")
    text = text.replace("parallelism = 0", "parallelism = 1")
    config.write_text(text, encoding="utf-8")
    return wt.root, open_catalog(wt.catalog_path)


def _ingest(
    wt_root: Path, conn: sqlite3.Connection, source_id: str, root: Path
) -> IngestSummary:
    cfg = load_config(wt_root / "config.toml")
    log = logging.getLogger("archive_pipeline.test")
    with record_run(conn, "ingest", {"source": source_id, "root": str(root)}) as run_id:
        return ingest_source(conn, cfg, source_id, root, run_id, log)


def _row(conn: sqlite3.Connection, source: str, rel: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM instance WHERE source = ? AND rel_path = ?", (source, rel)
    ).fetchone()
    assert row is not None, f"missing instance {source}:{rel}"
    return row


def test_golden_local_ingest(env: tuple[Path, sqlite3.Connection], corpus: Path) -> None:
    wt_root, conn = env
    summary = _ingest(wt_root, conn, "LOCAL", corpus / "LOCAL")

    # Acceptance: instance count equals filesystem count for the source.
    assert summary.discovered == 13
    assert summary.catalog_count == 13
    assert summary.processed == 13 and summary.skipped_unchanged == 0
    assert summary.by_kind == {"image": 12, "other": 1}
    assert summary.corrupt == 2  # empty.jpg + broken.jpg
    assert summary.sample_checked == 13

    beach = _row(conn, "LOCAL", "1998/beach_001.jpg")
    assert beach["mime"] == "image/jpeg"
    assert beach["exif_dto"] == "1998-07-12T14:33:05"
    assert beach["camera_make"] == "Canon"
    assert beach["camera_model"] == "PowerShot A5"
    assert beach["width"] == 96 and beach["height"] == 72
    assert beach["phash"] and beach["dhash"]
    assert beach["exif_tag_count"] > 0

    # Exact duplicate in a topical folder: identical bytes and perceptual hashes.
    dup = _row(conn, "LOCAL", "topical/vacations/beach_001.jpg")
    assert dup["sha256"] == beach["sha256"]
    assert dup["phash"] == beach["phash"]

    # Scanner batch: PNGs whose only date lives in XMP CreateDate.
    scan = _row(conn, "LOCAL", "scans/scan001.png")
    assert scan["mime"] == "image/png"
    assert scan["exif_dto"] is None
    assert "XMP:CreateDate" in scan["exif_json"]

    # Degenerate files still obey the conservation law: hashed, flagged, kept.
    empty = _row(conn, "LOCAL", "misc/empty.jpg")
    assert empty["sha256"] == EMPTY_SHA
    assert empty["phash"] is None
    empty_flags = json.loads(empty["flags"])
    assert "zero_byte" in empty_flags and "corrupt" in empty_flags

    broken = _row(conn, "LOCAL", "misc/broken.jpg")
    assert broken["mime"] == "image/jpeg"  # header survives truncation
    assert broken["phash"] is None
    assert "corrupt" in json.loads(broken["flags"])

    notes = _row(conn, "LOCAL", "misc/notes.txt")
    assert notes["kind"] == "other"


def test_golden_takeout_ingest(env: tuple[Path, sqlite3.Connection], corpus: Path) -> None:
    wt_root, conn = env
    summary = _ingest(wt_root, conn, "TAKEOUT:t2015", corpus / "TAKEOUT")
    assert summary.discovered == 13
    assert summary.by_kind == {"image": 7, "sidecar_json": 6}

    img = _row(conn, "TAKEOUT:t2015", "Google Photos/Photos from 2015/IMG_2015_001.jpg")
    album = _row(conn, "TAKEOUT:t2015", "Google Photos/Vacation 2015/IMG_2015_001.jpg")
    assert img["sha256"] == album["sha256"]
    assert img["exif_dto"] == "2015-04-18T09:30:00"

    sidecar = _row(
        conn,
        "TAKEOUT:t2015",
        "Google Photos/Photos from 2015/IMG_2015_001.jpg.supplemental-metadata.json",
    )
    assert sidecar["kind"] == "sidecar_json"
    assert sidecar["phash"] is None


def test_reingest_is_a_noop(env: tuple[Path, sqlite3.Connection], corpus: Path) -> None:
    wt_root, conn = env
    _ingest(wt_root, conn, "LOCAL", corpus / "LOCAL")
    ids_before = {
        row["rel_path"]: row["id"] for row in conn.execute("SELECT id, rel_path FROM instance")
    }
    summary = _ingest(wt_root, conn, "LOCAL", corpus / "LOCAL")
    assert summary.processed == 0
    assert summary.skipped_unchanged == 13
    ids_after = {
        row["rel_path"]: row["id"] for row in conn.execute("SELECT id, rel_path FROM instance")
    }
    assert ids_after == ids_before


def test_changed_file_is_reprocessed_in_place(
    env: tuple[Path, sqlite3.Connection], tmp_path: Path, corpus: Path
) -> None:
    wt_root, conn = env
    local = tmp_path / "LOCAL"
    shutil.copytree(corpus / "LOCAL", local)
    _ingest(wt_root, conn, "LOCAL", local)
    before = _row(conn, "LOCAL", "misc/notes.txt")

    (local / "misc/notes.txt").write_text("rewritten contents\n", encoding="utf-8")
    summary = _ingest(wt_root, conn, "LOCAL", local)
    assert summary.processed == 1
    after = _row(conn, "LOCAL", "misc/notes.txt")
    assert after["id"] == before["id"]
    assert after["sha256"] != before["sha256"]


def test_sources_untouched_by_ingest(
    env: tuple[Path, sqlite3.Connection], corpus: Path
) -> None:
    """INV-1: ingest changes nothing under the source root."""
    wt_root, conn = env
    before = {
        e.rel_path: (e.size_bytes, e.mtime_ns) for e in walk_source(corpus / "LOCAL")
    }
    _ingest(wt_root, conn, "LOCAL", corpus / "LOCAL")
    after = {
        e.rel_path: (e.size_bytes, e.mtime_ns) for e in walk_source(corpus / "LOCAL")
    }
    assert after == before


def test_refuses_to_run_as_root(
    env: tuple[Path, sqlite3.Connection], corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_root, conn = env
    monkeypatch.setattr("os.geteuid", lambda: 0)
    with pytest.raises(IngestError, match="INV-1"):
        _ingest(wt_root, conn, "LOCAL", corpus / "LOCAL")
    failed = conn.execute("SELECT status FROM run ORDER BY id DESC LIMIT 1").fetchone()
    assert failed["status"] == "failed"


def test_symlinks_are_skipped(
    env: tuple[Path, sqlite3.Connection], tmp_path: Path, corpus: Path
) -> None:
    wt_root, conn = env
    local = tmp_path / "LOCAL"
    shutil.copytree(corpus / "LOCAL", local)
    (local / "link.jpg").symlink_to(local / "1998/beach_001.jpg")
    summary = _ingest(wt_root, conn, "LOCAL", local)
    assert summary.discovered == 13
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM instance WHERE rel_path = 'link.jpg'"
        ).fetchone()[0]
        == 0
    )
