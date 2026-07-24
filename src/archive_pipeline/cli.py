"""``archive`` CLI: staged pipeline commands (spec section 5).

All stages are implemented: init, ingest (Stage 0 preserve gate + Stage 1),
takeout-normalize (2), local-provenance (2b), date-resolve (3), review serve
(4), dedup (5), materialize (6, dry-run by default), verify/report (7), and
maintain verify-checksums / import / purge-quarantine (8).

Usage:
    $ archive --working-tree /archive-project init
    $ archive fixtures generate --dest /tmp/corpus --seed 42
    $ archive --working-tree /archive-project ingest --source LOCAL --root /photos
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from archive_pipeline import __version__
from archive_pipeline.catalog import open_catalog, schema_version
from archive_pipeline.config import Config, ConfigError, load_config
from archive_pipeline.dates import AUDIT_REPORT, DateResolveError, resolve_dates
from archive_pipeline.dedup import CLUSTER_AUDIT_REPORT, DedupError, run_dedup
from archive_pipeline.fixtures.generator import generate_corpus
from archive_pipeline.ingest import IngestError, ingest_source
from archive_pipeline.logs import configure_logging
from archive_pipeline.materialize import (
    ARCHIVE_MANIFEST,
    KEYWORD_MAP,
    QUARANTINE_MANIFEST,
    MaterializeError,
    run_materialize,
)
from archive_pipeline.provenance import PROVENANCE_REPORT, ProvenanceError, classify_local
from archive_pipeline.runs import record_run
from archive_pipeline.space import SpaceError
from archive_pipeline.staging import StagingError, stage_takeout_zip
from archive_pipeline.takeout import UNMATCHED_REPORT, TakeoutError, normalize_takeout
from archive_pipeline.verify import (
    PURGED_MARKER,
    VERIFY_REPORT,
    VerifyError,
    collect_stats,
    run_verify,
)
from archive_pipeline.workingtree import WorkingTree, init_working_tree

app = typer.Typer(
    name="archive",
    help="Non-destructive photo/video archive consolidation pipeline.",
    no_args_is_help=True,
)
review_app = typer.Typer(help="Human review of date conflicts and duplicate clusters.")
maintain_app = typer.Typer(help="Ongoing archive maintenance.")
fixtures_app = typer.Typer(help="Deterministic synthetic test corpora (spec section 9).")
app.add_typer(review_app, name="review")
app.add_typer(maintain_app, name="maintain")
app.add_typer(fixtures_app, name="fixtures")

PRESERVE_REMINDER = (
    "Refusing to run: preserve.confirmed is false in config.toml.\n"
    "\n"
    "Stage 0 (Preserve) comes first: make a verbatim backup of LOCAL and every\n"
    "Takeout archive on a separate physical disk. Setting preserve.confirmed = true\n"
    "asserts that this backup exists and is current. The pipeline never modifies\n"
    "sources, but only your backup protects against drive failure mid-run."
)


class SourceKind(StrEnum):
    """Which kind of source root is being ingested."""

    LOCAL = "LOCAL"
    TAKEOUT = "TAKEOUT"


def _working_tree(ctx: typer.Context) -> WorkingTree:
    """The working tree selected by ``--working-tree`` / ARCHIVE_WORKING_TREE."""
    root: Path = ctx.obj
    return WorkingTree(root)


def _load_config_or_exit(wt: WorkingTree) -> Config:
    """Load the working tree's config; print the error and exit 1 on failure."""
    try:
        return load_config(wt.config_path)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"archive-pipeline {__version__}")
        raise typer.Exit(0)


@app.callback()
def main(
    ctx: typer.Context,
    working_tree: Annotated[
        Path,
        typer.Option(
            "--working-tree",
            "-w",
            envvar="ARCHIVE_WORKING_TREE",
            help="Pipeline-owned working tree (catalog, archive, quarantine, reports, logs).",
        ),
    ] = Path("."),
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print version and exit.",
        ),
    ] = False,
) -> None:
    """Select the working tree for all subcommands."""
    ctx.obj = working_tree.resolve()


@app.command()
def init(ctx: typer.Context) -> None:
    """Create the working-tree layout, default config.toml, and catalog schema."""
    wt, config_created = init_working_tree(_working_tree(ctx).root)
    log = configure_logging(wt.logs_dir, stage="init", console=False)
    conn = open_catalog(wt.catalog_path)
    try:
        version = schema_version(conn)
        with record_run(conn, "init", {"root": str(wt.root)}):
            log.info(
                "working tree initialized",
                extra={"root": str(wt.root), "schema_version": version},
            )
    finally:
        conn.close()
    typer.echo(f"Working tree ready at {wt.root} (schema v{version}).")
    if config_created:
        typer.echo("Wrote default config.toml — review it, complete your Stage 0 backup,")
        typer.echo("then set preserve.confirmed = true before running `archive ingest`.")
    else:
        typer.echo("Existing config.toml kept unchanged.")


@app.command()
def ingest(
    ctx: typer.Context,
    source: Annotated[SourceKind, typer.Option("--source", help="Source kind.")],
    root: Annotated[
        Path, typer.Option("--root", help="Source root directory, or a Takeout zip (read-only).")
    ],
    export_id: Annotated[
        str | None,
        typer.Option(
            "--export-id",
            help="TAKEOUT only: identifier for this export (default: root's name).",
        ),
    ] = None,
) -> None:
    """Inventory every file in a source root into the catalog (Stage 1)."""
    wt = _working_tree(ctx)
    cfg = _load_config_or_exit(wt)
    if not cfg.preserve.confirmed:
        typer.secho(PRESERVE_REMINDER, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if source is SourceKind.LOCAL and export_id is not None:
        typer.secho("--export-id only applies to --source TAKEOUT", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    root = root.expanduser().resolve()
    source_id = "LOCAL" if source is SourceKind.LOCAL else f"TAKEOUT:{export_id or root.stem}"
    log = configure_logging(wt.logs_dir, stage="ingest")
    conn = open_catalog(wt.catalog_path)
    try:
        if source is SourceKind.TAKEOUT and root.is_file():
            try:
                root = stage_takeout_zip(
                    root, wt.staging_dir, export_id or root.stem, cfg.space.margin_pct
                )
            except (StagingError, SpaceError) as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(1) from exc
            typer.echo(f"Takeout zip staged at {root}")
        try:
            with record_run(
                conn, "ingest", {"source": source_id, "root": str(root)}
            ) as run_id:
                summary = ingest_source(conn, cfg, source_id, root, run_id, log)
        except IngestError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
    finally:
        conn.close()

    kinds = ", ".join(f"{kind}={n}" for kind, n in sorted(summary.by_kind.items()))
    typer.echo(
        f"Ingested {source_id}: {summary.discovered} files on disk,"
        f" {summary.processed} processed, {summary.skipped_unchanged} unchanged,"
        f" {summary.corrupt} corrupt."
    )
    typer.echo(f"Catalog rows: {summary.catalog_count} ({kinds});"
               f" sample-verified {summary.sample_checked} hashes.")
    if summary.missing_from_disk:
        typer.secho(
            f"WARNING: {summary.missing_from_disk} cataloged file(s) missing from disk"
            " — sources should never change; see logs.",
            fg=typer.colors.YELLOW,
        )


@app.command("takeout-normalize")
def takeout_normalize(
    ctx: typer.Context,
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            help="Restrict to one TAKEOUT source id (e.g. TAKEOUT:t2015); default: all.",
        ),
    ] = None,
) -> None:
    """Attach Takeout JSON sidecars to media instances (Stage 2, catalog-only)."""
    wt = _working_tree(ctx)
    log = configure_logging(wt.logs_dir, stage="takeout-normalize")
    conn = open_catalog(wt.catalog_path)
    try:
        with record_run(conn, "takeout-normalize", {"source": source}):
            summary = normalize_takeout(
                conn, wt, log, sources=[source] if source else None
            )
    except TakeoutError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    methods = ", ".join(f"{m}={n}" for m, n in sorted(summary.matched_by_method.items()))
    typer.echo(
        f"Normalized {len(summary.sources)} TAKEOUT source(s):"
        f" {summary.sidecars_total} sidecars ({methods or 'none matched'}),"
        f" {summary.unmatched_sidecars} unmatched sidecars."
    )
    typer.echo(
        f"Media: {summary.matched_media}/{summary.media_total} matched"
        f" ({summary.match_rate:.1%}); {summary.edited_pairs} edited pair(s),"
        f" {summary.albums} album(s) ({summary.album_memberships} memberships),"
        f" {summary.recompressed} flagged google_recompressed."
    )
    if not summary.changed:
        typer.echo("Catalog already up to date (no changes written).")
    if summary.unmatched_sidecars or summary.unmatched_media:
        typer.echo(f"Unmatched detail: {wt.reports_dir / UNMATCHED_REPORT}")


@app.command("local-provenance")
def local_provenance(ctx: typer.Context) -> None:
    """Classify LOCAL directories as curated vs takeout-derived (Stage 2b)."""
    wt = _working_tree(ctx)
    cfg = _load_config_or_exit(wt)
    log = configure_logging(wt.logs_dir, stage="local-provenance")
    conn = open_catalog(wt.catalog_path)
    try:
        with record_run(conn, "local-provenance", {}):
            summary = classify_local(conn, cfg, wt, log)
    except ProvenanceError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    typer.echo(
        f"Classified {summary.dirs_total} directories in {len(summary.sources)}"
        f" source(s): {summary.curated} curated, {summary.derived} takeout-derived"
        f" ({summary.overridden} by config override)."
    )
    typer.echo(
        f"Linked {summary.sidecars_linked} sidecar(s) inside derived dirs;"
        f" flagged {summary.recompressed_flagged} media google_recompressed."
    )
    typer.echo(f"Review {wt.reports_dir / PROVENANCE_REPORT} and set overrides in")
    typer.echo("config.toml [provenance] before running `archive date-resolve`.")


@app.command("date-resolve")
def date_resolve(
    ctx: typer.Context,
    sample: Annotated[
        int, typer.Option("--sample", help="Audit-sample size exported for user review.")
    ] = 200,
) -> None:
    """Resolve each media instance's capture date via the trust hierarchy (Stage 3)."""
    wt = _working_tree(ctx)
    cfg = _load_config_or_exit(wt)
    log = configure_logging(wt.logs_dir, stage="date-resolve")
    conn = open_catalog(wt.catalog_path)
    try:
        with record_run(conn, "date-resolve", {"sample": sample}):
            summary = resolve_dates(conn, cfg, wt, log, sample_size=sample)
    except DateResolveError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    statuses = ", ".join(f"{s}={n}" for s, n in sorted(summary.by_status.items()))
    rules = ", ".join(f"{r}={n}" for r, n in sorted(summary.by_rule.items()))
    typer.echo(f"Resolved dates for {summary.total} media instances: {statuses}.")
    typer.echo(f"Rules fired: {rules or 'none'}.")
    typer.echo(
        f"Conflict rate {summary.conflict_rate:.1%};"
        f" {summary.reviewed_preserved} reviewed row(s) preserved."
    )
    typer.echo(f"Audit sample ({summary.sample_size}): {wt.reports_dir / AUDIT_REPORT}")
    if not summary.changed:
        typer.echo("Catalog already up to date (no changes written).")


@review_app.command("serve")
def review_serve(
    ctx: typer.Context,
    port: Annotated[int, typer.Option("--port", help="TCP port on 127.0.0.1.")] = 8765,
) -> None:
    """Serve the local review UI (Stage 4). Binds to 127.0.0.1 only."""
    import uvicorn

    from archive_pipeline.review.app import create_app

    wt = _working_tree(ctx)
    if not wt.catalog_path.is_file():
        typer.secho(
            f"no catalog at {wt.catalog_path} (run `archive init` and the pipeline"
            " stages first)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    configure_logging(wt.logs_dir, stage="review")
    conn = open_catalog(wt.catalog_path)
    try:
        with record_run(conn, "review", {"port": port}):
            typer.echo(f"Review UI at http://127.0.0.1:{port} — Ctrl-C to stop.")
            uvicorn.run(create_app(wt), host="127.0.0.1", port=port, log_level="warning")
    finally:
        conn.close()


@app.command()
def dedup(
    ctx: typer.Context,
    sample: Annotated[
        int, typer.Option("--sample", help="Cluster audit-sample size exported.")
    ] = 200,
) -> None:
    """Cluster duplicates, score winners, plan metadata merges (Stage 5)."""
    wt = _working_tree(ctx)
    cfg = _load_config_or_exit(wt)
    log = configure_logging(wt.logs_dir, stage="dedup")
    conn = open_catalog(wt.catalog_path)
    try:
        with record_run(conn, "dedup", {"sample": sample}):
            summary = run_dedup(conn, cfg, wt, log, sample_size=sample)
    except DedupError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    kinds = ", ".join(f"{k}={n}" for k, n in sorted(summary.by_kind.items()))
    typer.echo(
        f"Clustered {summary.clusters_total} cluster(s) ({kinds or 'none'});"
        f" {summary.singletons} singleton(s); {summary.locked_reviewed}"
        " instance(s) locked by prior review."
    )
    guardrails = ", ".join(f"{g}={n}" for g, n in sorted(summary.guardrails.items()))
    typer.echo(
        f"Auto: {summary.auto}; awaiting review: {summary.pending_review}"
        f" ({guardrails or 'no guardrails fired'})."
    )
    typer.echo(f"Audit sample ({summary.sample_size}): {wt.reports_dir / CLUSTER_AUDIT_REPORT}")
    if summary.pending_review:
        typer.echo("Review pending clusters with `archive review serve`.")
    if not summary.changed:
        typer.echo("Catalog already up to date (no changes written).")


@app.command()
def materialize(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--execute",
            help="Dry-run (default) writes manifests only; --execute mutates the archive.",
        ),
    ] = True,
) -> None:
    """Write the archive and quarantine; dry-run by default (INV-4) (Stage 6)."""
    wt = _working_tree(ctx)
    cfg = _load_config_or_exit(wt)
    log = configure_logging(wt.logs_dir, stage="materialize")
    conn = open_catalog(wt.catalog_path)
    try:
        with record_run(conn, "materialize", {"execute": not dry_run}):
            summary = run_materialize(conn, cfg, wt, log, execute=not dry_run)
    except (MaterializeError, SpaceError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    mode = "EXECUTED" if not dry_run else "DRY-RUN (no files written)"
    typer.echo(
        f"Materialize {mode}: {summary.archived} to archive"
        f" ({summary.undated} undated, {summary.sidecars_written} xmp sidecars),"
        f" {summary.quarantined} to quarantine ({summary.quarantine_copies} distinct"
        f" copies), {summary.excluded} excluded."
    )
    typer.echo(
        f"Bytes to copy: {summary.bytes_planned:,}; already done (skipped):"
        f" {summary.skipped_done}."
    )
    if summary.write_fallbacks:
        typer.secho(
            f"{summary.write_fallbacks} file(s) had a damaged metadata block —"
            " image bytes kept bit-identical, metadata written to an XMP sidecar"
            f" instead ({summary.metadata_skipped} could not take a sidecar either"
            " and were placed without metadata). See logs for the file list.",
            fg=typer.colors.YELLOW,
        )
    if summary.keyword_map_created:
        typer.secho(
            f"Wrote default keyword map: {wt.reports_dir / KEYWORD_MAP} — review it"
            " before executing.",
            fg=typer.colors.YELLOW,
        )
    typer.echo(
        f"Manifests: {wt.reports_dir / ARCHIVE_MANIFEST},"
        f" {wt.reports_dir / QUARANTINE_MANIFEST}"
    )
    if dry_run:
        typer.echo("Review the manifests, then run `archive materialize --execute`.")
    else:
        typer.echo(f"Post-execute sample verified: {summary.sample_checked} file(s).")


def _run_verify_command(ctx: typer.Context, checksums_only: bool) -> None:
    wt = _working_tree(ctx)
    log = configure_logging(wt.logs_dir, stage="verify")
    conn = open_catalog(wt.catalog_path)
    stage = "maintain-verify-checksums" if checksums_only else "verify"
    try:
        with record_run(conn, stage, {"checksums_only": checksums_only}):
            result = run_verify(conn, wt, log, checksums_only=checksums_only)
    except VerifyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    typer.echo(
        f"Checked {result.archive_checked} archive and {result.quarantine_checked}"
        f" quarantine file(s) ({result.bytes_archive + result.bytes_quarantine:,}"
        f" bytes); {result.excluded_count} excluded; {result.placements_total}"
        f"/{result.instances_total} instances placed."
    )
    if result.quarantine_purged:
        typer.echo("Quarantine was purged; its file checks were skipped.")
    if result.passed:
        typer.secho("VERIFICATION PASSED (conservation law holds).", fg=typer.colors.GREEN)
        typer.echo(f"Report: {wt.reports_dir / VERIFY_REPORT}")
        return
    typer.secho(
        f"VERIFICATION FAILED: {result.discrepancy_count} discrepanc(ies).",
        fg=typer.colors.RED,
        err=True,
    )
    for d in result.discrepancies[:20]:
        typer.secho(f"  [{d.kind}] {d.subject}: {d.detail}", err=True)
    if result.discrepancy_count > 20:
        typer.secho(
            f"  ... {result.discrepancy_count - 20} more in the report.", err=True
        )
    typer.echo(f"Full report: {wt.reports_dir / VERIFY_REPORT}", err=True)
    raise typer.Exit(1)


@app.command()
def verify(ctx: typer.Context) -> None:
    """Prove the conservation law (INV-3) and verify all checksums (Stage 7)."""
    _run_verify_command(ctx, checksums_only=False)


@app.command()
def report(ctx: typer.Context) -> None:
    """Human-readable summary of the whole pipeline state (Stage 7)."""
    wt = _working_tree(ctx)
    conn = open_catalog(wt.catalog_path)
    try:
        stats = collect_stats(conn)
    finally:
        conn.close()

    def _section(title: str, pairs: dict[str, object]) -> None:
        typer.secho(title, bold=True)
        for key, value in sorted(pairs.items()):
            typer.echo(f"  {key}: {value}")

    _section("Instances", {"total": stats["instances"]["total"]})
    _section("  by kind", stats["instances"]["by_kind"])
    _section("  by source", stats["instances"]["by_source"])
    _section("Placements", stats["placements"]["by_disposition"])
    _section("  source bytes", stats["placements"]["source_bytes_by_disposition"])
    _section("Dates by status", stats["dates"]["by_status"])
    _section("Dates by source", stats["dates"]["by_source"])
    _section("Dates by precision", stats["dates"]["by_precision"])
    _section("Clusters by kind", stats["clusters"]["by_kind"])
    _section("Clusters by status", stats["clusters"]["by_status"])
    _section("Cluster size histogram", stats["clusters"]["size_histogram"])
    _section("Decisions by actor", stats["decisions"]["by_actor"])
    _section("Decisions by stage", stats["decisions"]["by_stage"])
    if stats["takeout_only_videos"]:
        typer.secho("Takeout-only videos (Google held these uniquely):", bold=True)
        for rel in stats["takeout_only_videos"]:
            typer.echo(f"  {rel}")
    _section(
        "Last run per stage",
        {stage: f"{r['status']} ({r['finished'] or 'running'})"
         for stage, r in stats["runs"].items()},
    )
    verify_report = wt.reports_dir / VERIFY_REPORT
    if verify_report.is_file():
        payload = json.loads(verify_report.read_text(encoding="utf-8"))
        state = "PASSED" if payload.get("passed") else "FAILED"
        typer.echo(f"Last verify: {state} at {payload.get('generated')}")
    else:
        typer.echo("Last verify: never run.")


@maintain_app.command("verify-checksums")
def maintain_verify_checksums(ctx: typer.Context) -> None:
    """Periodic (cron-able) checksum re-verification of archive + quarantine."""
    _run_verify_command(ctx, checksums_only=True)


@maintain_app.command("import")
def maintain_import(
    ctx: typer.Context,
    root: Annotated[Path, typer.Option("--root", help="New photos to import incrementally.")],
    source_id: Annotated[
        str | None,
        typer.Option("--source-id", help="Catalog source id (default IMPORT:<root name>)."),
    ] = None,
) -> None:
    """Incrementally import new photos: ingest -> dates -> dedup-against-archive."""
    wt = _working_tree(ctx)
    cfg = _load_config_or_exit(wt)
    if not cfg.preserve.confirmed:
        typer.secho(PRESERVE_REMINDER, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    root = root.expanduser().resolve()
    sid = source_id or f"IMPORT:{root.name}"
    log = configure_logging(wt.logs_dir, stage="maintain-import")
    conn = open_catalog(wt.catalog_path)
    try:
        with record_run(conn, "maintain-import", {"source": sid, "root": str(root)}) as run_id:
            ingest = ingest_source(conn, cfg, sid, root, run_id, log)
            classify_local(conn, cfg, wt, log)
            dates = resolve_dates(conn, cfg, wt, log)
            dedup_summary = run_dedup(conn, cfg, wt, log)
    except (IngestError, ProvenanceError, DateResolveError, DedupError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    typer.echo(
        f"Imported {sid}: {ingest.processed} new/changed file(s);"
        f" {dates.by_status.get('conflict', 0)} date conflict(s);"
        f" {dedup_summary.pending_review} cluster(s) awaiting review."
    )
    typer.echo(
        "Next: resolve any conflicts/clusters in `archive review serve`, then"
        " `archive materialize` (dry-run) and `--execute`."
    )


PURGE_PHRASE = "PURGE QUARANTINE"


@maintain_app.command("purge-quarantine")
def maintain_purge_quarantine(ctx: typer.Context) -> None:
    """Destroy quarantined files. Manual, gated on a passing verify (Stage 8)."""
    wt = _working_tree(ctx)
    report_path = wt.reports_dir / VERIFY_REPORT
    if not report_path.is_file():
        typer.secho(
            "Refusing: no verify report. Run `archive verify` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if not payload.get("passed"):
        typer.secho(
            "Refusing: the last verify FAILED. Fix discrepancies first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)
    marker = wt.quarantine_dir / PURGED_MARKER
    files = [
        p for p in sorted(wt.quarantine_dir.rglob("*"))
        if p.is_file() and p.name not in (PURGED_MARKER,)
    ]
    if not files:
        typer.echo("Quarantine is already empty.")
        raise typer.Exit(0)
    total_bytes = sum(p.stat().st_size for p in files)
    typer.secho(
        f"This will PERMANENTLY DESTROY {len(files)} file(s)"
        f" ({total_bytes:,} bytes) under {wt.quarantine_dir}:",
        fg=typer.colors.RED,
    )
    for p in files[:10]:
        typer.echo(f"  {p.relative_to(wt.quarantine_dir)}")
    if len(files) > 10:
        typer.echo(f"  ... and {len(files) - 10} more")
    typer.echo(
        "Recommendation: purge no sooner than 6 months after verify passes and"
        " 3-2-1 backups of archive/ + catalog.db + reports/ exist."
    )
    typed = typer.prompt(f"Type '{PURGE_PHRASE}' to proceed")
    if typed.strip() != PURGE_PHRASE:
        typer.secho("Aborted: phrase did not match. Nothing was deleted.",
                    fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    log = configure_logging(wt.logs_dir, stage="purge-quarantine")
    conn = open_catalog(wt.catalog_path)
    try:
        with record_run(conn, "purge-quarantine", {"files": len(files),
                                                   "bytes": total_bytes}):
            for p in files:
                p.unlink()
            marker.write_text(
                json.dumps(
                    {"purged_at": _utcnow_iso(), "files": len(files),
                     "bytes": total_bytes},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with conn:
                conn.execute(
                    "INSERT INTO decision (ts, stage, subject, rule, detail, actor)"
                    " VALUES (?, 'purge-quarantine', 'quarantine', 'purge.confirmed',"
                    " ?, 'review:user')",
                    (_utcnow_iso(),
                     json.dumps({"files": len(files), "bytes": total_bytes})),
                )
            log.info("quarantine purged",
                     extra={"files": len(files), "bytes": total_bytes})
    finally:
        conn.close()
    typer.echo(f"Purged {len(files)} file(s). Marker written: {marker}")


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@fixtures_app.command("generate")
def fixtures_generate(
    ctx: typer.Context,
    dest: Annotated[Path, typer.Option("--dest", help="Empty destination directory.")],
    seed: Annotated[int, typer.Option("--seed", help="RNG seed for deterministic content.")] = 0,
) -> None:
    """Generate the deterministic synthetic fixture corpus (v0)."""
    try:
        manifest = generate_corpus(dest, seed=seed)
    except (FileExistsError, RuntimeError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    exif_note = "with embedded EXIF" if manifest.exif_written else "WITHOUT EXIF (no exiftool)"
    typer.echo(f"Generated {manifest.count} files under {dest} (seed={seed}, {exif_note}).")
    typer.echo(f"Manifest: {dest / 'MANIFEST.json'}")


if __name__ == "__main__":
    app()
