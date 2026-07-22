"""Run bookkeeping: every stage invocation is recorded in the ``run`` table.

A run row is inserted with status ``running`` when the stage starts and updated
to ``ok`` or ``failed`` when it finishes. An interrupted process leaves the row
at ``running``, which is honest: resumability (INV-5) is the stage's job, and a
stale ``running`` row is visible evidence of an interruption.

Usage:
    >>> from archive_pipeline.catalog import open_catalog
    >>> from archive_pipeline.runs import record_run
    >>> conn = open_catalog(db_path)  # doctest: +SKIP
    >>> with record_run(conn, "ingest", {"root": "/photos"}) as run_id:  # doctest: +SKIP
    ...     do_work()
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    """Current UTC time as ISO-8601 with second precision."""
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def pipeline_git_rev() -> str | None:
    """Git revision of the pipeline source tree, or None if unavailable.

    Usage:
        >>> rev = pipeline_git_rev()
        >>> rev is None or len(rev) == 40
        True
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


@contextmanager
def record_run(
    conn: sqlite3.Connection, stage: str, args: dict[str, Any] | None = None
) -> Iterator[int]:
    """Record a stage run; yield its run id; finalize status on exit.

    On a raised exception the run is marked ``failed`` and the exception
    propagates; on clean exit it is marked ``ok``.

    Usage:
        >>> with record_run(conn, "ingest", {"source": "LOCAL"}) as run_id:  # doctest: +SKIP
        ...     pass
    """
    with conn:
        cursor = conn.execute(
            "INSERT INTO run (stage, started, args_json, git_rev, status)"
            " VALUES (?, ?, ?, ?, 'running')",
            (stage, _utcnow(), json.dumps(args or {}, sort_keys=True), pipeline_git_rev()),
        )
    run_id = cursor.lastrowid
    assert run_id is not None
    try:
        yield run_id
    except BaseException:
        with conn:
            conn.execute(
                "UPDATE run SET finished = ?, status = 'failed' WHERE id = ?",
                (_utcnow(), run_id),
            )
        raise
    else:
        with conn:
            conn.execute(
                "UPDATE run SET finished = ?, status = 'ok' WHERE id = ?",
                (_utcnow(), run_id),
            )
