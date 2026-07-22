"""Dedup integration: exact clusters over the corpus, near-dups, edited exclusion."""

import json
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest
from conftest import PipelineEnv
from PIL import Image

from archive_pipeline.catalog import open_catalog
from archive_pipeline.config import load_config
from archive_pipeline.dates import resolve_dates
from archive_pipeline.dedup import DedupError, run_dedup
from archive_pipeline.ingest import ingest_source
from archive_pipeline.provenance import classify_local
from archive_pipeline.review.actions import cluster_accept
from archive_pipeline.runs import record_run
from archive_pipeline.workingtree import WorkingTree, init_working_tree

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

LOG = logging.getLogger("archive_pipeline.test")


@pytest.fixture
def env(tmp_path: Path) -> PipelineEnv:
    e = PipelineEnv(tmp_path)
    classify_local(e.conn, e.cfg, e.wt, LOG)
    resolve_dates(e.conn, e.cfg, e.wt, LOG)
    return e


def _cluster_of(conn: sqlite3.Connection, rel_suffix: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT c.* FROM cluster c JOIN cluster_member m ON m.cluster_id = c.id"
        " JOIN instance i ON i.id = m.instance_id WHERE i.rel_path LIKE ?",
        (f"%{rel_suffix}",),
    ).fetchone()
    assert row is not None, f"no cluster containing {rel_suffix}"
    return row


def test_requires_date_resolution(tmp_path: Path) -> None:
    e = PipelineEnv(tmp_path)
    with pytest.raises(DedupError, match="date-resolve"):
        run_dedup(e.conn, e.cfg, e.wt, LOG)


def test_exact_clusters_over_corpus(env: PipelineEnv) -> None:
    summary = run_dedup(env.conn, env.cfg, env.wt, LOG)
    assert summary.by_kind == {"exact": 7}
    assert summary.pending_review == 0 and summary.auto == 7
    assert summary.singletons == 10
    assert summary.guardrails == {}

    beach = _cluster_of(env.conn, "1998/beach_001.jpg")
    winner = env.conn.execute(
        "SELECT rel_path FROM instance WHERE id = ?", (beach["winner_instance_id"],)
    ).fetchone()
    assert winner["rel_path"] == "1998/beach_001.jpg"  # curated original wins
    assert beach["status"] == "auto"

    img1 = _cluster_of(env.conn, "Vacation 2015/IMG_2015_001.jpg")
    members = env.conn.execute(
        "SELECT COUNT(*) FROM cluster_member WHERE cluster_id = ?", (img1["id"],)
    ).fetchone()[0]
    assert members == 4  # TAKEOUT x2 + merged google-import x2

    merged = json.loads(
        env.conn.execute(
            "SELECT merged_json FROM cluster_merge WHERE cluster_id = ?",
            (img1["id"],),
        ).fetchone()["merged_json"]
    )
    assert "Vacation 2015" in merged["keyword_candidates"]
    assert merged["date"]["date"] == "2015-04-18T09:30:00"
    assert merged["gps"]["source"] == "takeout"  # sidecar geoData, flagged
    assert "gps_from_takeout" in merged["flags"]
    assert merged["descriptions"] == ["Lake hike"]

    # Winner scores carry full breakdowns (INV-6 auditability).
    breakdown = env.conn.execute(
        "SELECT score_breakdown FROM cluster_member WHERE cluster_id = ?"
        " AND role = 'winner'",
        (beach["id"],),
    ).fetchone()
    assert "resolution" in breakdown["score_breakdown"]

    audit = (env.wt.reports_dir / "cluster_audit_sample.csv").read_text("utf-8")
    assert "beach_001.jpg" in audit


def test_rerun_is_idempotent_and_respects_review_locks(env: PipelineEnv) -> None:
    first = run_dedup(env.conn, env.cfg, env.wt, LOG)
    second = run_dedup(env.conn, env.cfg, env.wt, LOG)
    assert first.changed and not second.changed

    beach = _cluster_of(env.conn, "1998/beach_001.jpg")
    cluster_accept(env.conn, beach["id"])
    third = run_dedup(env.conn, env.cfg, env.wt, LOG)
    assert third.locked_reviewed == 2  # both beach members locked
    assert not third.changed
    row = env.conn.execute(
        "SELECT status FROM cluster WHERE id = ?", (beach["id"],)
    ).fetchone()
    assert row["status"] == "reviewed"


def _mini_env(tmp_path: Path) -> tuple[WorkingTree, sqlite3.Connection, Path]:
    wt, _ = init_working_tree(tmp_path / "worktree")
    text = wt.config_path.read_text(encoding="utf-8")
    text = text.replace("confirmed = false", "confirmed = true")
    text = text.replace("parallelism = 0", "parallelism = 1")
    wt.config_path.write_text(text, encoding="utf-8")
    return wt, open_catalog(wt.catalog_path), tmp_path / "LOCAL"


def _noise(seed: int, size: tuple[int, int] = (200, 150)) -> Image.Image:
    import random

    rng = random.Random(seed)
    img = Image.new("RGB", size, (rng.randrange(256),) * 3)
    for _ in range(60):
        x0, y0 = rng.randrange(size[0]), rng.randrange(size[1])
        img.paste(
            (rng.randrange(256), rng.randrange(256), rng.randrange(256)),
            (x0, y0, min(size[0], x0 + 20), min(size[1], y0 + 20)),
        )
    return img


def test_near_duplicates_and_edited_exclusion(tmp_path: Path) -> None:
    wt, conn, local = _mini_env(tmp_path)
    d = local / "2020"
    d.mkdir(parents=True)
    base1 = _noise(1)
    base1.save(d / "a1.jpg", quality=95)
    base1.resize((100, 75)).save(d / "b1.jpg", quality=85)  # near dup, 1/4 res
    base2 = _noise(2)
    base2.save(d / "a2.jpg", quality=95)
    base2.save(d / "c2.jpg", quality=50)  # near dup, same res -> margin review
    base3 = _noise(3)
    base3.save(d / "e.jpg", quality=95)
    base3.save(d / "e-edited.jpg", quality=70)  # near dup but edited-linked

    cfg = load_config(wt.config_path)
    with record_run(conn, "ingest") as run_id:
        ingest_source(conn, cfg, "LOCAL", local, run_id, LOG)
    ids = {
        row["rel_path"]: row["id"]
        for row in conn.execute("SELECT id, rel_path FROM instance")
    }
    with conn:
        conn.execute(
            "INSERT INTO edited_pair (edited_instance_id, original_instance_id)"
            " VALUES (?, ?)",
            (ids["2020/e-edited.jpg"], ids["2020/e.jpg"]),
        )
    classify_local(conn, cfg, wt, LOG)
    resolve_dates(conn, cfg, wt, LOG)
    summary = run_dedup(conn, cfg, wt, LOG)

    assert summary.by_kind.get("near_image") == 2

    resolution_cluster = _cluster_of(conn, "2020/a1.jpg")
    assert resolution_cluster["status"] == "auto"  # 4x resolution gap > margin
    winner = conn.execute(
        "SELECT rel_path FROM instance WHERE id = ?",
        (resolution_cluster["winner_instance_id"],),
    ).fetchone()
    assert winner["rel_path"] == "2020/a1.jpg"

    margin_cluster = _cluster_of(conn, "2020/a2.jpg")
    assert margin_cluster["status"] == "pending"
    decision = conn.execute(
        "SELECT detail FROM decision WHERE subject = ? AND stage = 'dedup'",
        (f"cluster:{margin_cluster['id']}",),
    ).fetchone()
    assert "score_margin" in decision["detail"]

    # The edited pair is linked, not clustered: neither file is in any cluster.
    for rel in ("2020/e.jpg", "2020/e-edited.jpg"):
        in_cluster = conn.execute(
            "SELECT COUNT(*) FROM cluster_member WHERE instance_id = ?",
            (ids[rel],),
        ).fetchone()[0]
        assert in_cluster == 0, f"{rel} must not be clustered"
