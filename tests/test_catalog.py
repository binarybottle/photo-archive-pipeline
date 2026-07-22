"""Tests for the SQLite catalog: schema creation, pragmas, migration idempotency."""

import sqlite3
from pathlib import Path

import pytest

from archive_pipeline.catalog import (
    LATEST_SCHEMA_VERSION,
    connect,
    migrate,
    open_catalog,
    schema_version,
)

EXPECTED_TABLES = {
    "instance",
    "takeout_sidecar",
    "date_resolution",
    "cluster",
    "cluster_member",
    "decision",
    "placement",
    "run",
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "catalog.db"


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_open_catalog_creates_full_schema(db_path: Path) -> None:
    conn = open_catalog(db_path)
    assert _tables(conn) >= EXPECTED_TABLES
    assert schema_version(conn) == LATEST_SCHEMA_VERSION


def test_wal_and_foreign_keys_enabled(db_path: Path) -> None:
    conn = open_catalog(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_is_idempotent(db_path: Path) -> None:
    conn = connect(db_path)
    assert migrate(conn) == [LATEST_SCHEMA_VERSION]
    assert migrate(conn) == []
    conn.close()
    reopened = open_catalog(db_path)
    assert schema_version(reopened) == LATEST_SCHEMA_VERSION


def test_foreign_keys_actually_enforced(db_path: Path) -> None:
    conn = open_catalog(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cluster_member (cluster_id, instance_id) VALUES (999, 999)"
        )


def test_unique_source_relpath(db_path: Path) -> None:
    conn = open_catalog(db_path)
    row = ("LOCAL", "a/b.jpg", 10, "ab" * 32, 1)
    sql = (
        "INSERT INTO instance (source, rel_path, size_bytes, sha256, ingest_run_id)"
        " VALUES (?, ?, ?, ?, ?)"
    )
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)
