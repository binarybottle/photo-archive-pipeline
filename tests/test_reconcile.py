"""Stage 8 tests: adopting hand-curation of archive/ back into the ledger."""

import json
import logging
import shutil
from pathlib import Path

import pytest
from conftest import PipelineEnv
from typer.testing import CliRunner

from archive_pipeline.cli import app
from archive_pipeline.config import Config
from archive_pipeline.dates import resolve_dates
from archive_pipeline.dedup import run_dedup
from archive_pipeline.materialize import run_materialize
from archive_pipeline.provenance import classify_local
from archive_pipeline.reconcile import (
    ReconcileError,
    interpret_folder,
    plan_reconcile,
    read_trash,
    run_reconcile,
)
from archive_pipeline.review.actions import batch_apply
from archive_pipeline.sidecars import run_apply_sidecars, sidecar_args
from archive_pipeline.verify import run_verify

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

LOG = logging.getLogger("archive_pipeline.test")
runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path) -> PipelineEnv:
    """A materialized working tree that verify passes over."""
    e = PipelineEnv(tmp_path)
    classify_local(e.conn, e.cfg, e.wt, LOG)
    resolve_dates(e.conn, e.cfg, e.wt, LOG)
    batch_apply(e.conn, "LOCAL", "scans", "exif")
    run_dedup(e.conn, e.cfg, e.wt, LOG)
    run_materialize(e.conn, e.cfg, e.wt, LOG, execute=False)
    run_materialize(e.conn, e.cfg, e.wt, LOG, execute=True)
    assert run_verify(e.conn, e.wt, LOG).passed
    return e


def _archived(env: PipelineEnv, limit: int = 5) -> list[tuple[int, str]]:
    return [
        (row["instance_id"], row["dest_rel_path"])
        for row in env.conn.execute(
            "SELECT instance_id, dest_rel_path FROM placement"
            " WHERE disposition = 'archive' AND dest_rel_path LIKE '%.jpg'"
            " ORDER BY dest_rel_path LIMIT ?",
            (limit,),
        )
    ]


def _move(env: PipelineEnv, rel: str, new_dir: str) -> str:
    src = env.wt.archive_dir / rel
    dest = env.wt.archive_dir / new_dir / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return f"{new_dir}/{src.name}"


def _digikam_delete(env: PipelineEnv, rel: str) -> None:
    """Delete a file the way digiKam does: into .dtrash with an info record."""
    src = env.wt.archive_dir / rel
    trash = env.wt.archive_dir / ".dtrash"
    (trash / "files").mkdir(parents=True, exist_ok=True)
    (trash / "info").mkdir(parents=True, exist_ok=True)
    stem = src.stem + "-deadbeef"
    shutil.move(str(src), str(trash / "files" / (stem + src.suffix)))
    (trash / "info" / f"{stem}.dtrashinfo").write_text(
        json.dumps({"deletiontimestamp": "2026-07-25T16:24:12", "imageid": "1",
                    "path": str((env.wt.archive_dir / rel).resolve())}),
        encoding="utf-8",
    )


# --- Folder interpretation ------------------------------------------------------


def test_date_folder_reads_as_a_date() -> None:
    intent = interpret_folder("2005/2005-01/x__ab12cd34.jpg", Config())
    assert (intent.date, intent.precision) == ("2005-01-01", "month")
    assert intent.keywords == ()


def test_topical_folder_reads_as_keywords() -> None:
    intent = interpret_folder("caves/2006/x__ab12cd34.jpg", Config())
    assert intent.keywords == ("caves",)
    assert (intent.date, intent.precision) == ("2006-01-01", "year")


def test_year_range_and_bucket_folders_do_not_date() -> None:
    for folder, bucket in (("2004-2006", "2004-2006"), ("undated", "undated"),
                           ("pre-2000", "pre-2000")):
        intent = interpret_folder(f"{folder}/x__ab12cd34.jpg", Config())
        assert intent.bucket == bucket, folder
        assert intent.date is None, folder
        assert intent.keywords == (), folder


# --- Planning -------------------------------------------------------------------


def test_move_into_agreeing_year_keeps_the_precise_date(env: PipelineEnv) -> None:
    """A year folder that contains the resolved date must not coarsen it."""
    instance_id, rel = _archived(env, 1)[0]
    date = env.conn.execute(
        "SELECT resolved_date FROM date_resolution WHERE instance_id = ?",
        (instance_id,),
    ).fetchone()[0]
    _move(env, rel, f"caves/{date[:4]}")

    plan = plan_reconcile(env.conn, env.cfg, env.wt)
    move = next(m for m in plan.moves if m.instance_id == instance_id)
    assert move.date_action == "keep"
    assert move.intent.keywords == ("caves",)


def test_move_into_contradicting_month_corrects_the_date(env: PipelineEnv) -> None:
    instance_id, rel = _archived(env, 1)[0]
    _move(env, rel, "1975/1975-04")

    plan = plan_reconcile(env.conn, env.cfg, env.wt)
    move = next(m for m in plan.moves if m.instance_id == instance_id)
    assert move.date_action == "correct"
    assert move.intent.date == "1975-04-01"
    assert move.intent.precision == "month"


def test_move_into_bucket_demotes_the_date(env: PipelineEnv) -> None:
    instance_id, rel = _archived(env, 1)[0]
    _move(env, rel, "pre-2000")

    plan = plan_reconcile(env.conn, env.cfg, env.wt)
    move = next(m for m in plan.moves if m.instance_id == instance_id)
    assert move.date_action == "demote"
    assert move.intent.bucket == "pre-2000"


def test_digikam_deletion_is_confirmed_from_the_trash(env: PipelineEnv) -> None:
    instance_id, rel = _archived(env, 1)[0]
    _digikam_delete(env, rel)

    assert read_trash(env.wt) == {rel: Path(rel).stem + "-deadbeef"}
    plan = plan_reconcile(env.conn, env.cfg, env.wt)
    assert [(r.instance_id, r.rel) for r in plan.removals] == [(instance_id, rel)]
    assert plan.unaccounted == []


def test_deletion_without_a_trash_record_is_unaccounted(env: PipelineEnv) -> None:
    """A file that vanished with no evidence is reported, never silently adopted."""
    _, rel = _archived(env, 1)[0]
    (env.wt.archive_dir / rel).unlink()

    plan = plan_reconcile(env.conn, env.cfg, env.wt)
    assert [r.rel for r in plan.unaccounted] == [rel]
    assert plan.removals == []


def test_emptied_trash_still_reconciles_when_asked(env: PipelineEnv) -> None:
    """After the trash is emptied the evidence is gone, but the intent was not."""
    instance_id, rel = _archived(env, 1)[0]
    _digikam_delete(env, rel)
    shutil.rmtree(env.wt.archive_dir / ".dtrash")

    refused = run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)
    assert len(refused.unaccounted) == 1
    assert env.conn.execute(
        "SELECT disposition FROM placement WHERE instance_id = ?", (instance_id,)
    ).fetchone()[0] == "archive"

    adopted = run_reconcile(
        env.conn, env.cfg, env.wt, LOG, execute=True, adopt_unaccounted=True
    )
    assert adopted.unaccounted == []
    assert [r.instance_id for r in adopted.removals] == [instance_id]
    assert run_verify(env.conn, env.wt, LOG, cfg=env.cfg).passed

    detail = json.loads(env.conn.execute(
        "SELECT detail FROM decision WHERE subject = ? AND rule = 'reconcile.removed'",
        (f"instance:{instance_id}",),
    ).fetchone()[0])
    assert detail["confirmed_by"] == "absence"


def test_verify_passes_after_the_trash_is_emptied(env: PipelineEnv) -> None:
    """Adopted deletions stay proven once digiKam's trash is gone."""
    _, rel = _archived(env, 1)[0]
    _digikam_delete(env, rel)
    run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)
    assert run_verify(env.conn, env.wt, LOG, cfg=env.cfg).passed

    shutil.rmtree(env.wt.archive_dir / ".dtrash")
    result = run_verify(env.conn, env.wt, LOG, cfg=env.cfg)
    assert result.passed, [(d.kind, d.subject) for d in result.discrepancies[:5]]
    assert result.removed_count == 1


def test_photo_manager_files_are_ignored_not_orphaned(env: PipelineEnv) -> None:
    (env.wt.archive_dir / ".DS_Store").write_bytes(b"junk")
    (env.wt.archive_dir / ".dtrash").mkdir(exist_ok=True)
    (env.wt.archive_dir / ".dtrash" / "digikam.uuid").write_text("uuid", encoding="utf-8")

    plan = plan_reconcile(env.conn, env.cfg, env.wt)
    assert plan.clean
    assert plan.ignored == 2
    assert run_verify(env.conn, env.wt, LOG, cfg=env.cfg).passed


def test_unknown_media_is_reported_separately(env: PipelineEnv) -> None:
    (env.wt.archive_dir / "1999" / "intruder.jpg").parent.mkdir(
        parents=True, exist_ok=True
    )
    (env.wt.archive_dir / "1999" / "intruder.jpg").write_bytes(b"not from the pipeline")

    plan = plan_reconcile(env.conn, env.cfg, env.wt)
    assert plan.unknown_media == ["1999/intruder.jpg"]
    assert plan.moves == []


def test_reconcile_requires_materialization(tmp_path: Path) -> None:
    e = PipelineEnv(tmp_path)
    with pytest.raises(ReconcileError, match="materialize"):
        plan_reconcile(e.conn, e.cfg, e.wt)


# --- Applying -------------------------------------------------------------------


def test_dry_run_changes_nothing(env: PipelineEnv) -> None:
    instance_id, rel = _archived(env, 1)[0]
    _move(env, rel, "caves")

    run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=False)
    assert env.conn.execute(
        "SELECT dest_rel_path FROM placement WHERE instance_id = ?", (instance_id,)
    ).fetchone()[0] == rel
    assert env.conn.execute("SELECT COUNT(*) FROM manual_keyword").fetchone()[0] == 0


def test_execute_adopts_moves_and_restores_verify(env: PipelineEnv) -> None:
    moved = _archived(env, 3)
    new_rels = {iid: _move(env, rel, "caves") for iid, rel in moved}
    deleted_id, deleted_rel = _archived(env, 5)[4]
    _digikam_delete(env, deleted_rel)

    assert not run_verify(env.conn, env.wt, LOG, cfg=env.cfg).passed  # drift is visible

    plan = run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)
    assert len(plan.moves) == 3
    assert len(plan.removals) == 1
    for instance_id, new_rel in new_rels.items():
        assert env.conn.execute(
            "SELECT dest_rel_path FROM placement WHERE instance_id = ?", (instance_id,)
        ).fetchone()[0] == new_rel
    assert env.conn.execute(
        "SELECT disposition FROM placement WHERE instance_id = ?", (deleted_id,)
    ).fetchone()[0] == "removed"

    result = run_verify(env.conn, env.wt, LOG, cfg=env.cfg)
    assert result.passed, [(d.kind, d.subject) for d in result.discrepancies[:5]]
    assert result.removed_count == 1


def test_adoption_is_idempotent(env: PipelineEnv) -> None:
    _, rel = _archived(env, 1)[0]
    _move(env, rel, "caves")

    run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)
    second = run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)
    assert second.clean
    assert env.conn.execute("SELECT COUNT(*) FROM manual_keyword").fetchone()[0] == 1


def test_date_correction_is_logged_with_its_predecessor(env: PipelineEnv) -> None:
    instance_id, rel = _archived(env, 1)[0]
    before = env.conn.execute(
        "SELECT resolved_date FROM date_resolution WHERE instance_id = ?", (instance_id,)
    ).fetchone()[0]
    _move(env, rel, "1975/1975-04")
    run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)

    row = env.conn.execute(
        "SELECT resolved_date, resolved_source, status FROM date_resolution"
        " WHERE instance_id = ?", (instance_id,)
    ).fetchone()
    assert row["resolved_date"] == "1975-04-01"
    assert (row["resolved_source"], row["status"]) == ("folder_move", "reviewed")

    detail = json.loads(env.conn.execute(
        "SELECT detail FROM decision WHERE subject = ? AND rule ="
        " 'reconcile.date_corrected'", (f"instance:{instance_id}",)
    ).fetchone()[0])
    assert detail["from"] == before
    assert detail["to"] == "1975-04-01"


def test_restoring_a_deleted_file_is_adopted_back(env: PipelineEnv) -> None:
    instance_id, rel = _archived(env, 1)[0]
    _digikam_delete(env, rel)
    run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)

    trashed = next((env.wt.archive_dir / ".dtrash" / "files").iterdir())
    shutil.move(str(trashed), str(env.wt.archive_dir / rel))
    plan = run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)

    assert [r.instance_id for r in plan.restorations] == [instance_id]
    assert env.conn.execute(
        "SELECT disposition FROM placement WHERE instance_id = ?", (instance_id,)
    ).fetchone()[0] == "archive"


# --- Sidecars -------------------------------------------------------------------


def test_sidecar_args_preserve_the_previous_date() -> None:
    args = sidecar_args("2005-01-01", "month", None, ["caves"], "2011-06-02T09:00:00")
    assert "-XMP-ArchivePipe:OriginalDate=2011-06-02T09:00:00" in args
    assert "-DateTimeOriginal=2005:01:01 00:00:00" in args
    assert args.index("-XMP-dc:Subject-=caves") < args.index("-XMP-dc:Subject+=caves")


def test_apply_sidecars_writes_metadata_without_touching_bytes(env: PipelineEnv) -> None:
    instance_id, rel = _archived(env, 1)[0]
    new_rel = _move(env, rel, "caves/1975/1975-04")
    run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)
    media = env.wt.archive_dir / new_rel
    before = media.read_bytes()

    dry = run_apply_sidecars(env.conn, env.cfg, env.wt, LOG, execute=False)
    assert dry.pending == 1
    assert not (env.wt.archive_dir / (new_rel + ".xmp")).exists()

    summary = run_apply_sidecars(env.conn, env.cfg, env.wt, LOG, execute=True)
    assert summary.written == 1
    assert media.read_bytes() == before

    sidecar = env.wt.archive_dir / (new_rel + ".xmp")
    text = sidecar.read_text(encoding="utf-8")
    assert "1975-04-01" in text or "1975:04:01" in text
    assert "caves" in text
    assert env.conn.execute(
        "SELECT written_at FROM sidecar_task WHERE instance_id = ?", (instance_id,)
    ).fetchone()[0] is not None
    assert run_verify(env.conn, env.wt, LOG, cfg=env.cfg).passed


def test_apply_sidecars_is_idempotent(env: PipelineEnv) -> None:
    _, rel = _archived(env, 1)[0]
    new_rel = _move(env, rel, "caves")
    run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)
    run_apply_sidecars(env.conn, env.cfg, env.wt, LOG, execute=True)
    first = (env.wt.archive_dir / (new_rel + ".xmp")).read_text(encoding="utf-8")

    again = run_apply_sidecars(env.conn, env.cfg, env.wt, LOG, execute=True)
    assert again.pending == 0
    assert (env.wt.archive_dir / (new_rel + ".xmp")).read_text(encoding="utf-8") == first


def test_keywords_are_not_appended_twice(env: PipelineEnv) -> None:
    """Re-queued work must not duplicate a keyword already in the sidecar."""
    instance_id, rel = _archived(env, 1)[0]
    new_rel = _move(env, rel, "caves")
    run_reconcile(env.conn, env.cfg, env.wt, LOG, execute=True)
    run_apply_sidecars(env.conn, env.cfg, env.wt, LOG, execute=True)
    with env.conn:
        env.conn.execute(
            "UPDATE sidecar_task SET written_at = NULL WHERE instance_id = ?",
            (instance_id,),
        )
    run_apply_sidecars(env.conn, env.cfg, env.wt, LOG, execute=True)

    text = (env.wt.archive_dir / (new_rel + ".xmp")).read_text(encoding="utf-8")
    assert text.count("<rdf:li>caves</rdf:li>") == 1


# --- CLI ------------------------------------------------------------------------


def test_cli_reconcile_dry_run_then_execute(env: PipelineEnv) -> None:
    _, rel = _archived(env, 1)[0]
    _move(env, rel, "caves")
    env.conn.close()

    args = ["--working-tree", str(env.wt.root), "maintain", "reconcile"]
    dry = runner.invoke(app, args)
    assert dry.exit_code == 0, dry.output
    assert "Dry run" in dry.output

    done = runner.invoke(app, [*args, "--execute"])
    assert done.exit_code == 0, done.output
    assert "Adopted into the catalog." in done.output
    assert (env.wt.reports_dir / "reconcile_report.json").is_file()
    assert (env.wt.reports_dir / "reconcile_drift.csv").is_file()


def test_cli_reports_a_clean_archive(env: PipelineEnv) -> None:
    env.conn.close()
    result = runner.invoke(
        app, ["--working-tree", str(env.wt.root), "maintain", "reconcile"]
    )
    assert result.exit_code == 0, result.output
    assert "nothing to reconcile" in result.output
