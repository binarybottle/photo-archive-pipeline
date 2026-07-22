"""Stage 2b + Stage 3 integration over the fixture corpus.

The LOCAL tree is the v0 corpus plus a merged-in prior Takeout extraction
(``google-import/Google Photos/...``), mirroring the user's real situation.
"""

import json
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest

from archive_pipeline.catalog import open_catalog
from archive_pipeline.config import load_config
from archive_pipeline.dates import DateResolveError, resolve_dates
from archive_pipeline.fixtures.generator import generate_corpus
from archive_pipeline.ingest import ingest_source
from archive_pipeline.provenance import classify_local
from archive_pipeline.runs import record_run
from archive_pipeline.takeout import normalize_takeout
from archive_pipeline.workingtree import init_working_tree

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

LOG = logging.getLogger("archive_pipeline.test")


class Env:
    """Fully ingested working tree over the merged corpus."""

    def __init__(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        generate_corpus(corpus, seed=0)
        self.local_root = tmp_path / "LOCAL"
        shutil.copytree(corpus / "LOCAL", self.local_root)
        shutil.copytree(
            corpus / "TAKEOUT" / "Google Photos",
            self.local_root / "google-import" / "Google Photos",
        )
        self.wt, _ = init_working_tree(tmp_path / "worktree")
        text = self.wt.config_path.read_text(encoding="utf-8")
        text = text.replace("confirmed = false", "confirmed = true")
        text = text.replace("parallelism = 0", "parallelism = 1")
        self.wt.config_path.write_text(text, encoding="utf-8")
        self.conn = open_catalog(self.wt.catalog_path)
        self.cfg = load_config(self.wt.config_path)
        with record_run(self.conn, "ingest") as run_id:
            ingest_source(self.conn, self.cfg, "LOCAL", self.local_root, run_id, LOG)
        with record_run(self.conn, "ingest") as run_id:
            ingest_source(
                self.conn, self.cfg, "TAKEOUT:t2015", corpus / "TAKEOUT", run_id, LOG
            )
        normalize_takeout(self.conn, self.wt, LOG)

    def reload_config(self) -> None:
        self.cfg = load_config(self.wt.config_path)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _classification(conn: sqlite3.Connection, dir_path: str) -> str:
    row = conn.execute(
        "SELECT classification FROM local_provenance WHERE source = 'LOCAL'"
        " AND dir_path = ?",
        (dir_path,),
    ).fetchone()
    assert row is not None, f"unclassified dir: {dir_path}"
    return row["classification"]


def test_provenance_classifies_every_dir(env: Env) -> None:
    summary = classify_local(env.conn, env.cfg, env.wt, LOG)
    assert summary.changed
    # Hand-curated dirs stay curated; the merged Takeout subtree is derived.
    assert _classification(env.conn, "1998") == "curated"
    assert _classification(env.conn, "scans") == "curated"
    assert (
        _classification(env.conn, "google-import/Google Photos/Photos from 2015")
        == "takeout_derived"
    )
    assert (
        _classification(env.conn, "google-import/Google Photos/Vacation 2015")
        == "takeout_derived"
    )
    report = (env.wt.reports_dir / "local_provenance.csv").read_text(encoding="utf-8")
    assert "google-import/Google Photos/Photos from 2015" in report

    trust = {
        row["rel_path"]: row["effective_trust"]
        for row in env.conn.execute(
            "SELECT rel_path, effective_trust FROM instance WHERE source = 'LOCAL'"
            " AND kind IN ('image', 'video')"
        )
    }
    assert trust["1998/beach_001.jpg"] == "curated"
    assert trust["google-import/Google Photos/Photos from 2015/IMG_2015_001.jpg"] == "takeout"
    takeout_trust = env.conn.execute(
        "SELECT DISTINCT effective_trust FROM instance WHERE source = 'TAKEOUT:t2015'"
    ).fetchall()
    assert [row[0] for row in takeout_trust] == ["takeout"]

    # Sidecars inside the derived subtree are parsed like Stage 2.
    linked = env.conn.execute(
        "SELECT s.photo_taken_time FROM takeout_sidecar s"
        " JOIN instance m ON m.id = s.media_instance_id"
        " WHERE m.source = 'LOCAL' AND m.rel_path LIKE 'google-import%IMG_2015_001.jpg'"
    ).fetchone()
    assert linked is not None
    assert linked["photo_taken_time"] == "2015-04-18T09:30:00+00:00"


def test_provenance_overrides_honored(env: Env) -> None:
    text = env.wt.config_path.read_text(encoding="utf-8")
    text = text.replace(
        "curated_overrides = []",
        'curated_overrides = ["google-import/Google Photos/Vacation 2015"]',
    )
    text = text.replace(
        "takeout_derived_overrides = []", 'takeout_derived_overrides = ["topical"]'
    )
    env.wt.config_path.write_text(text, encoding="utf-8")
    env.reload_config()
    summary = classify_local(env.conn, env.cfg, env.wt, LOG)
    assert summary.overridden == 2
    assert _classification(env.conn, "google-import/Google Photos/Vacation 2015") == "curated"
    assert _classification(env.conn, "topical/vacations") == "takeout_derived"


def test_provenance_is_idempotent(env: Env) -> None:
    first = classify_local(env.conn, env.cfg, env.wt, LOG)
    decisions = env.conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0]
    second = classify_local(env.conn, env.cfg, env.wt, LOG)
    assert first.changed and not second.changed
    assert env.conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == decisions


def test_date_resolve_requires_provenance(env: Env) -> None:
    with pytest.raises(DateResolveError, match="local-provenance"):
        resolve_dates(env.conn, env.cfg, env.wt, LOG)


def _resolution(conn: sqlite3.Connection, source: str, rel_suffix: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT d.* FROM date_resolution d JOIN instance i ON i.id = d.instance_id"
        " WHERE i.source = ? AND i.rel_path LIKE ?",
        (source, f"%{rel_suffix}"),
    ).fetchone()
    assert row is not None, f"no resolution for {rel_suffix}"
    return row


def test_golden_date_resolution(env: Env) -> None:
    classify_local(env.conn, env.cfg, env.wt, LOG)
    summary = resolve_dates(env.conn, env.cfg, env.wt, LOG)

    assert summary.by_status.get("pending", 0) == 0
    assert summary.total == summary.by_status.get("auto", 0) + summary.by_status.get(
        "conflict", 0
    )

    # R1: trusted EXIF bracketed by the year folder.
    beach = _resolution(env.conn, "LOCAL", "1998/beach_001.jpg")
    assert beach["resolved_date"] == "1998-07-12T14:33:05"
    assert beach["resolved_source"] == "exif"
    assert beach["status"] == "auto"

    # R3: no EXIF at all -> curated folder date at year precision.
    beach3 = _resolution(env.conn, "LOCAL", "1998/beach_003.jpg")
    assert beach3["resolved_date"] == "1998-01-01"
    assert (beach3["resolved_source"], beach3["resolved_precision"]) == ("folder", "year")

    # Epoch-default EXIF is distrusted; the curated month folder wins (R3).
    park = _resolution(env.conn, "LOCAL", "2003-07/park_001.jpg")
    assert "epoch_default" in json.loads(park["exif_flags"])
    assert park["resolved_date"] == "2003-07-01"
    assert park["resolved_source"] == "folder"

    # Scanner batch: CreateDate-only PNGs, no folder date -> conflict (R7).
    scan = _resolution(env.conn, "LOCAL", "scans/scan001.png")
    assert "scanner_createdate" in json.loads(scan["exif_flags"])
    assert scan["status"] == "conflict"

    # TAKEOUT: trusted EXIF wins (R2); "Photos from 2015" folder is not curated.
    img1 = _resolution(env.conn, "TAKEOUT:t2015", "Photos from 2015/IMG_2015_001.jpg")
    assert img1["resolved_date"] == "2015-04-18T09:30:00"
    assert img1["resolved_source"] == "exif"

    # R4: no EXIF -> sidecar photoTakenTime.
    img2 = _resolution(env.conn, "TAKEOUT:t2015", "Photos from 2015/IMG_2015_002.jpg")
    assert img2["resolved_source"] == "takeout_json"
    assert img2["resolved_date"].startswith("2015-04-18T11:30:00")

    # The -edited file inherits the original's sidecar through edited_pair (R4).
    edited = _resolution(env.conn, "TAKEOUT:t2015", "IMG_2015_003-edited.jpg")
    assert edited["resolved_source"] == "takeout_json"

    # Merged prior-Takeout subtree in LOCAL: folder date is takeout-trust, so
    # the sidecar wins (R4), not the "Photos from 2015" folder.
    merged = _resolution(env.conn, "LOCAL", "google-import%IMG_2015_002.jpg")
    assert merged["resolved_source"] == "takeout_json"

    # Nothing usable on the zero-byte file -> R7 conflict.
    empty = _resolution(env.conn, "LOCAL", "misc/empty.jpg")
    assert empty["status"] == "conflict"

    audit = (env.wt.reports_dir / "date_audit_sample.csv").read_text(encoding="utf-8")
    assert "resolved_date" in audit and "beach_001.jpg" in audit


def test_date_resolution_idempotent_and_preserves_review(env: Env) -> None:
    classify_local(env.conn, env.cfg, env.wt, LOG)
    first = resolve_dates(env.conn, env.cfg, env.wt, LOG)
    second = resolve_dates(env.conn, env.cfg, env.wt, LOG)
    assert first.changed and not second.changed

    # A reviewed row survives re-resolution untouched (INV-6).
    scan_id = env.conn.execute(
        "SELECT id FROM instance WHERE rel_path = 'scans/scan001.png'"
    ).fetchone()["id"]
    with env.conn:
        env.conn.execute(
            "UPDATE date_resolution SET status = 'reviewed',"
            " resolved_date = '2001-07-01', resolved_precision = 'month',"
            " resolved_source = 'review' WHERE instance_id = ?",
            (scan_id,),
        )
    third = resolve_dates(env.conn, env.cfg, env.wt, LOG)
    assert third.reviewed_preserved == 1
    row = env.conn.execute(
        "SELECT resolved_date, status FROM date_resolution WHERE instance_id = ?",
        (scan_id,),
    ).fetchone()
    assert (row["resolved_date"], row["status"]) == ("2001-07-01", "reviewed")


def test_mass_identical_flag(env: Env, tmp_path: Path) -> None:
    text = env.wt.config_path.read_text(encoding="utf-8")
    text = text.replace("mass_identical_n = 25", "mass_identical_n = 2")
    env.wt.config_path.write_text(text, encoding="utf-8")
    env.reload_config()
    classify_local(env.conn, env.cfg, env.wt, LOG)
    resolve_dates(env.conn, env.cfg, env.wt, LOG)
    # The three scans share one CreateDate; with N=2 they are mass-identical.
    scan = _resolution(env.conn, "LOCAL", "scans/scan002.png")
    assert "mass_identical" in json.loads(scan["exif_flags"])


def test_camera_era_flag(env: Env) -> None:
    text = env.wt.config_path.read_text(encoding="utf-8")
    text = text.replace(
        "[dates.camera_era]", '[dates.camera_era]\n"PowerShot A5" = 1999'
    )
    env.wt.config_path.write_text(text, encoding="utf-8")
    env.reload_config()
    classify_local(env.conn, env.cfg, env.wt, LOG)
    resolve_dates(env.conn, env.cfg, env.wt, LOG)
    # 1998 EXIF from a camera whose era starts 1999 -> distrusted -> folder (R3).
    beach = _resolution(env.conn, "LOCAL", "1998/beach_001.jpg")
    assert "predates_camera" in json.loads(beach["exif_flags"])
    assert beach["resolved_source"] == "folder"
