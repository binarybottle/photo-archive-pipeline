"""Materialize tests: naming/date/keyword units + full dry-run/execute cycle."""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import PipelineEnv

from archive_pipeline.dates import resolve_dates
from archive_pipeline.dedup import run_dedup
from archive_pipeline.materialize import (
    MaterializeError,
    dest_rel_path,
    exiftool_config_path,
    exiftool_date,
    folder_keyword_candidates,
    load_keyword_map,
    quarantine_rel_path,
    run_materialize,
    write_keyword_map,
)
from archive_pipeline.provenance import classify_local
from archive_pipeline.review.actions import batch_apply

LOG = logging.getLogger("archive_pipeline.test")

# --- Units ---------------------------------------------------------------------


def test_dest_rel_path_layout() -> None:
    sha = "ab12cd34" * 8
    assert dest_rel_path("2015-04-18T09:30:00", "second", "undated", "x/IMG.jpg", sha) \
        == "2015/2015-04/IMG__ab12cd34.jpg"
    assert dest_rel_path("2010-06-15", "day", "undated", "x/w.JPG", sha) \
        == "2010/2010-06/w__ab12cd34.jpg"
    assert dest_rel_path("1998-01-01", "year", "undated", "x/b.png", sha) \
        == "1998/b__ab12cd34.png"
    assert dest_rel_path(None, None, "undated", "x/b.png", sha) \
        == "undated/b__ab12cd34.png"


def test_quarantine_rel_path() -> None:
    sha = "ef" * 32
    assert quarantine_rel_path(sha, "a/b/photo.jpg") == f"ef/{sha}__photo.jpg"


def test_exiftool_date_padding() -> None:
    assert exiftool_date("1998-07-12T14:33:05", "second") == "1998:07:12 14:33:05"
    assert exiftool_date("2015-04-18T09:30:00+00:00", "second") == "2015:04:18 09:30:00"
    assert exiftool_date("2003-07-01", "month") == "2003:07:01 00:00:00"


def test_keyword_map_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "keyword_map.csv"
    write_keyword_map(path, {"vacations", "scans"})
    mapping = load_keyword_map(path)
    assert mapping == {"vacations": "vacations", "scans": "scans"}
    path.write_text(
        "folder_name,proposed_keyword,action\n"
        "vacations,Travel/Vacations,rename\n"
        "scans,,drop\n"
        "topical,topical,keep\n",
        encoding="utf-8",
    )
    mapping = load_keyword_map(path)
    assert mapping == {"vacations": "Travel/Vacations", "scans": None,
                       "topical": "topical"}
    path.write_text("folder_name,proposed_keyword,action\nx,y,explode\n", encoding="utf-8")
    with pytest.raises(MaterializeError, match="unknown action"):
        load_keyword_map(path)


def test_folder_keyword_candidates_filters() -> None:
    patterns = (r"^(?P<year>(19|20)\d{2})$",)
    assert folder_keyword_candidates("1998/x.jpg", patterns) == []
    assert folder_keyword_candidates(
        "Takeout/Google Photos/Photos from 2015/x.jpg", patterns
    ) == []
    assert folder_keyword_candidates("topical/vacations/x.jpg", patterns) == [
        "topical", "vacations",
    ]


# --- Integration ----------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> PipelineEnv:
    """Full pipeline through dedup, scans batch-resolved, materialized."""
    e = PipelineEnv(tmp_path_factory.mktemp("m7"))
    classify_local(e.conn, e.cfg, e.wt, LOG)
    resolve_dates(e.conn, e.cfg, e.wt, LOG)
    batch_apply(e.conn, "LOCAL", "scans", "exif")  # scanner batch -> reviewed
    run_dedup(e.conn, e.cfg, e.wt, LOG)
    return e


def _snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_dry_run_writes_nothing_and_generates_keyword_map(env: PipelineEnv) -> None:
    summary = run_materialize(env.conn, env.cfg, env.wt, LOG, execute=False)
    assert not summary.executed
    assert summary.keyword_map_created
    assert _snapshot(env.wt.archive_dir) == set()
    assert _snapshot(env.wt.quarantine_dir) == set()
    manifest = (env.wt.reports_dir / "archive_manifest.csv").read_text("utf-8")
    assert "1998/1998-07/beach_001__" in manifest  # exif-second date -> YYYY/YYYY-MM
    keyword_map = (env.wt.reports_dir / "keyword_map.csv").read_text("utf-8")
    assert "vacations" in keyword_map and "Vacation 2015" in keyword_map
    # Placements are only recorded by --execute.
    assert env.conn.execute("SELECT COUNT(*) FROM placement").fetchone()[0] == 0


def test_execute_materializes_archive_and_quarantine(env: PipelineEnv) -> None:
    # User edits the keyword map: rename one, drop another.
    map_path = env.wt.reports_dir / "keyword_map.csv"
    text = map_path.read_text(encoding="utf-8")
    text = text.replace("vacations,vacations,keep", "vacations,Travel/Vacations,rename")
    text = text.replace("google-import,google-import,keep", "google-import,,drop")
    map_path.write_text(text, encoding="utf-8")

    summary = run_materialize(env.conn, env.cfg, env.wt, LOG, execute=True)
    assert summary.executed
    assert summary.archived > 0 and summary.quarantined > 0
    assert summary.sample_checked >= 5

    # Every instance is placed exactly once (conservation precondition).
    total = env.conn.execute("SELECT COUNT(*) FROM instance").fetchone()[0]
    placed = env.conn.execute("SELECT COUNT(*) FROM placement").fetchone()[0]
    assert placed == total

    archived = _snapshot(env.wt.archive_dir)
    beach = next(p for p in archived if "beach_001__" in p)
    assert beach.startswith("1998/1998-07/")

    # Metadata written through exiftool, including the ArchivePipe namespace.
    out = subprocess.run(
        ["exiftool", "-config", str(exiftool_config_path()), "-j", "-G",
         str(env.wt.archive_dir / beach)],
        capture_output=True, text=True, check=True,
    )
    tags = json.loads(out.stdout)[0]
    assert tags["EXIF:DateTimeOriginal"] == "1998:07:12 14:33:05"
    assert tags["XMP:DateSource"] == "exif"  # both members resolved via R1/R2
    assert tags["XMP:DatePrecision"] == "second"
    assert tags["XMP:SourcePath"] == "LOCAL:1998/beach_001.jpg"
    assert tags["XMP:PipelineVersion"] == "0.1.0"
    subjects = tags.get("XMP:Subject")
    subjects = subjects if isinstance(subjects, list) else [subjects]
    assert "Travel/Vacations" in subjects and "topical" in subjects
    merged_from = tags["XMP:MergedFrom"]
    assert any("topical/vacations" in m for m in merged_from)

    # Takeout-described winner carries description + GPS from the sidecar.
    img1 = next(p for p in archived if "IMG_2015_001__" in p)
    out = subprocess.run(
        ["exiftool", "-n", "-j", "-G", str(env.wt.archive_dir / img1)],
        capture_output=True, text=True, check=True,
    )
    tags = json.loads(out.stdout)[0]
    assert tags["XMP:Description"] == "Lake hike"
    assert round(tags["EXIF:GPSLatitude"], 2) == 44.06
    assert round(tags["EXIF:GPSLongitude"], 2) == 71.29
    assert tags["EXIF:GPSLongitudeRef"] == "W"

    # Edited pair policy: edited winner rated 4 + keyword; original has-edit.
    edited = next(p for p in archived if "IMG_2015_003-edited__" in p)
    out = subprocess.run(
        ["exiftool", "-j", "-G", str(env.wt.archive_dir / edited)],
        capture_output=True, text=True, check=True,
    )
    tags = json.loads(out.stdout)[0]
    assert tags["XMP:Rating"] == 4
    subjects = tags["XMP:Subject"]
    subjects = subjects if isinstance(subjects, list) else [subjects]
    assert "edited-preferred" in subjects

    # Quarantine: byte-identical, content-addressed, one copy per hash.
    quarantined = _snapshot(env.wt.quarantine_dir)
    losers = [p for p in quarantined if "IMG_2015_001" in p]
    assert len(losers) == 1  # 3 identical losers -> one physical copy
    index_lines = [
        json.loads(line)
        for line in (env.wt.quarantine_dir / "index.jsonl").read_text("utf-8").splitlines()
    ]
    img1_entry = next(e for e in index_lines if "IMG_2015_001" in e["quarantine_path"])
    assert len(img1_entry["sources"]) == 3
    assert img1_entry["winner_dest"] is not None

    # Corrupt files are quarantined, never archived (edge case 9).
    assert any("empty.jpg" in p for p in quarantined)
    assert any("broken.jpg" in p for p in quarantined)
    assert not any("empty" in p for p in archived)

    # Excluded: sidecar JSONs and non-media, with reasons.
    excluded = (env.wt.reports_dir / "excluded_manifest.csv").read_text("utf-8")
    assert "supplemental-metadata.json" in excluded and "takeout_sidecar" in excluded
    assert "notes.txt" in excluded and "non_media" in excluded

    # Scanner batch (reviewed to its CreateDate) landed dated.
    assert any(p.startswith("2019/2019-11/scan001__") for p in archived)


def test_execute_rerun_skips_everything(env: PipelineEnv) -> None:
    before = _snapshot(env.wt.archive_dir)
    summary = run_materialize(env.conn, env.cfg, env.wt, LOG, execute=True)
    assert summary.skipped_done == env.conn.execute(
        "SELECT COUNT(*) FROM placement"
    ).fetchone()[0]
    assert _snapshot(env.wt.archive_dir) == before


def test_sources_untouched_by_materialize(env: PipelineEnv) -> None:
    """INV-1/INV-2: metadata went to copies; the source tree is unchanged."""
    from archive_pipeline.ingest import walk_source

    entries = walk_source(env.local_root)
    for entry in entries:
        if "beach_001" in entry.rel_path:
            out = subprocess.run(
                ["exiftool", "-j", str(entry.abs_path)],
                capture_output=True, text=True, check=True,
            )
            tags = json.loads(out.stdout)[0]
            assert "Subject" not in json.dumps(tags)  # no keywords leaked back


def test_execute_refuses_pending_clusters(tmp_path: Path) -> None:
    e = PipelineEnv(tmp_path)
    classify_local(e.conn, e.cfg, e.wt, LOG)
    resolve_dates(e.conn, e.cfg, e.wt, LOG)
    run_dedup(e.conn, e.cfg, e.wt, LOG)
    with e.conn:
        e.conn.execute("UPDATE cluster SET status = 'pending' WHERE id ="
                       " (SELECT id FROM cluster LIMIT 1)")
    with pytest.raises(MaterializeError, match="await review"):
        run_materialize(e.conn, e.cfg, e.wt, LOG, execute=False)
