"""Stage 8 — push adopted curation out to XMP sidecars (spec §8, INV-7).

``reconcile`` records what the user's folder moves meant; this step makes them
visible to the photo manager. Corrected dates and folder-derived keywords are
written to each file's ``name.ext.xmp`` sidecar with exiftool — never into the
image — so every archived byte stays identical to what ``verify`` recorded and
the conservation proof survives a curation pass.

Nothing is discarded. Before a date is changed, the date the pipeline originally
wrote is preserved in ``XMP-ArchivePipe:OriginalDate``, and the full before/after
pair is already in the append-only decision log. A file demoted to a coarse
bucket keeps its existing date tags — a bucket asserts "filed coarsely", not
"this timestamp is wrong" — and gains the bucket as a keyword plus a
``DatePrecision`` of ``bucket:<name>`` so the coarseness is explicit.

Each write is idempotent: a keyword is removed and re-added in one command, so
it ends up present exactly once however many times the step runs, and other
keywords (the photo manager's own tags) are left alone. A completed instance is
marked in ``sidecar_task`` so an interrupted run resumes where it stopped
(INV-5).

Usage:
    >>> from archive_pipeline.sidecars import run_apply_sidecars
    >>> summary = run_apply_sidecars(conn, cfg, wt, log, execute=True)  # doctest: +SKIP
    >>> summary.written  # doctest: +SKIP
    11146
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path

from exiftool import ExifToolHelper
from exiftool.exceptions import ExifToolExecuteError

from archive_pipeline.config import Config
from archive_pipeline.materialize import exiftool_config_path, exiftool_date
from archive_pipeline.reconcile import keywords_for, pending_sidecars, sidecar_path
from archive_pipeline.space import require_space
from archive_pipeline.workingtree import WorkingTree

#: Generous per-sidecar estimate for the space preflight (INV-9).
_SIDECAR_BYTES = 8192


class SidecarError(Exception):
    """Raised when sidecar application cannot proceed."""


@dataclass
class SidecarSummary:
    """Outcome of one sidecar-application run."""

    pending: int = 0
    written: int = 0
    skipped_missing_media: int = 0
    failed: int = 0
    dates_written: int = 0
    keywords_written: int = 0
    bytes_estimated: int = 0
    failures: list[str] = field(default_factory=list)
    executed: bool = False


def sidecar_args(
    date: str | None,
    precision: str | None,
    bucket: str | None,
    keywords: list[str],
    original_date: str | None,
) -> list[str]:
    """The exiftool assignments for one curated file's sidecar.

    Each keyword is removed and re-added so repeated runs leave exactly one
    copy without disturbing keywords the pipeline did not put there.

    Usage:
        >>> sidecar_args("2005-01-01", "month", None, ["caves"], None)
        ['-DateTimeOriginal=2005:01:01 00:00:00', '-CreateDate=2005:01:01 00:00:00', \
'-ModifyDate=2005:01:01 00:00:00', '-XMP-ArchivePipe:DatePrecision=month', \
'-XMP-dc:Subject-=caves', '-XMP-dc:Subject+=caves', \
'-XMP-ArchivePipe:DateSource=folder_move']
        >>> sidecar_args(None, None, "undated", [], None)
        ['-XMP-ArchivePipe:DatePrecision=bucket:undated', '-XMP-dc:Subject-=undated', \
'-XMP-dc:Subject+=undated', '-XMP-ArchivePipe:DateSource=folder_move']
    """
    args: list[str] = []
    if date:
        stamp = exiftool_date(date, precision)
        args += [
            f"-DateTimeOriginal={stamp}",
            f"-CreateDate={stamp}",
            f"-ModifyDate={stamp}",
        ]
        if precision:
            args.append(f"-XMP-ArchivePipe:DatePrecision={precision}")
    elif bucket:
        args.append(f"-XMP-ArchivePipe:DatePrecision=bucket:{bucket}")
    if original_date:
        args.append(f"-XMP-ArchivePipe:OriginalDate={original_date}")
    subjects = list(keywords)
    if bucket and bucket not in subjects:
        subjects.append(bucket)
    for keyword in subjects:
        args += [f"-XMP-dc:Subject-={keyword}", f"-XMP-dc:Subject+={keyword}"]
    args.append("-XMP-ArchivePipe:DateSource=folder_move")
    return args


def _original_date(conn: sqlite3.Connection, instance_id: int) -> str | None:
    """The date this file carried before reconcile changed it, if it changed.

    Read back out of the append-only decision log, which is the record of
    truth for what a correction replaced.
    """
    row = conn.execute(
        "SELECT detail FROM decision WHERE subject = ? AND rule IN"
        " ('reconcile.date_corrected', 'reconcile.date_demoted')"
        " ORDER BY id LIMIT 1",
        (f"instance:{instance_id}",),
    ).fetchone()
    if row is None:
        return None
    try:
        previous = json.loads(row[0]).get("from")
    except (TypeError, ValueError):
        return None
    return str(previous) if previous else None


def run_apply_sidecars(
    conn: sqlite3.Connection,
    cfg: Config,
    wt: WorkingTree,
    log: Logger,
    execute: bool = False,
) -> SidecarSummary:
    """Write every pending curated date/keyword set to its XMP sidecar.

    Dry-run by default (INV-4): reports exactly which sidecars would be written
    and the space required, touching nothing.

    Usage:
        >>> summary = run_apply_sidecars(conn, cfg, wt, log)  # doctest: +SKIP
    """
    rows = pending_sidecars(conn)
    summary = SidecarSummary(pending=len(rows), executed=execute)
    summary.bytes_estimated = len(rows) * _SIDECAR_BYTES
    if not rows:
        log.info("no pending sidecar writes")
        return summary
    if not execute:
        for row in rows:
            if "date" in row["reason"]:
                summary.dates_written += 1
            if "keywords" in row["reason"]:
                summary.keywords_written += 1
        log.info(
            "sidecar dry run",
            extra={"pending": summary.pending, "bytes": summary.bytes_estimated},
        )
        return summary

    require_space(wt.root, summary.bytes_estimated, cfg.space.margin_pct)
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    with ExifToolHelper(
        common_args=["-n"], config_file=str(exiftool_config_path())
    ) as et:
        for row in rows:
            instance_id = row["instance_id"]
            media = wt.archive_dir / row["rel"]
            if not media.is_file():
                summary.skipped_missing_media += 1
                log.warning(
                    "curated file is not where the ledger says; skipped",
                    extra={"instance_id": instance_id, "rel": row["rel"]},
                )
                continue
            keywords = keywords_for(conn, instance_id)
            args = sidecar_args(
                row["date"], row["precision"], row["bucket"], keywords,
                _original_date(conn, instance_id),
            )
            try:
                _write(et, args, media, sidecar_path(wt.archive_dir, row["rel"]))
            except SidecarError as exc:
                summary.failed += 1
                if len(summary.failures) < 50:
                    summary.failures.append(str(exc))
                log.error("sidecar write failed", extra={"rel": row["rel"]})
                continue
            summary.written += 1
            if "date" in row["reason"]:
                summary.dates_written += 1
            if keywords:
                summary.keywords_written += 1
            with conn:
                conn.execute(
                    "UPDATE sidecar_task SET written_at = ? WHERE instance_id = ?",
                    (now, instance_id),
                )
    log.info(
        "sidecars applied",
        extra={"written": summary.written, "failed": summary.failed,
               "skipped": summary.skipped_missing_media},
    )
    return summary


def _write(et: ExifToolHelper, args: list[str], media: Path, sidecar: Path) -> None:
    """Create or update one sidecar, leaving the media file untouched.

    Creation is two passes on purpose. In a single ``-o out.xmp src`` call the
    tags copied from the source outrank the assignments on the same command
    line, so a corrected date would silently lose to the wrong one embedded in
    the image; copying first and assigning to the sidecar afterwards makes the
    correction win.
    """
    try:
        if not sidecar.is_file():
            et.execute("-o", str(sidecar), str(media))
        output = et.execute("-overwrite_original", *args, str(sidecar))
    except ExifToolExecuteError as exc:
        raise SidecarError(
            f"exiftool sidecar write failed for {media}: {(exc.stderr or '').strip()}"
        ) from exc
    if not sidecar.is_file():
        raise SidecarError(f"sidecar not written for {media}: {output.strip()}")
