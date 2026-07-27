"""Stage 8 — Reconcile: adopt hand-curation of ``archive/`` back into the ledger.

Once ``verify`` has passed, the archive is handed to a photo manager (digiKam)
and the user starts curating: moving files into better folders, deleting the
ones they do not want. Those edits are legitimate, but they leave the catalog
describing a tree that no longer exists, so ``verify`` drowns in discrepancies
and stops being able to prove anything about later imports.

``reconcile`` closes that loop. It reads the archive, explains every difference
from the ledger, and writes the explanation back into the catalog:

* **Moves** — a recorded destination that vanished, with its (sha-stamped)
  filename present elsewhere, is a move. ``placement.dest_rel_path`` is updated
  and the new folder is *interpreted*: a date-shaped folder is a date
  correction, a coarse bucket (``undated``, ``pre-2000``, ``2004-2006``) is a
  demotion to that bucket, and any other folder component becomes a keyword.
* **Deletions** — a recorded destination that vanished with no counterpart on
  disk becomes ``disposition = 'removed'``, dated, and logged. digiKam's
  ``.dtrash`` records the original path of everything it deleted, so a deletion
  is confirmed rather than guessed whenever the trash is still present.
* **Exports** — a file moved *out* of the archive entirely (the user decides a
  whole topical folder belongs somewhere else) is not a deletion: it still
  exists. Given ``exported_to``, reconcile looks for the vanished files under
  that path and records ``disposition = 'exported'`` with the location it found
  them at, so the ledger can still say where every original ended up.
* **Everything else** — new ``.xmp`` sidecars the photo manager wrote, and its
  own bookkeeping files, are recognized as expected rather than flagged.

Nothing is destroyed and nothing is overwritten: the pre-move date lives on in
the append-only decision log and in the untouched ``instance.exif_dto`` /
``date_resolution.cand_*`` columns, so any correction can be traced or undone.
Reconcile itself never touches a media file — it only writes the catalog.
``maintain apply-sidecars`` is the separate, explicit step that pushes adopted
dates and keywords out to XMP sidecars where digiKam can see them, leaving
image bytes bit-identical (INV-7).

Usage:
    >>> from archive_pipeline.reconcile import run_reconcile
    >>> plan = run_reconcile(conn, cfg, wt, log, execute=False)  # doctest: +SKIP
    >>> plan.moves_total  # doctest: +SKIP
    12432
"""

from __future__ import annotations

import csv
import json
import posixpath
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path

from archive_pipeline.config import Config
from archive_pipeline.dates import folder_candidate
from archive_pipeline.materialize import folder_keyword_candidates
from archive_pipeline.workingtree import WorkingTree

RECONCILE_REPORT = "reconcile_report.json"
DRIFT_REPORT = "reconcile_drift.csv"
TRASH_DIR = ".dtrash"

#: A folder spanning years (``2004-2006``) files coarsely rather than dating.
_YEAR_RANGE_RE = re.compile(r"^(19|20)\d{2}\s*-\s*(19|20)\d{2}$")

#: Sidecar suffix the pipeline (and digiKam, configured to match) writes.
_SIDECAR_SUFFIX = ".xmp"


class ReconcileError(Exception):
    """Raised when the archive cannot be reconciled at all."""


@dataclass(frozen=True)
class FolderIntent:
    """What a file's new archive folder says about it.

    ``date`` is the date the folder implies (None when the folder is not
    date-shaped), ``bucket`` a coarse destination that deliberately carries no
    date, and ``keywords`` the topical path components.
    """

    date: str | None = None
    precision: str | None = None
    bucket: str | None = None
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Move:
    """One archived file the user filed somewhere else."""

    instance_id: int
    old_rel: str
    new_rel: str
    intent: FolderIntent
    date_action: str  # keep | correct | demote
    old_date: str | None
    old_precision: str | None


@dataclass(frozen=True)
class Removal:
    """One archived file that is no longer where the ledger placed it.

    ``trashed_as`` names the photo manager's trash entry when the trash still
    records the deletion. Without it the file's disappearance is unexplained —
    which is not the same thing as deliberate — so it is only adopted when the
    user says so with ``--adopt-unaccounted``.
    """

    instance_id: int
    rel: str
    trashed_as: str | None


@dataclass(frozen=True)
class Export:
    """One archived file the user moved out of the archive but kept."""

    instance_id: int
    rel: str
    found_at: str  # absolute path it was located at


@dataclass
class ReconcilePlan:
    """Every difference between the ledger and the archive, explained."""

    moves: list[Move] = field(default_factory=list)
    removals: list[Removal] = field(default_factory=list)
    exports: list[Export] = field(default_factory=list)
    restorations: list[Removal] = field(default_factory=list)
    unaccounted: list[Removal] = field(default_factory=list)
    new_sidecars: list[str] = field(default_factory=list)
    unknown_media: list[str] = field(default_factory=list)
    ignored: int = 0
    trashed_sidecars: int = 0
    executed: bool = False

    @property
    def moves_total(self) -> int:
        return len(self.moves)

    @property
    def date_corrections(self) -> list[Move]:
        return [m for m in self.moves if m.date_action == "correct"]

    @property
    def demotions(self) -> list[Move]:
        return [m for m in self.moves if m.date_action == "demote"]

    @property
    def keyword_moves(self) -> list[Move]:
        return [m for m in self.moves if m.intent.keywords]

    @property
    def clean(self) -> bool:
        """True when nothing at all needs adopting."""
        return not (
            self.moves
            or self.removals
            or self.exports
            or self.restorations
            or self.unaccounted
            or self.unknown_media
        )


# --- Reading the archive --------------------------------------------------------


def interpret_folder(new_rel: str, cfg: Config) -> FolderIntent:
    """Read a file's archive-relative path as a curation instruction.

    A date-shaped folder dates the file; a year-range or configured bucket
    folder files it coarsely; anything else is topical and becomes a keyword.
    Both can apply at once — ``caves/2006`` means "keyword caves, year 2006".

    Usage:
        >>> from archive_pipeline.config import Config
        >>> cfg = Config()
        >>> interpret_folder("2005/2005-01/x.jpg", cfg)
        FolderIntent(date='2005-01-01', precision='month', bucket=None, keywords=())
        >>> interpret_folder("caves/2006/x.jpg", cfg).keywords
        ('caves',)
        >>> interpret_folder("2004-2006/x.jpg", cfg).bucket
        '2004-2006'
        >>> interpret_folder("undated/x.jpg", cfg).bucket
        'undated'
    """
    buckets = {cfg.policy.undated_placement, *cfg.reconcile.bucket_dirs}
    bucket: str | None = None
    kept: list[str] = []
    for component in posixpath.dirname(new_rel).split("/"):
        if component in buckets or _YEAR_RANGE_RE.match(component):
            # A bucket is deliberately coarse; reading a date out of a name like
            # "pre-2000" would invent precision the user did not intend.
            bucket = component
        elif component:
            kept.append(component)
    remainder = posixpath.join(*kept, posixpath.basename(new_rel)) if kept else new_rel
    found = folder_candidate(remainder, cfg.dates.folder_patterns) if kept else None
    date, precision = found if found else (None, None)
    keywords = (
        tuple(folder_keyword_candidates(remainder, cfg.dates.folder_patterns))
        if kept
        else ()
    )
    return FolderIntent(date=date, precision=precision, bucket=bucket, keywords=keywords)


def scan_archive(wt: WorkingTree, cfg: Config) -> tuple[dict[str, list[str]], set[str], int]:
    """Index the archive on disk: basename -> paths, all paths, ignored count.

    The photo manager's own bookkeeping (``.DS_Store``, ``digikam.uuid``) and
    its trash subtree are skipped: they are not pipeline files and must not read
    as archive contents.

    Usage:
        >>> by_name, paths, ignored = scan_archive(wt, cfg)  # doctest: +SKIP
    """
    by_name: dict[str, list[str]] = defaultdict(list)
    paths: set[str] = set()
    ignored = 0
    ignore_dirs = {TRASH_DIR, *cfg.reconcile.ignore_dirs}
    for path in wt.archive_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(wt.archive_dir).as_posix()
        head = rel.split("/", 1)[0]
        if head in ignore_dirs or path.name in cfg.reconcile.ignore_names:
            ignored += 1
            continue
        paths.add(rel)
        by_name[path.name].append(rel)
    return by_name, paths, ignored


def read_trash(wt: WorkingTree) -> dict[str, str]:
    """Map archive-relative original path -> trash entry, from digiKam's trash.

    digiKam writes one ``<name>.dtrashinfo`` JSON per deleted file recording the
    absolute path it came from, which turns a guessed deletion into a confirmed
    one. An absent or unreadable trash simply yields no confirmations.

    Usage:
        >>> read_trash(wt)  # doctest: +SKIP
        {'2013/2013-10/x__ab12cd34.jpg': 'x__ab12cd34-a398c956'}
    """
    info_dir = wt.archive_dir / TRASH_DIR / "info"
    if not info_dir.is_dir():
        return {}
    # archive/ is commonly a symlink to the real storage; digiKam records the
    # resolved path, so compare against the resolved root.
    root = str(wt.archive_dir.resolve())
    out: dict[str, str] = {}
    for info in info_dir.glob("*.dtrashinfo"):
        try:
            payload = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        original = str(payload.get("path", ""))
        if original.startswith(root + "/"):
            out[original[len(root) + 1:]] = info.stem
    return out


# --- Planning -------------------------------------------------------------------


def find_exported(
    unaccounted: list[Removal], exported_to: Path, wt: WorkingTree
) -> tuple[list[Export], list[Removal]]:
    """Split vanished files into those found under ``exported_to`` and the rest.

    Archive filenames carry an ``__<sha8>`` stamp, so a basename match outside
    the archive identifies a file that was moved out rather than destroyed. The
    archive itself is excluded from the search so a path that merely contains it
    cannot match a file against itself.

    Usage:
        >>> gone = plan.unaccounted
        >>> found, missing = find_exported(gone, Path("/photos"), wt)  # doctest: +SKIP
    """
    archive_root = wt.archive_dir.resolve()
    by_name: dict[str, str] = {}
    for path in exported_to.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved == archive_root or archive_root in resolved.parents:
            continue
        by_name.setdefault(path.name, str(resolved))
    exports: list[Export] = []
    still_missing: list[Removal] = []
    for missing in unaccounted:
        found_at = by_name.get(posixpath.basename(missing.rel))
        if found_at:
            exports.append(Export(missing.instance_id, missing.rel, found_at))
        else:
            still_missing.append(missing)
    return exports, still_missing


def plan_reconcile(conn: sqlite3.Connection, cfg: Config, wt: WorkingTree) -> ReconcilePlan:
    """Explain every difference between the archive placements and the disk.

    Usage:
        >>> plan = plan_reconcile(conn, cfg, wt)  # doctest: +SKIP
        >>> plan.clean  # doctest: +SKIP
        True
    """
    if not wt.archive_dir.is_dir():
        raise ReconcileError(f"no archive directory at {wt.archive_dir}")
    placed = conn.execute(
        "SELECT p.instance_id, p.dest_rel_path AS rel, d.resolved_date AS date,"
        " d.resolved_precision AS precision"
        " FROM placement p LEFT JOIN date_resolution d ON d.instance_id = p.instance_id"
        " WHERE p.disposition = 'archive' AND p.dest_rel_path IS NOT NULL"
    ).fetchall()
    if not placed:
        raise ReconcileError(
            "no archive placements recorded — run `archive materialize --execute` first"
        )

    by_name, on_disk, ignored = scan_archive(wt, cfg)
    trash = read_trash(wt)
    recorded = {row["rel"] for row in placed}
    plan = ReconcilePlan(ignored=ignored)

    # A previously-removed file that is back where it was (restored from the
    # photo manager's trash) rejoins the archive rather than reading as an
    # unknown intruder.
    for row in conn.execute(
        "SELECT instance_id, dest_rel_path AS rel FROM placement"
        " WHERE disposition IN ('removed', 'exported') AND dest_rel_path IS NOT NULL"
    ):
        if row["rel"] in on_disk:
            plan.restorations.append(Removal(row["instance_id"], row["rel"], None))
            recorded.add(row["rel"])
    known = recorded | {rel + _SIDECAR_SUFFIX for rel in recorded}

    # Candidate destinations for a move: on disk, and not itself a recorded
    # placement (that file is accounted for where it stands).
    for row in placed:
        rel = row["rel"]
        if rel in on_disk:
            continue
        candidates = [c for c in by_name.get(posixpath.basename(rel), []) if c not in recorded]
        if candidates:
            new_rel = sorted(candidates)[0]
            intent = interpret_folder(new_rel, cfg)
            action, old_date, old_precision = _date_action(row, intent)
            plan.moves.append(
                Move(
                    instance_id=row["instance_id"],
                    old_rel=rel,
                    new_rel=new_rel,
                    intent=intent,
                    date_action=action,
                    old_date=old_date,
                    old_precision=old_precision,
                )
            )
        elif rel in trash:
            plan.removals.append(Removal(row["instance_id"], rel, trash[rel]))
        else:
            plan.unaccounted.append(Removal(row["instance_id"], rel, None))

    moved_dests = {m.new_rel for m in plan.moves}
    for rel in sorted(on_disk - known - moved_dests):
        if rel.endswith(_SIDECAR_SUFFIX):
            plan.new_sidecars.append(rel)
        else:
            plan.unknown_media.append(rel)
    plan.trashed_sidecars = sum(1 for rel in trash if rel.endswith(_SIDECAR_SUFFIX))
    plan.unaccounted.sort(key=lambda r: r.rel)
    return plan


def _date_action(
    row: sqlite3.Row, intent: FolderIntent
) -> tuple[str, str | None, str | None]:
    """Decide what a move means for the file's date.

    A folder date that *contains* the resolved date confirms it and changes
    nothing — a photo dated 2006-03-14 filed under ``caves/2006`` keeps its
    precise timestamp, because coarsening it would throw information away. Only
    a folder that contradicts the resolved date (or a bucket that deliberately
    carries none) changes anything.
    """
    old_date, old_precision = row["date"], row["precision"]
    if intent.date is None:
        if intent.bucket and old_date is None:
            return "keep", old_date, old_precision
        if intent.bucket:
            return "demote", old_date, old_precision
        return "keep", old_date, old_precision
    if old_date is None:
        return "correct", old_date, old_precision
    if _contains(intent.date, intent.precision, old_date):
        return "keep", old_date, old_precision
    return "correct", old_date, old_precision


def _contains(folder_date: str, precision: str | None, resolved: str) -> bool:
    """True when a folder's date range brackets an already-resolved date.

    Usage:
        >>> _contains("2006-01-01", "year", "2006-03-14T10:00:00")
        True
        >>> _contains("2006-03-01", "month", "2006-04-02")
        False
    """
    if precision == "year":
        return resolved[:4] == folder_date[:4]
    if precision == "month":
        return resolved[:7] == folder_date[:7]
    return resolved[:10] == folder_date[:10]


# --- Applying -------------------------------------------------------------------


def apply_reconcile(
    conn: sqlite3.Connection,
    plan: ReconcilePlan,
    log: Logger,
) -> None:
    """Write an adopted plan into the catalog, appending a decision per change.

    Usage:
        >>> apply_reconcile(conn, plan, log)  # doctest: +SKIP
    """
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    with conn:
        for move in plan.moves:
            conn.execute(
                "UPDATE placement SET dest_rel_path = ? WHERE instance_id = ?",
                (move.new_rel, move.instance_id),
            )
            _decide(
                conn, now, f"instance:{move.instance_id}", "reconcile.moved",
                {"from": move.old_rel, "to": move.new_rel,
                 "date_action": move.date_action,
                 "keywords": list(move.intent.keywords),
                 "bucket": move.intent.bucket},
            )
            if move.date_action == "correct":
                _correct_date(conn, now, move)
            elif move.date_action == "demote":
                _demote_date(conn, now, move)
            for keyword in move.intent.keywords:
                conn.execute(
                    "INSERT OR IGNORE INTO manual_keyword"
                    " (instance_id, keyword, origin, added) VALUES (?, ?, ?, ?)",
                    (move.instance_id, keyword, f"folder_move:{move.new_rel}", now),
                )
            if move.intent.keywords or move.date_action != "keep":
                _queue_sidecar(conn, move, now)
        for removal in plan.removals:
            conn.execute(
                "UPDATE placement SET disposition = 'removed', removed_at = ?"
                " WHERE instance_id = ?",
                (now, removal.instance_id),
            )
            conn.execute("DELETE FROM sidecar_task WHERE instance_id = ?",
                         (removal.instance_id,))
            _decide(
                conn, now, f"instance:{removal.instance_id}", "reconcile.removed",
                {"was": removal.rel, "trashed_as": removal.trashed_as,
                 "confirmed_by": "trash" if removal.trashed_as else "absence"},
            )
        for export in plan.exports:
            conn.execute(
                "UPDATE placement SET disposition = 'exported', removed_at = ?"
                " WHERE instance_id = ?",
                (now, export.instance_id),
            )
            conn.execute("DELETE FROM sidecar_task WHERE instance_id = ?",
                         (export.instance_id,))
            _decide(
                conn, now, f"instance:{export.instance_id}", "reconcile.exported",
                {"was": export.rel, "found_at": export.found_at},
            )
        for restored in plan.restorations:
            conn.execute(
                "UPDATE placement SET disposition = 'archive', removed_at = NULL"
                " WHERE instance_id = ?",
                (restored.instance_id,),
            )
            _decide(
                conn, now, f"instance:{restored.instance_id}", "reconcile.restored",
                {"at": restored.rel},
            )
    plan.executed = True
    log.info(
        "reconcile applied",
        extra={
            "moves": len(plan.moves),
            "date_corrections": len(plan.date_corrections),
            "demotions": len(plan.demotions),
            "removals": len(plan.removals),
            "exports": len(plan.exports),
            "unaccounted": len(plan.unaccounted),
        },
    )


def _correct_date(conn: sqlite3.Connection, now: str, move: Move) -> None:
    assert move.intent.date is not None
    conn.execute(
        "INSERT INTO date_resolution (instance_id, resolved_date, resolved_precision,"
        " resolved_source, status, confidence, bucket) VALUES (?, ?, ?, 'folder_move',"
        " 'reviewed', 1.0, NULL) ON CONFLICT(instance_id) DO UPDATE SET"
        " resolved_date = excluded.resolved_date,"
        " resolved_precision = excluded.resolved_precision,"
        " resolved_source = 'folder_move', status = 'reviewed', bucket = NULL",
        (move.instance_id, move.intent.date, move.intent.precision),
    )
    _decide(
        conn, now, f"instance:{move.instance_id}", "reconcile.date_corrected",
        {"from": move.old_date, "from_precision": move.old_precision,
         "to": move.intent.date, "to_precision": move.intent.precision,
         "folder": posixpath.dirname(move.new_rel)},
    )


def _demote_date(conn: sqlite3.Connection, now: str, move: Move) -> None:
    conn.execute(
        "INSERT INTO date_resolution (instance_id, resolved_date, resolved_precision,"
        " resolved_source, status, confidence, bucket) VALUES (?, NULL, NULL,"
        " 'folder_move', 'reviewed', 1.0, ?) ON CONFLICT(instance_id) DO UPDATE SET"
        " resolved_date = NULL, resolved_precision = NULL,"
        " resolved_source = 'folder_move', status = 'reviewed', bucket = excluded.bucket",
        (move.instance_id, move.intent.bucket),
    )
    _decide(
        conn, now, f"instance:{move.instance_id}", "reconcile.date_demoted",
        {"from": move.old_date, "from_precision": move.old_precision,
         "bucket": move.intent.bucket},
    )


def _queue_sidecar(conn: sqlite3.Connection, move: Move, now: str) -> None:
    reasons = []
    if move.date_action in ("correct", "demote"):
        reasons.append("date")
    if move.intent.keywords:
        reasons.append("keywords")
    conn.execute(
        "INSERT INTO sidecar_task (instance_id, reason, queued, written_at)"
        " VALUES (?, ?, ?, NULL) ON CONFLICT(instance_id) DO UPDATE SET"
        " reason = excluded.reason, queued = excluded.queued, written_at = NULL",
        (move.instance_id, "+".join(reasons), now),
    )


def _decide(
    conn: sqlite3.Connection, now: str, subject: str, rule: str, detail: dict[str, object]
) -> None:
    conn.execute(
        "INSERT INTO decision (ts, stage, subject, rule, detail, actor)"
        " VALUES (?, 'reconcile', ?, ?, ?, 'review:user')",
        (now, subject, rule, json.dumps(detail, sort_keys=True)),
    )


# --- Entry point and reporting --------------------------------------------------


def run_reconcile(
    conn: sqlite3.Connection,
    cfg: Config,
    wt: WorkingTree,
    log: Logger,
    execute: bool = False,
    adopt_unaccounted: bool = False,
    exported_to: Path | None = None,
) -> ReconcilePlan:
    """Plan (and with ``execute``, adopt) the archive's hand-curation.

    Dry-run by default (INV-4): the plan and its reports are produced without
    touching the catalog.

    ``exported_to`` is searched for files that vanished from the archive, and
    any found there are recorded as exported rather than deleted — the honest
    answer when a whole folder was moved out of the archive on purpose. It is
    applied before ``adopt_unaccounted``, so a file that still exists is never
    written down as destroyed.

    ``adopt_unaccounted`` treats files that vanished with no trash record as
    deliberate deletions too. That is the right call after the photo manager's
    trash has been emptied — the evidence is gone, not the intent — but it is
    off by default because an unexplained disappearance is exactly what the
    conservation law exists to catch.

    Usage:
        >>> plan = run_reconcile(conn, cfg, wt, log, execute=True)  # doctest: +SKIP
    """
    plan = plan_reconcile(conn, cfg, wt)
    if exported_to is not None and plan.unaccounted:
        plan.exports, plan.unaccounted = find_exported(
            plan.unaccounted, exported_to, wt
        )
    if adopt_unaccounted:
        plan.removals.extend(plan.unaccounted)
        plan.unaccounted = []
    if execute:
        apply_reconcile(conn, plan, log)
    else:
        log.info(
            "reconcile dry run",
            extra={"moves": len(plan.moves), "removals": len(plan.removals),
                   "unaccounted": len(plan.unaccounted)},
        )
    write_reports(wt, plan)
    return plan


def write_reports(wt: WorkingTree, plan: ReconcilePlan) -> None:
    """Write the machine-readable summary and the per-file drift CSV."""
    payload = {
        "generated": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "executed": plan.executed,
        "counts": {
            "moves": len(plan.moves),
            "date_corrections": len(plan.date_corrections),
            "date_demotions": len(plan.demotions),
            "keyword_moves": len(plan.keyword_moves),
            "removals": len(plan.removals),
            "exports": len(plan.exports),
            "restorations": len(plan.restorations),
            "unaccounted": len(plan.unaccounted),
            "new_sidecars": len(plan.new_sidecars),
            "unknown_media": len(plan.unknown_media),
            "ignored_files": plan.ignored,
            "trashed_sidecars": plan.trashed_sidecars,
        },
        "keywords": _keyword_histogram(plan),
        "unaccounted": [r.rel for r in plan.unaccounted[:1000]],
        "unknown_media": plan.unknown_media[:1000],
    }
    (wt.reports_dir / RECONCILE_REPORT).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    with (wt.reports_dir / DRIFT_REPORT).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["change", "ledger_path", "now_at", "date_action", "old_date",
             "new_date", "precision", "bucket", "keywords"]
        )
        for move in plan.moves:
            writer.writerow(
                ["moved", move.old_rel, move.new_rel, move.date_action,
                 move.old_date or "", move.intent.date or "",
                 move.intent.precision or "", move.intent.bucket or "",
                 ";".join(move.intent.keywords)]
            )
        for removal in plan.removals:
            writer.writerow(
                ["removed", removal.rel, removal.trashed_as or "", "", "", "", "", "", ""]
            )
        for export in plan.exports:
            writer.writerow(
                ["exported", export.rel, export.found_at, "", "", "", "", "", ""]
            )
        for restored in plan.restorations:
            writer.writerow(["restored", restored.rel, restored.rel, "", "", "", "", "", ""])
        for missing in plan.unaccounted:
            writer.writerow(["unaccounted", missing.rel, "", "", "", "", "", "", ""])
        for rel in plan.unknown_media:
            writer.writerow(["unknown_media", "", rel, "", "", "", "", "", ""])


def _keyword_histogram(plan: ReconcilePlan) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for move in plan.moves:
        for keyword in move.intent.keywords:
            counts[keyword] += 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def pending_sidecars(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Instances whose adopted date/keywords have not reached a sidecar yet.

    Usage:
        >>> rows = pending_sidecars(conn)  # doctest: +SKIP
    """
    return conn.execute(
        "SELECT t.instance_id, t.reason, p.dest_rel_path AS rel,"
        " d.resolved_date AS date, d.resolved_precision AS precision,"
        " d.bucket AS bucket"
        " FROM sidecar_task t JOIN placement p ON p.instance_id = t.instance_id"
        " LEFT JOIN date_resolution d ON d.instance_id = t.instance_id"
        " WHERE t.written_at IS NULL AND p.disposition = 'archive'"
        " AND p.dest_rel_path IS NOT NULL ORDER BY p.dest_rel_path"
    ).fetchall()


def keywords_for(conn: sqlite3.Connection, instance_id: int) -> list[str]:
    """Keywords adopted from folder moves for one instance."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT keyword FROM manual_keyword WHERE instance_id = ?"
            " ORDER BY keyword",
            (instance_id,),
        )
    ]


def sidecar_path(archive_dir: Path, rel: str) -> Path:
    """The XMP sidecar path for an archived file (``name.ext.xmp``)."""
    media = archive_dir / rel
    return media.with_suffix(media.suffix + _SIDECAR_SUFFIX)
