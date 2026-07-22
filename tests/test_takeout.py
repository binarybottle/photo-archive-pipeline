"""Stage 2 tests: one per sidecar pathology, plus integration over the corpus."""

import json
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest

from archive_pipeline.catalog import open_catalog
from archive_pipeline.config import load_config
from archive_pipeline.fixtures.generator import generate_corpus
from archive_pipeline.ingest import ingest_source
from archive_pipeline.runs import record_run
from archive_pipeline.takeout import (
    NameParts,
    TakeoutError,
    edited_original,
    is_google_recompressed,
    match_directory,
    normalize_takeout,
    parse_sidecar_name,
    parse_sidecar_text,
)
from archive_pipeline.workingtree import WorkingTree, init_working_tree

# --- Pathology 1a: plain and supplemental-metadata exact names -----------------


def test_parse_plain_and_supplemental_names() -> None:
    assert parse_sidecar_name("IMG_1.JPG.json") == NameParts("IMG_1.JPG", None)
    assert parse_sidecar_name("IMG_1.JPG.supplemental-metadata.json") == NameParts(
        "IMG_1.JPG", None
    )
    assert parse_sidecar_name("metadata.json") is None
    assert parse_sidecar_name("metadata(1).json") is None
    assert parse_sidecar_name("IMG_1.JPG") is None


def test_truncated_supplemental_suffix_is_stripped() -> None:
    assert parse_sidecar_name("IMG_1.JPG.supplemental-me.json") == NameParts("IMG_1.JPG", None)
    assert parse_sidecar_name("IMG_1.JPG.sup.json") == NameParts("IMG_1.JPG", None)


def test_exact_match() -> None:
    (match,) = match_directory(["IMG_1.JPG.json"], ["IMG_1.JPG", "IMG_2.JPG"])
    assert (match.media_name, match.method) == ("IMG_1.JPG", "exact")


# --- Pathology 1b: truncated base names ----------------------------------------


def test_truncation_match() -> None:
    long_name = "a_very_long_filename_that_google_truncates_in_sidecars_2015.jpg"
    (match,) = match_directory([long_name[:46] + ".json"], [long_name, "other.jpg"])
    assert (match.media_name, match.method) == (long_name, "truncation")


def test_ambiguous_truncation_goes_unmatched() -> None:
    (match,) = match_directory(
        ["shared_prefix_.json"], ["shared_prefix_a.jpg", "shared_prefix_b.jpg"]
    )
    assert match.media_name is None
    assert (match.method, match.reason) == ("unmatched", "ambiguous_truncation")


# --- Pathology 1c: (n) numbered duplicates, both orderings ---------------------


def test_numbered_n_after_extension() -> None:
    (match,) = match_directory(
        ["IMG_1.JPG(1).json"], ["IMG_1.JPG", "IMG_1(1).JPG"]
    )
    assert (match.media_name, match.method) == ("IMG_1(1).JPG", "numbered")


def test_numbered_n_before_extension_is_exact() -> None:
    (match,) = match_directory(["IMG_1(1).JPG.json"], ["IMG_1.JPG", "IMG_1(1).JPG"])
    assert (match.media_name, match.method) == ("IMG_1(1).JPG", "exact")


def test_numbered_supplemental() -> None:
    (match,) = match_directory(
        ["IMG_1.JPG.supplemental-metadata(1).json"], ["IMG_1.JPG", "IMG_1(1).JPG"]
    )
    assert (match.media_name, match.method) == ("IMG_1(1).JPG", "numbered")


def test_numbered_without_twin_never_steals_the_base_sidecar() -> None:
    (match,) = match_directory(["IMG_1.JPG(1).json"], ["IMG_1.JPG"])
    assert match.media_name is None
    assert match.reason == "no_media_match"


# --- Pathology 2: -edited pairs ------------------------------------------------


def test_edited_original_names() -> None:
    assert edited_original("IMG_1-edited.JPG") == "IMG_1.JPG"
    assert edited_original("IMG_1.JPG") is None
    assert edited_original("-edited.JPG") is None


# --- Pathology 6: Google recompression heuristic -------------------------------


def test_recompression_heuristic() -> None:
    assert is_google_recompressed({}, "image/jpeg") is True
    assert is_google_recompressed({"EXIF:Software": "Google"}, "image/jpeg") is True
    assert is_google_recompressed({"EXIF:Make": "Canon"}, "image/jpeg") is False
    assert is_google_recompressed({"MakerNotes:Quality": "fine"}, "image/jpeg") is False
    assert is_google_recompressed({}, "image/png") is False


# --- Sidecar JSON parsing ------------------------------------------------------


def test_parse_sidecar_text_full() -> None:
    body = json.dumps(
        {
            "title": "IMG_1.JPG",
            "description": "Lake hike",
            "photoTakenTime": {"timestamp": "1429349400"},
            "creationTime": {"timestamp": "1431941400"},
            "geoData": {"latitude": 44.06, "longitude": -71.29},
        }
    )
    data = parse_sidecar_text(body)
    assert data is not None
    assert data.photo_taken_time == "2015-04-18T09:30:00+00:00"
    assert data.creation_time == "2015-05-18T09:30:00+00:00"
    assert data.gps_lat == 44.06
    assert data.description == "Lake hike"


def test_parse_sidecar_zero_gps_is_absent() -> None:
    data = parse_sidecar_text('{"geoData": {"latitude": 0.0, "longitude": 0.0}}')
    assert data is not None
    assert data.gps_lat is None and data.gps_lon is None


def test_parse_sidecar_invalid_json_is_none() -> None:
    assert parse_sidecar_text("not json {") is None


# --- Integration over the fixture corpus ---------------------------------------

needs_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)


@pytest.fixture
def normalized(tmp_path: Path) -> tuple[WorkingTree, sqlite3.Connection]:
    corpus = tmp_path / "corpus"
    generate_corpus(corpus, seed=0)
    wt, _ = init_working_tree(tmp_path / "worktree")
    config = wt.config_path
    text = config.read_text(encoding="utf-8")
    text = text.replace("confirmed = false", "confirmed = true")
    text = text.replace("parallelism = 0", "parallelism = 1")
    config.write_text(text, encoding="utf-8")
    conn = open_catalog(wt.catalog_path)
    cfg = load_config(config)
    log = logging.getLogger("archive_pipeline.test")
    with record_run(conn, "ingest") as run_id:
        ingest_source(conn, cfg, "TAKEOUT:t2015", corpus / "TAKEOUT", run_id, log)
    return wt, conn


@needs_exiftool
def test_integration_matches_all_pathologies(
    normalized: tuple[WorkingTree, sqlite3.Connection]
) -> None:
    wt, conn = normalized
    log = logging.getLogger("archive_pipeline.test")
    summary = normalize_takeout(conn, wt, log)

    assert summary.changed
    assert summary.matched_by_method == {"exact": 3, "truncation": 1, "numbered": 1}
    assert summary.unmatched_sidecars == 0
    # The album copy of IMG_2015_001 has no sidecar of its own.
    assert summary.unmatched_media == 1
    assert summary.edited_pairs == 1
    assert summary.albums == 1 and summary.album_memberships == 1

    taken = conn.execute(
        "SELECT s.photo_taken_time, s.match_method FROM takeout_sidecar s"
        " JOIN instance m ON m.id = s.media_instance_id"
        " WHERE m.rel_path LIKE '%IMG_2015_001.jpg'"
    ).fetchone()
    assert taken["photo_taken_time"] == "2015-04-18T09:30:00+00:00"
    assert taken["match_method"] == "exact"

    edited = conn.execute(
        "SELECT e.rel_path AS edited, o.rel_path AS original FROM edited_pair p"
        " JOIN instance e ON e.id = p.edited_instance_id"
        " JOIN instance o ON o.id = p.original_instance_id"
    ).fetchone()
    assert edited["edited"].endswith("IMG_2015_003-edited.jpg")
    assert edited["original"].endswith("IMG_2015_003.jpg")

    album = conn.execute(
        "SELECT a.album, m.rel_path FROM album_membership a"
        " JOIN instance m ON m.id = a.media_instance_id"
    ).fetchone()
    assert album["album"] == "Vacation 2015"
    assert album["rel_path"].endswith("Vacation 2015/IMG_2015_001.jpg")

    # Recompression: plain no-EXIF Takeout JPEGs are flagged; the one with a
    # camera Make is not.
    flagged = {
        row["rel_path"]
        for row in conn.execute(
            "SELECT rel_path FROM instance WHERE google_recompressed = 1"
        )
    }
    assert any(r.endswith("IMG_2015_002.jpg") for r in flagged)
    assert not any(r.endswith("IMG_2015_001.jpg") for r in flagged)

    report = (wt.reports_dir / "takeout_unmatched.csv").read_text(encoding="utf-8")
    assert "no_sidecar" in report and "Vacation 2015/IMG_2015_001.jpg" in report


@needs_exiftool
def test_integration_rerun_is_idempotent(
    normalized: tuple[WorkingTree, sqlite3.Connection]
) -> None:
    wt, conn = normalized
    log = logging.getLogger("archive_pipeline.test")
    first = normalize_takeout(conn, wt, log)
    decisions_after_first = conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0]
    second = normalize_takeout(conn, wt, log)
    assert first.changed and not second.changed
    assert first.matched_by_method == second.matched_by_method
    assert conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == decisions_after_first


@needs_exiftool
def test_unknown_source_is_an_error(
    normalized: tuple[WorkingTree, sqlite3.Connection]
) -> None:
    wt, conn = normalized
    log = logging.getLogger("archive_pipeline.test")
    with pytest.raises(TakeoutError, match="not in catalog"):
        normalize_takeout(conn, wt, log, sources=["TAKEOUT:nope"])


def test_no_takeout_sources_is_an_error(tmp_path: Path) -> None:
    wt, _ = init_working_tree(tmp_path / "worktree")
    conn = open_catalog(wt.catalog_path)
    log = logging.getLogger("archive_pipeline.test")
    with pytest.raises(TakeoutError, match="no TAKEOUT sources"):
        normalize_takeout(conn, wt, log)
