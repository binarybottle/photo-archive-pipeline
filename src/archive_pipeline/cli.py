"""``archive`` CLI: staged pipeline commands (spec section 5).

M1 implements the scaffold: working-tree init, config loading, catalog schema,
run bookkeeping, structured logging, and the fixture generator. Stage commands
for later milestones are stubs that exit with code 2 and name their milestone;
``ingest`` already enforces the Stage 0 preserve gate.

Usage:
    $ archive --working-tree /archive-project init
    $ archive fixtures generate --dest /tmp/corpus --seed 42
    $ archive --working-tree /archive-project ingest --source LOCAL --root /photos
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from archive_pipeline import __version__
from archive_pipeline.catalog import open_catalog, schema_version
from archive_pipeline.config import Config, ConfigError, load_config
from archive_pipeline.fixtures.generator import generate_corpus
from archive_pipeline.ingest import IngestError, ingest_source
from archive_pipeline.logs import configure_logging
from archive_pipeline.runs import record_run
from archive_pipeline.space import SpaceError
from archive_pipeline.staging import StagingError, stage_takeout_zip
from archive_pipeline.takeout import UNMATCHED_REPORT, TakeoutError, normalize_takeout
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

EXIT_NOT_IMPLEMENTED = 2

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


def _not_implemented(stage: str, milestone: str) -> None:
    """Exit with code 2 for a stage scheduled in a later milestone."""
    typer.secho(
        f"`{stage}` is not implemented yet (arrives in milestone {milestone}).",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(EXIT_NOT_IMPLEMENTED)


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


@app.command("date-resolve")
def date_resolve(ctx: typer.Context) -> None:
    """Resolve each instance's capture date via the trust hierarchy (M4)."""
    _not_implemented("date-resolve", "M4")


@review_app.command("serve")
def review_serve(ctx: typer.Context) -> None:
    """Serve the local review UI on 127.0.0.1 (M5)."""
    _not_implemented("review serve", "M5")


@app.command()
def dedup(ctx: typer.Context) -> None:
    """Cluster duplicates and pick winners with guardrails (M6)."""
    _not_implemented("dedup", "M6")


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
    """Write the archive and quarantine; dry-run by default (INV-4) (M7)."""
    _not_implemented("materialize", "M7")


@app.command()
def verify(ctx: typer.Context) -> None:
    """Prove the conservation law and verify checksums (M8)."""
    _not_implemented("verify", "M8")


@app.command()
def report(ctx: typer.Context) -> None:
    """Human-readable summary of the whole pipeline state (M8)."""
    _not_implemented("report", "M8")


@maintain_app.command("verify-checksums")
def maintain_verify_checksums(ctx: typer.Context) -> None:
    """Periodic checksum re-verification of the archive (M8)."""
    _not_implemented("maintain verify-checksums", "M8")


@maintain_app.command("import")
def maintain_import(
    ctx: typer.Context,
    root: Annotated[Path, typer.Option("--root", help="New photos to import incrementally.")],
) -> None:
    """Incrementally import new photos through the full pipeline (M8)."""
    _not_implemented("maintain import", "M8")


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
