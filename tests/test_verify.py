"""M8 tests: conservation proof, tamper detection, stats, import, purge."""

import json
import logging
import shutil
from pathlib import Path

import pytest
from conftest import PipelineEnv
from PIL import Image
from typer.testing import CliRunner

from archive_pipeline.cli import app
from archive_pipeline.dates import resolve_dates
from archive_pipeline.dedup import run_dedup
from archive_pipeline.materialize import run_materialize
from archive_pipeline.provenance import classify_local
from archive_pipeline.review.actions import batch_apply
from archive_pipeline.verify import VerifyError, collect_stats, run_verify

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

LOG = logging.getLogger("archive_pipeline.test")
runner = CliRunner()


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> PipelineEnv:
    """Fully materialized working tree (module-scoped; tamper tests restore)."""
    e = PipelineEnv(tmp_path_factory.mktemp("m8"))
    classify_local(e.conn, e.cfg, e.wt, LOG)
    resolve_dates(e.conn, e.cfg, e.wt, LOG)
    batch_apply(e.conn, "LOCAL", "scans", "exif")
    run_dedup(e.conn, e.cfg, e.wt, LOG)
    run_materialize(e.conn, e.cfg, e.wt, LOG, execute=False)
    run_materialize(e.conn, e.cfg, e.wt, LOG, execute=True)
    return e


def test_verify_requires_materialization(tmp_path: Path) -> None:
    e = PipelineEnv(tmp_path)
    with pytest.raises(VerifyError, match="materialize"):
        run_verify(e.conn, e.wt, LOG)


def test_conservation_law_holds(env: PipelineEnv) -> None:
    result = run_verify(env.conn, env.wt, LOG)
    assert result.passed, [
        (d.kind, d.subject, d.detail) for d in result.discrepancies
    ]
    assert result.placements_total == result.instances_total
    assert result.archive_checked > 0 and result.quarantine_checked > 0
    report = json.loads(
        (env.wt.reports_dir / "verify_report.json").read_text("utf-8")
    )
    assert report["passed"] is True
    assert report["counts"]["discrepancies"] == 0
    assert report["stats"]["clusters"]["by_kind"]["exact"] == 7


def test_checksums_only_mode(env: PipelineEnv) -> None:
    result = run_verify(env.conn, env.wt, LOG, checksums_only=True)
    assert result.passed and result.checksums_only


def test_tampered_archive_file_fails(env: PipelineEnv) -> None:
    victim = next(p for p in env.wt.archive_dir.rglob("*.jpg"))
    original = victim.read_bytes()
    try:
        victim.write_bytes(original + b"\x00")
        result = run_verify(env.conn, env.wt, LOG)
        assert not result.passed
        assert any(d.kind == "hash_mismatch" for d in result.discrepancies)
    finally:
        victim.write_bytes(original)
    assert run_verify(env.conn, env.wt, LOG).passed


def test_missing_dest_file_fails(env: PipelineEnv) -> None:
    victim = next(p for p in env.wt.quarantine_dir.rglob("*__*"))
    original = victim.read_bytes()
    try:
        victim.unlink()
        result = run_verify(env.conn, env.wt, LOG)
        assert not result.passed
        assert any(d.kind == "missing_dest_file" for d in result.discrepancies)
    finally:
        victim.write_bytes(original)


def test_orphan_file_fails(env: PipelineEnv) -> None:
    orphan = env.wt.archive_dir / "1998" / "sneaky.jpg"
    try:
        orphan.write_bytes(b"unaccounted")
        result = run_verify(env.conn, env.wt, LOG)
        assert not result.passed
        assert any(d.kind == "orphan_in_archive" for d in result.discrepancies)
    finally:
        orphan.unlink()


def test_unplaced_instance_fails_conservation(env: PipelineEnv) -> None:
    with env.conn:
        cur = env.conn.execute(
            "INSERT INTO instance (source, rel_path, size_bytes, sha256, kind,"
            " ingest_run_id) VALUES ('LOCAL', 'phantom/new.jpg', 1, ?, 'image', 1)",
            ("feed" * 16,),
        )
        phantom = cur.lastrowid
    try:
        result = run_verify(env.conn, env.wt, LOG)
        assert not result.passed
        assert any(
            d.kind == "missing_placement" and "phantom/new.jpg" in d.subject
            for d in result.discrepancies
        )
        # checksums-only mode skips the completeness pass (cron use).
        assert run_verify(env.conn, env.wt, LOG, checksums_only=True).passed
    finally:
        with env.conn:
            env.conn.execute("DELETE FROM instance WHERE id = ?", (phantom,))
    assert run_verify(env.conn, env.wt, LOG).passed


def test_stats_takeout_only_videos(env: PipelineEnv) -> None:
    with env.conn:
        cur = env.conn.execute(
            "INSERT INTO instance (source, rel_path, size_bytes, sha256, kind,"
            " ingest_run_id) VALUES ('TAKEOUT:t2015', 'Google Photos/clip.mp4',"
            " 9, ?, 'video', 1)",
            ("ca11ab1e" * 8,),
        )
        vid = cur.lastrowid
    try:
        stats = collect_stats(env.conn)
        assert stats["takeout_only_videos"] == ["Google Photos/clip.mp4"]
    finally:
        with env.conn:
            env.conn.execute("DELETE FROM instance WHERE id = ?", (vid,))


def test_report_command(env: PipelineEnv) -> None:
    result = runner.invoke(app, ["--working-tree", str(env.wt.root), "report"])
    assert result.exit_code == 0, result.output
    assert "Clusters by kind" in result.output
    assert "exact: 7" in result.output
    assert "Last verify: PASSED" in result.output


def test_cli_verify_exit_codes(env: PipelineEnv) -> None:
    ok = runner.invoke(app, ["--working-tree", str(env.wt.root), "verify"])
    assert ok.exit_code == 0, ok.output
    assert "VERIFICATION PASSED" in ok.output
    victim = next(p for p in env.wt.archive_dir.rglob("*.jpg"))
    original = victim.read_bytes()
    try:
        victim.write_bytes(original + b"\x00")
        bad = runner.invoke(app, ["--working-tree", str(env.wt.root), "verify"])
        assert bad.exit_code == 1
        assert "VERIFICATION FAILED" in bad.output
        assert "hash_mismatch" in bad.output
    finally:
        victim.write_bytes(original)
    run_verify(env.conn, env.wt, LOG)  # leave a passing report behind


def test_maintain_import_chain(env: PipelineEnv, tmp_path: Path) -> None:
    new_root = tmp_path / "new-photos"
    (new_root / "2026-07").mkdir(parents=True)
    Image.new("RGB", (64, 48), (10, 200, 30)).save(
        new_root / "2026-07" / "fresh.jpg", quality=90
    )
    shutil.copyfile(  # exact duplicate of an already-archived photo
        env.local_root / "1998" / "beach_001.jpg",
        new_root / "2026-07" / "beach_copy.jpg",
    )
    result = runner.invoke(
        app,
        ["--working-tree", str(env.wt.root), "maintain", "import",
         "--root", str(new_root), "--source-id", "IMPORT:new-photos"],
    )
    assert result.exit_code == 0, result.output
    assert "Imported IMPORT:new-photos: 2 new/changed" in result.output

    # The duplicate joined the archived original's cluster as a loser.
    row = env.conn.execute(
        "SELECT m.role FROM cluster_member m JOIN instance i ON i.id ="
        " m.instance_id WHERE i.rel_path = '2026-07/beach_copy.jpg'"
    ).fetchone()
    assert row["role"] == "loser"

    # Materialize picks up only the new instances; then conservation holds.
    summary = run_materialize(env.conn, env.cfg, env.wt, LOG, execute=True)
    assert summary.skipped_done > 0
    assert any(
        "fresh__" in p.name for p in env.wt.archive_dir.rglob("*.jpg")
    )
    assert run_verify(env.conn, env.wt, LOG).passed


def test_purge_quarantine_gated_and_marker(env: PipelineEnv) -> None:
    root = str(env.wt.root)
    run_verify(env.conn, env.wt, LOG)  # ensure a passing report

    wrong = runner.invoke(
        app, ["--working-tree", root, "maintain", "purge-quarantine"],
        input="nope\n",
    )
    assert wrong.exit_code == 1
    assert "Nothing was deleted" in wrong.output
    assert any(env.wt.quarantine_dir.rglob("*__*"))

    ok = runner.invoke(
        app, ["--working-tree", root, "maintain", "purge-quarantine"],
        input="PURGE QUARANTINE\n",
    )
    assert ok.exit_code == 0, ok.output
    assert not any(env.wt.quarantine_dir.rglob("*__*"))
    assert (env.wt.quarantine_dir / ".purged.json").is_file()

    # Verify still passes, reporting the purge instead of failing.
    result = run_verify(env.conn, env.wt, LOG)
    assert result.passed and result.quarantine_purged

    decision = env.conn.execute(
        "SELECT actor FROM decision WHERE stage = 'purge-quarantine'"
    ).fetchone()
    assert decision["actor"] == "review:user"


def test_purge_refuses_without_passing_verify(tmp_path: Path) -> None:
    e = PipelineEnv(tmp_path)
    result = runner.invoke(
        app, ["--working-tree", str(e.wt.root), "maintain", "purge-quarantine"],
        input="PURGE QUARANTINE\n",
    )
    assert result.exit_code == 1
    assert "no verify report" in result.output
