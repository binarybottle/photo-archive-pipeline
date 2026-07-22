"""Tests for run bookkeeping: ok/failed statuses, timestamps, args recording."""

import json
import sqlite3
from pathlib import Path

import pytest

from archive_pipeline.catalog import open_catalog
from archive_pipeline.runs import record_run


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_catalog(tmp_path / "catalog.db")


def _run_row(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    assert row is not None
    return row


def test_successful_run_recorded_ok(conn: sqlite3.Connection) -> None:
    with record_run(conn, "ingest", {"source": "LOCAL", "root": "/photos"}) as run_id:
        row = _run_row(conn, run_id)
        assert row["status"] == "running"
        assert row["finished"] is None
    row = _run_row(conn, run_id)
    assert row["stage"] == "ingest"
    assert row["status"] == "ok"
    assert row["started"] is not None and row["finished"] is not None
    assert json.loads(row["args_json"]) == {"source": "LOCAL", "root": "/photos"}


def test_failed_run_recorded_failed_and_reraises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="boom"):  # noqa: SIM117
        with record_run(conn, "dedup") as run_id:
            raise ValueError("boom")
    row = _run_row(conn, run_id)
    assert row["status"] == "failed"
    assert row["finished"] is not None


def test_runs_accumulate(conn: sqlite3.Connection) -> None:
    with record_run(conn, "a"):
        pass
    with record_run(conn, "b"):
        pass
    count = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
    assert count == 2
