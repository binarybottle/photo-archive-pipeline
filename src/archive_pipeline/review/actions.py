"""Review actions: catalog writes driven by the human reviewer (spec Stage 4).

Every action updates the catalog and appends to the ``decision`` table with
actor ``review:user`` (INV-6: overrides append, never overwrite history).
This module is HTTP-free so actions are unit-testable without the web app.

Usage:
    >>> from archive_pipeline.review.actions import accept_candidate
    >>> accept_candidate(conn, instance_id=7, candidate="folder")  # doctest: +SKIP
    '1998-01-01'
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

PRECISIONS = ("second", "day", "month", "year")

#: candidate name -> (date_resolution column, resolved_source label)
_CANDIDATES = {
    "exif": ("cand_exif", "exif"),
    "folder": ("cand_folder", "folder"),
    "takeout": ("cand_takeout", "takeout_json"),
    "filename": ("cand_filename", "filename"),
}

_MANUAL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2})?$")


class ReviewError(Exception):
    """Raised for invalid review actions (bad dates, missing candidates...)."""


def _decision(
    conn: sqlite3.Connection, subject: str, rule: str, detail: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO decision (ts, stage, subject, rule, detail, actor)"
        " VALUES (?, 'review', ?, ?, ?, 'review:user')",
        (
            datetime.now(tz=UTC).isoformat(timespec="seconds"),
            subject,
            rule,
            json.dumps(detail, sort_keys=True),
        ),
    )


def _resolution_row(conn: sqlite3.Connection, instance_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM date_resolution WHERE instance_id = ?", (instance_id,)
    ).fetchone()
    if row is None:
        raise ReviewError(f"no date resolution for instance {instance_id}")
    return row


def _apply_resolution(
    conn: sqlite3.Connection,
    instance_id: int,
    date: str,
    precision: str,
    source: str,
    rule: str,
    detail: dict[str, Any],
) -> None:
    with conn:
        conn.execute(
            "UPDATE date_resolution SET resolved_date = ?, resolved_precision = ?,"
            " resolved_source = ?, status = 'reviewed', confidence = 1.0"
            " WHERE instance_id = ?",
            (date, precision, source, instance_id),
        )
        _decision(
            conn, f"instance:{instance_id}", rule,
            detail | {"resolved": date, "precision": precision, "source": source},
        )


def resolve_manual(
    conn: sqlite3.Connection, instance_id: int, date: str, precision: str
) -> None:
    """Set a manually entered date with an explicit precision.

    Usage:
        >>> resolve_manual(conn, 7, "2001-07-01", "month")  # doctest: +SKIP
    """
    if precision not in PRECISIONS:
        raise ReviewError(f"invalid precision: {precision}")
    if not _MANUAL_DATE_RE.match(date):
        raise ReviewError(f"invalid date (want YYYY-MM-DD[THH:MM:SS]): {date}")
    try:
        datetime.fromisoformat(date)
    except ValueError as exc:
        raise ReviewError(f"invalid date: {date}") from exc
    if precision == "second" and "T" not in date:
        raise ReviewError("second precision requires a time component")
    _resolution_row(conn, instance_id)
    _apply_resolution(
        conn, instance_id, date, precision, "review", "review.date.manual", {}
    )


def accept_candidate(conn: sqlite3.Connection, instance_id: int, candidate: str) -> str:
    """Accept one of the stored candidates; return the applied date.

    Usage:
        >>> accept_candidate(conn, 7, "exif")  # doctest: +SKIP
        '1998-07-12T14:33:05'
    """
    if candidate not in _CANDIDATES:
        raise ReviewError(f"unknown candidate: {candidate}")
    column, source = _CANDIDATES[candidate]
    row = _resolution_row(conn, instance_id)
    date = row[column]
    if not date:
        raise ReviewError(f"instance {instance_id} has no {candidate} candidate")
    if candidate == "folder":
        precision = row["folder_precision"] or "day"
    else:
        precision = "second" if "T" in date else "day"
    _apply_resolution(
        conn, instance_id, date, precision, source,
        f"review.date.accept_{candidate}", {"candidate": candidate},
    )
    return str(date)


def batch_apply(
    conn: sqlite3.Connection, source: str, dir_path: str, action: str
) -> int:
    """Apply one candidate to every *conflict* item in one folder; return count.

    ``action`` is ``folder`` ("apply the folder date"), ``exif`` ("trust EXIF
    for this whole camera roll"), or ``filename`` ("use the date parsed from
    each filename"). Items lacking that candidate are skipped.

    Usage:
        >>> batch_apply(conn, "LOCAL", "scans", "exif")  # doctest: +SKIP
        3
    """
    if action not in ("folder", "exif", "filename"):
        raise ReviewError(f"unknown batch action: {action}")
    column, source_label = _CANDIDATES[action]
    like = f"{dir_path}/%" if dir_path else "%"
    rows = conn.execute(
        f"SELECT d.instance_id, d.{column} AS cand, d.folder_precision"
        " FROM date_resolution d JOIN instance i ON i.id = d.instance_id"
        " WHERE d.status = 'conflict' AND i.source = ? AND i.rel_path LIKE ?"
        " AND i.rel_path NOT LIKE ?",
        (source, like, f"{like}/%"),
    ).fetchall()
    applied = 0
    for row in rows:
        if not row["cand"]:
            continue
        if action == "folder":
            precision = row["folder_precision"] or "day"
        else:
            precision = "second" if "T" in row["cand"] else "day"
        _apply_resolution(
            conn, row["instance_id"], row["cand"], precision, source_label,
            f"review.date.batch_{action}", {"dir": dir_path, "source": source},
        )
        applied += 1
    return applied


_PARTIAL_DATE = (
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "day", "{0}"),
    (re.compile(r"^\d{4}-\d{2}$"), "month", "{0}-01"),
    (re.compile(r"^\d{4}$"), "year", "{0}-01-01"),
)


def batch_manual(
    conn: sqlite3.Connection, source: str, dir_path: str, entered: str
) -> int:
    """Assign a manually typed date to every conflict item in one folder.

    Accepts a partial date and infers its precision: ``YYYY`` (year),
    ``YYYY-MM`` (month), or ``YYYY-MM-DD`` (day). A year range ``YYYY-YYYY`` is
    filed into that range bucket instead (``archive/YYYY-YYYY/``). Returns the
    count updated.

    Usage:
        >>> batch_manual(conn, "LOCAL", "Ellora/2004", "2004")  # doctest: +SKIP
        116
    """
    entered = entered.strip()
    if _YEAR_RANGE.match(entered):
        return batch_bucket(conn, source, dir_path, entered)
    date = precision = None
    for pattern, prec, template in _PARTIAL_DATE:
        if pattern.match(entered):
            date, precision = template.format(entered), prec
            break
    if date is None or precision is None:
        raise ReviewError(
            f"enter a year, year-month, or year-month-day (got {entered!r})"
        )
    try:
        datetime.fromisoformat(date)
    except ValueError as exc:
        raise ReviewError(f"not a real date: {entered}") from exc
    like = f"{dir_path}/%" if dir_path else "%"
    rows = conn.execute(
        "SELECT d.instance_id FROM date_resolution d JOIN instance i"
        " ON i.id = d.instance_id WHERE d.status = 'conflict' AND i.source = ?"
        " AND i.rel_path LIKE ? AND i.rel_path NOT LIKE ?",
        (source, like, f"{like}/%"),
    ).fetchall()
    for row in rows:
        _apply_resolution(
            conn, row["instance_id"], date, precision, "review",
            "review.date.batch_manual", {"dir": dir_path, "entered": entered},
        )
    return len(rows)


def batch_skip(
    conn: sqlite3.Connection, source: str, dir_path: str, skipped: bool
) -> int:
    """Hide (``skipped=True``) or restore every conflict item in one folder.

    Skipping only affects the queue view — the items stay unresolved conflicts.
    Returns the number of rows changed.

    Usage:
        >>> batch_skip(conn, "LOCAL", "Ellora/_movies", True)  # doctest: +SKIP
        20
    """
    like = f"{dir_path}/%" if dir_path else "%"
    with conn:
        cur = conn.execute(
            "UPDATE date_resolution SET skipped = ? WHERE status = 'conflict'"
            " AND instance_id IN (SELECT id FROM instance WHERE source = ?"
            " AND rel_path LIKE ? AND rel_path NOT LIKE ?)",
            (1 if skipped else 0, source, like, f"{like}/%"),
        )
    return cur.rowcount


PRE_2000_BUCKET = "pre-2000"
_YEAR_RANGE = re.compile(r"^(?P<a>(19|20)\d{2})-(?P<b>(19|20)\d{2})$")


def normalize_bucket(bucket: str) -> str:
    """Validate a coarse-bucket name, returning it, or raise ``ReviewError``.

    Allowed: ``pre-2000`` or a year range ``YYYY-YYYY`` (start <= end). Both are
    safe folder names filed under ``archive/<bucket>/``.

    Usage:
        >>> normalize_bucket("2004-2009")
        '2004-2009'
    """
    bucket = bucket.strip()
    if bucket == PRE_2000_BUCKET:
        return bucket
    match = _YEAR_RANGE.match(bucket)
    if not match:
        raise ReviewError(f"not a bucket (want pre-2000 or YYYY-YYYY): {bucket!r}")
    if int(match["a"]) > int(match["b"]):
        raise ReviewError(f"range start after end: {bucket}")
    return bucket


def batch_bucket(
    conn: sqlite3.Connection, source: str, dir_path: str, bucket: str
) -> int:
    """File every conflict item in one folder into a coarse archive bucket.

    Used for photos the user knows only loosely — ``pre-2000`` for old,
    undateable images, or a year range ``YYYY-YYYY`` for a span. The bucket
    becomes an ``archive/<bucket>/`` folder at materialize; the row is marked
    reviewed so re-resolution leaves it alone.

    Usage:
        >>> batch_bucket(conn, "LOCAL", "2004-2009", "2004-2009")  # doctest: +SKIP
        42
    """
    bucket = normalize_bucket(bucket)
    like = f"{dir_path}/%" if dir_path else "%"
    rows = conn.execute(
        "SELECT d.instance_id FROM date_resolution d JOIN instance i"
        " ON i.id = d.instance_id WHERE d.status = 'conflict' AND i.source = ?"
        " AND i.rel_path LIKE ? AND i.rel_path NOT LIKE ?",
        (source, like, f"{like}/%"),
    ).fetchall()
    with conn:
        for row in rows:
            conn.execute(
                "UPDATE date_resolution SET resolved_date = NULL,"
                " resolved_precision = NULL, resolved_source = 'review',"
                " bucket = ?, status = 'reviewed', confidence = 1.0"
                " WHERE instance_id = ?",
                (bucket, row["instance_id"]),
            )
            _decision(
                conn, f"instance:{row['instance_id']}", "review.date.bucket",
                {"bucket": bucket, "dir": dir_path},
            )
    return len(rows)


def set_sequence_hint(
    conn: sqlite3.Connection, instance_id: int, hint: int | None
) -> None:
    """Set (or clear) the intra-day ordering hint for scanned batches.

    Usage:
        >>> set_sequence_hint(conn, 7, 3)  # doctest: +SKIP
    """
    _resolution_row(conn, instance_id)
    with conn:
        conn.execute(
            "UPDATE date_resolution SET sequence_hint = ? WHERE instance_id = ?",
            (hint, instance_id),
        )
        _decision(
            conn, f"instance:{instance_id}", "review.sequence_hint", {"hint": hint}
        )


def _cluster_row(conn: sqlite3.Connection, cluster_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM cluster WHERE id = ?", (cluster_id,)
    ).fetchone()
    if row is None:
        raise ReviewError(f"no cluster {cluster_id}")
    return row


def cluster_accept(conn: sqlite3.Connection, cluster_id: int) -> None:
    """Accept the automatic winner choice for a cluster.

    Usage:
        >>> cluster_accept(conn, 3)  # doctest: +SKIP
    """
    row = _cluster_row(conn, cluster_id)
    with conn:
        conn.execute(
            "UPDATE cluster SET status = 'reviewed' WHERE id = ?", (cluster_id,)
        )
        _decision(
            conn, f"cluster:{cluster_id}", "review.cluster.accept",
            {"winner": row["winner_instance_id"]},
        )


def cluster_swap_winner(
    conn: sqlite3.Connection, cluster_id: int, instance_id: int
) -> None:
    """Make ``instance_id`` the cluster's winner (it must be a member).

    Usage:
        >>> cluster_swap_winner(conn, 3, 42)  # doctest: +SKIP
    """
    previous = _cluster_row(conn, cluster_id)["winner_instance_id"]
    member = conn.execute(
        "SELECT role FROM cluster_member WHERE cluster_id = ? AND instance_id = ?",
        (cluster_id, instance_id),
    ).fetchone()
    if member is None:
        raise ReviewError(f"instance {instance_id} is not in cluster {cluster_id}")
    with conn:
        conn.execute(
            "UPDATE cluster SET winner_instance_id = ?, status = 'reviewed'"
            " WHERE id = ?",
            (instance_id, cluster_id),
        )
        conn.execute(
            "UPDATE cluster_member SET role = 'loser' WHERE cluster_id = ?"
            " AND role = 'winner'",
            (cluster_id,),
        )
        conn.execute(
            "UPDATE cluster_member SET role = 'winner' WHERE cluster_id = ?"
            " AND instance_id = ?",
            (cluster_id, instance_id),
        )
        _decision(
            conn, f"cluster:{cluster_id}", "review.cluster.swap_winner",
            {"from": previous, "to": instance_id},
        )


def cluster_split(
    conn: sqlite3.Connection, cluster_id: int, instance_id: int
) -> int:
    """Split one member out into its own *reviewed* singleton cluster.

    The reviewed singleton locks the instance against re-clustering on later
    dedup runs, so the user's split decision persists (INV-6). If the source
    cluster is left with a single member, it too becomes a reviewed singleton.
    Returns the new cluster's id.

    Usage:
        >>> cluster_split(conn, 3, 42)  # doctest: +SKIP
        7
    """
    cluster = _cluster_row(conn, cluster_id)
    member = conn.execute(
        "SELECT role FROM cluster_member WHERE cluster_id = ? AND instance_id = ?",
        (cluster_id, instance_id),
    ).fetchone()
    if member is None:
        raise ReviewError(f"instance {instance_id} is not in cluster {cluster_id}")
    with conn:
        cur = conn.execute(
            "INSERT INTO cluster (kind, status, winner_instance_id)"
            " VALUES (?, 'reviewed', ?)",
            (cluster["kind"], instance_id),
        )
        new_cluster_id = cur.lastrowid
        assert new_cluster_id is not None
        conn.execute(
            "UPDATE cluster_member SET cluster_id = ?, role = 'winner'"
            " WHERE cluster_id = ? AND instance_id = ?",
            (new_cluster_id, cluster_id, instance_id),
        )
        remaining = conn.execute(
            "SELECT instance_id FROM cluster_member WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchall()
        if cluster["winner_instance_id"] == instance_id and remaining:
            conn.execute(
                "UPDATE cluster SET winner_instance_id = ? WHERE id = ?",
                (remaining[0]["instance_id"], cluster_id),
            )
        if len(remaining) == 1:
            conn.execute(
                "UPDATE cluster SET status = 'reviewed', winner_instance_id = ?"
                " WHERE id = ?",
                (remaining[0]["instance_id"], cluster_id),
            )
            conn.execute(
                "UPDATE cluster_member SET role = 'winner' WHERE cluster_id = ?",
                (cluster_id,),
            )
        _decision(
            conn, f"cluster:{cluster_id}", "review.cluster.split",
            {"instance": instance_id, "new_cluster": new_cluster_id},
        )
    return new_cluster_id


def cluster_not_duplicate(conn: sqlite3.Connection, cluster_id: int) -> None:
    """Mark a cluster as not-a-duplicate: no winner, every member stands alone.

    Usage:
        >>> cluster_not_duplicate(conn, 3)  # doctest: +SKIP
    """
    _cluster_row(conn, cluster_id)
    with conn:
        conn.execute(
            "UPDATE cluster SET status = 'reviewed', winner_instance_id = NULL"
            " WHERE id = ?",
            (cluster_id,),
        )
        conn.execute(
            "UPDATE cluster_member SET role = 'not_duplicate' WHERE cluster_id = ?",
            (cluster_id,),
        )
        _decision(conn, f"cluster:{cluster_id}", "review.cluster.not_duplicate", {})
