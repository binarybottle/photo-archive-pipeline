"""Stage 7 — Verify: prove the conservation law and checksums (spec §8).

INV-3, proven on disk rather than trusted from bookkeeping: every instance in
the source inventory must be locatable in exactly one of archive, quarantine,
the intentionally-excluded set, or — once the archive is being curated by hand —
the set the user deliberately removed. Concretely: every instance has exactly one
placement; every archive placement's destination exists and re-hashes to its
recorded post-metadata-write SHA-256; every quarantine placement's destination
exists and re-hashes to the *source* SHA-256 (byte identity is the quarantine
contract); nothing sits in archive/ or quarantine/ that the ledger doesn't
know about. Discrepancies are enumerated exactly and the run exits nonzero on
any (via the CLI). A machine-readable report lands in
``reports/verify_report.json`` alongside the statistics used by
``archive report``.

A ``removed`` placement is one ``maintain reconcile`` adopted after the user
deleted the file in a photo manager, and an ``exported`` one is a file they moved
out of the archive but kept: both are dated and logged, so the file's fate is
still provable, and both are expected to be absent from disk. That manager's own
files
(its trash subtree, ``.DS_Store``, its uuid marker) are not archive contents and
are skipped rather than reported as orphans — see ``[reconcile]`` in config.toml.

A purged quarantine (see ``maintain purge-quarantine``) is recorded by a
marker file; verify then skips quarantine file checks and reports the purge
explicitly instead of failing.

Usage:
    >>> from archive_pipeline.verify import run_verify
    >>> result = run_verify(conn, wt, log)  # doctest: +SKIP
    >>> result.passed  # doctest: +SKIP
    True
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path
from typing import Any

from archive_pipeline.config import Config
from archive_pipeline.hashing import sha256_file
from archive_pipeline.materialize import QUARANTINE_INDEX
from archive_pipeline.workingtree import WorkingTree

VERIFY_REPORT = "verify_report.json"
PURGED_MARKER = ".purged.json"

_VALID_DISPOSITIONS = frozenset(
    {"archive", "quarantine", "excluded", "removed", "exported"}
)

#: Dispositions that deliberately have no file under the working tree.
_FILELESS_DISPOSITIONS = frozenset({"excluded", "removed", "exported"})
_MAX_ENUMERATED = 1000  # cap stored discrepancy details; the count is exact


class VerifyError(Exception):
    """Raised when verification cannot run at all (nothing materialized)."""


@dataclass(frozen=True)
class Discrepancy:
    """One exact violation of the conservation law or a checksum mismatch."""

    kind: str
    subject: str
    detail: str


@dataclass
class VerifyResult:
    """Outcome of one verification run."""

    passed: bool = True
    checksums_only: bool = False
    quarantine_purged: bool = False
    instances_total: int = 0
    placements_total: int = 0
    archive_checked: int = 0
    quarantine_checked: int = 0
    excluded_count: int = 0
    removed_count: int = 0
    exported_count: int = 0
    bytes_archive: int = 0
    bytes_quarantine: int = 0
    discrepancy_count: int = 0
    discrepancies: list[Discrepancy] = field(default_factory=list)

    def add(self, kind: str, subject: str, detail: str) -> None:
        self.discrepancy_count += 1
        self.passed = False
        if len(self.discrepancies) < _MAX_ENUMERATED:
            self.discrepancies.append(Discrepancy(kind, subject, detail))


def run_verify(
    conn: sqlite3.Connection,
    wt: WorkingTree,
    log: Logger,
    checksums_only: bool = False,
    cfg: Config | None = None,
) -> VerifyResult:
    """Run the conservation + checksum verification; write the JSON report.

    ``checksums_only=True`` (the cron-able ``maintain verify-checksums``)
    re-hashes archive and quarantine against the ledger but skips the
    inventory-completeness pass. ``cfg`` supplies the ``[reconcile]`` ignore
    lists; the defaults are used when it is omitted.

    Usage:
        >>> result = run_verify(conn, wt, log)  # doctest: +SKIP
    """
    cfg = cfg or Config()
    result = VerifyResult(checksums_only=checksums_only)
    result.quarantine_purged = (wt.quarantine_dir / PURGED_MARKER).exists()

    placements = conn.execute(
        "SELECT p.instance_id, p.disposition, p.dest_rel_path, p.dest_sha256,"
        " i.source, i.rel_path, i.sha256 AS source_sha, i.size_bytes"
        " FROM placement p JOIN instance i ON i.id = p.instance_id"
    ).fetchall()
    if not placements:
        raise VerifyError(
            "no placements recorded — run `archive materialize --execute` first"
        )
    result.placements_total = len(placements)
    result.instances_total = conn.execute("SELECT COUNT(*) FROM instance").fetchone()[0]

    if not checksums_only:
        for row in conn.execute(
            "SELECT i.source, i.rel_path FROM instance i LEFT JOIN placement p"
            " ON p.instance_id = i.id WHERE p.instance_id IS NULL"
            " ORDER BY i.source, i.rel_path"
        ):
            result.add(
                "missing_placement",
                f"{row['source']}:{row['rel_path']}",
                "instance has no placement row (conservation law violated)",
            )

    # Hash each physical file once even when several instances share it
    # (exact-duplicate losers share one quarantine copy).
    hash_cache: dict[Path, str | None] = {}

    def _hash(path: Path) -> str | None:
        if path not in hash_cache:
            hash_cache[path] = sha256_file(path) if path.is_file() else None
        return hash_cache[path]

    for row in placements:
        subject = f"{row['source']}:{row['rel_path']}"
        disposition = row["disposition"]
        if disposition not in _VALID_DISPOSITIONS:
            result.add("invalid_disposition", subject, f"disposition={disposition!r}")
            continue
        if disposition in _FILELESS_DISPOSITIONS:
            if disposition == "excluded":
                result.excluded_count += 1
            elif disposition == "removed":
                result.removed_count += 1
            else:
                result.exported_count += 1
            continue
        if not row["dest_rel_path"] or not row["dest_sha256"]:
            result.add(
                "missing_dest_record", subject,
                f"{disposition} placement lacks destination path or hash",
            )
            continue
        if disposition == "quarantine":
            if row["dest_sha256"] != row["source_sha"]:
                result.add(
                    "quarantine_not_byte_identical", subject,
                    f"recorded {row['dest_sha256'][:12]} != source"
                    f" {row['source_sha'][:12]}",
                )
            if result.quarantine_purged:
                continue
            path = wt.quarantine_dir / row["dest_rel_path"]
        else:
            path = wt.archive_dir / row["dest_rel_path"]
        actual = _hash(path)
        if actual is None:
            result.add(
                "missing_dest_file", subject, f"{disposition}/{row['dest_rel_path']}"
            )
            continue
        if actual != row["dest_sha256"]:
            result.add(
                "hash_mismatch",
                f"{disposition}/{row['dest_rel_path']}",
                f"on-disk {actual[:12]} != recorded {row['dest_sha256'][:12]}",
            )
            continue
        if disposition == "archive":
            result.archive_checked += 1
            result.bytes_archive += path.stat().st_size
        else:
            result.quarantine_checked += 1
            result.bytes_quarantine += path.stat().st_size

    _orphan_scan(result, wt, cfg, placements)
    _write_report(conn, wt, result)
    log.info(
        "verification " + ("passed" if result.passed else "FAILED"),
        extra={
            "checksums_only": checksums_only,
            "placements": result.placements_total,
            "archive_checked": result.archive_checked,
            "quarantine_checked": result.quarantine_checked,
            "discrepancies": result.discrepancy_count,
            "quarantine_purged": result.quarantine_purged,
        },
    )
    return result


def _orphan_scan(
    result: VerifyResult, wt: WorkingTree, cfg: Config, placements: list[sqlite3.Row]
) -> None:
    """Anything on disk the ledger doesn't account for is a discrepancy."""
    archive_known: set[str] = set()
    quarantine_known = {QUARANTINE_INDEX, PURGED_MARKER}
    for row in placements:
        if not row["dest_rel_path"]:
            continue
        if row["disposition"] == "archive":
            archive_known.add(row["dest_rel_path"])
            # exiftool sidecars for RAW/video are pipeline-written companions.
            archive_known.add(row["dest_rel_path"] + ".xmp")
        elif row["disposition"] == "quarantine":
            quarantine_known.add(row["dest_rel_path"])
    ignore_dirs = set(cfg.reconcile.ignore_dirs)
    ignore_names = set(cfg.reconcile.ignore_names)
    for base, known, kind in (
        (wt.archive_dir, archive_known, "orphan_in_archive"),
        (wt.quarantine_dir, quarantine_known, "orphan_in_quarantine"),
    ):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name in ignore_names:
                continue
            rel = path.relative_to(base).as_posix()
            if rel.split("/", 1)[0] in ignore_dirs:
                continue
            if rel not in known:
                result.add(kind, rel, "file on disk without a placement record")


def _write_report(
    conn: sqlite3.Connection, wt: WorkingTree, result: VerifyResult
) -> None:
    payload = {
        "generated": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "passed": result.passed,
        "checksums_only": result.checksums_only,
        "quarantine_purged": result.quarantine_purged,
        "counts": {
            "instances": result.instances_total,
            "placements": result.placements_total,
            "archive_checked": result.archive_checked,
            "quarantine_checked": result.quarantine_checked,
            "excluded": result.excluded_count,
            "removed": result.removed_count,
            "exported": result.exported_count,
            "bytes_archive": result.bytes_archive,
            "bytes_quarantine": result.bytes_quarantine,
            "discrepancies": result.discrepancy_count,
        },
        "discrepancies": [
            {"kind": d.kind, "subject": d.subject, "detail": d.detail}
            for d in result.discrepancies
        ],
        "stats": collect_stats(conn),
    }
    (wt.reports_dir / VERIFY_REPORT).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def collect_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Statistics for the verify report and ``archive report`` (spec Stage 7).

    Usage:
        >>> stats = collect_stats(conn)  # doctest: +SKIP
        >>> "clusters" in stats
        True
    """

    def _pairs(sql: str) -> dict[str, int]:
        return {str(row[0]): row[1] for row in conn.execute(sql) if row[0] is not None}

    takeout_only_videos = [
        row["rel_path"]
        for row in conn.execute(
            "SELECT rel_path FROM instance WHERE kind = 'video' AND sha256 IN ("
            " SELECT sha256 FROM instance WHERE kind = 'video' GROUP BY sha256"
            " HAVING SUM(CASE WHEN source NOT LIKE 'TAKEOUT:%' THEN 1 ELSE 0 END)"
            " = 0) AND source LIKE 'TAKEOUT:%' ORDER BY rel_path"
        )
    ]
    return {
        "instances": {
            "total": conn.execute("SELECT COUNT(*) FROM instance").fetchone()[0],
            "by_kind": _pairs("SELECT kind, COUNT(*) FROM instance GROUP BY kind"),
            "by_source": _pairs("SELECT source, COUNT(*) FROM instance GROUP BY source"),
        },
        "placements": {
            "by_disposition": _pairs(
                "SELECT disposition, COUNT(*) FROM placement GROUP BY disposition"
            ),
            "source_bytes_by_disposition": _pairs(
                "SELECT p.disposition, SUM(i.size_bytes) FROM placement p"
                " JOIN instance i ON i.id = p.instance_id GROUP BY p.disposition"
            ),
        },
        "dates": {
            "by_status": _pairs(
                "SELECT status, COUNT(*) FROM date_resolution GROUP BY status"
            ),
            "by_source": _pairs(
                "SELECT resolved_source, COUNT(*) FROM date_resolution"
                " GROUP BY resolved_source"
            ),
            "by_precision": _pairs(
                "SELECT resolved_precision, COUNT(*) FROM date_resolution"
                " GROUP BY resolved_precision"
            ),
        },
        "clusters": {
            "by_kind": _pairs("SELECT kind, COUNT(*) FROM cluster GROUP BY kind"),
            "by_status": _pairs("SELECT status, COUNT(*) FROM cluster GROUP BY status"),
            "size_histogram": _pairs(
                "SELECT n, COUNT(*) FROM (SELECT COUNT(*) AS n FROM cluster_member"
                " GROUP BY cluster_id) GROUP BY n ORDER BY n"
            ),
        },
        "decisions": {
            "by_actor": _pairs("SELECT actor, COUNT(*) FROM decision GROUP BY actor"),
            "by_stage": _pairs("SELECT stage, COUNT(*) FROM decision GROUP BY stage"),
        },
        "takeout_only_videos": takeout_only_videos,
        "runs": {
            str(row["stage"]): {"finished": row["finished"], "status": row["status"]}
            for row in conn.execute(
                "SELECT stage, finished, status FROM run WHERE id IN"
                " (SELECT MAX(id) FROM run GROUP BY stage)"
            )
        },
    }
