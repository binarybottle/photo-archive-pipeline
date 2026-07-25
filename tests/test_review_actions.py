"""Unit tests for the review action layer (no HTTP, no exiftool)."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from archive_pipeline.catalog import open_catalog
from archive_pipeline.review.actions import (
    ReviewError,
    accept_candidate,
    batch_apply,
    batch_bucket,
    batch_manual,
    batch_skip,
    bulk_accept_pending,
    bulk_prefer_curated,
    cluster_accept,
    cluster_not_duplicate,
    cluster_split,
    cluster_swap_winner,
    resolve_manual,
    set_sequence_hint,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_catalog(tmp_path / "catalog.db")


def _instance(conn: sqlite3.Connection, rel_path: str, source: str = "LOCAL") -> int:
    cur = conn.execute(
        "INSERT INTO instance (source, rel_path, size_bytes, sha256, kind,"
        " ingest_run_id) VALUES (?, ?, 1, ?, 'image', 1)",
        (source, rel_path, f"hash-{source}-{rel_path}"),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _resolution(conn: sqlite3.Connection, instance_id: int, **kw: Any) -> None:
    row = {
        "instance_id": instance_id,
        "cand_exif": None,
        "cand_folder": None,
        "cand_takeout": None,
        "cand_filename": None,
        "folder_precision": None,
        "status": "conflict",
    } | kw
    conn.execute(
        "INSERT INTO date_resolution (instance_id, cand_exif, cand_folder,"
        " cand_takeout, cand_filename, folder_precision, status)"
        " VALUES (:instance_id, :cand_exif, :cand_folder, :cand_takeout,"
        " :cand_filename, :folder_precision, :status)",
        row,
    )


def _row(conn: sqlite3.Connection, instance_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM date_resolution WHERE instance_id = ?", (instance_id,)
    ).fetchone()


def _last_decision(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM decision ORDER BY id DESC LIMIT 1").fetchone()


def test_accept_candidate_exif(conn: sqlite3.Connection) -> None:
    iid = _instance(conn, "a/x.jpg")
    _resolution(conn, iid, cand_exif="1998-07-12T14:33:05")
    assert accept_candidate(conn, iid, "exif") == "1998-07-12T14:33:05"
    row = _row(conn, iid)
    assert (row["status"], row["resolved_source"], row["resolved_precision"]) == (
        "reviewed", "exif", "second"
    )
    decision = _last_decision(conn)
    assert decision["actor"] == "review:user"
    assert decision["rule"] == "review.date.accept_exif"


def test_accept_folder_uses_folder_precision(conn: sqlite3.Connection) -> None:
    iid = _instance(conn, "1998/x.jpg")
    _resolution(conn, iid, cand_folder="1998-01-01", folder_precision="year")
    accept_candidate(conn, iid, "folder")
    row = _row(conn, iid)
    assert (row["resolved_date"], row["resolved_precision"]) == ("1998-01-01", "year")


def test_accept_missing_candidate_raises(conn: sqlite3.Connection) -> None:
    iid = _instance(conn, "a/x.jpg")
    _resolution(conn, iid)
    with pytest.raises(ReviewError, match="no takeout candidate"):
        accept_candidate(conn, iid, "takeout")
    with pytest.raises(ReviewError, match="unknown candidate"):
        accept_candidate(conn, iid, "mtime")


def test_resolve_manual(conn: sqlite3.Connection) -> None:
    iid = _instance(conn, "scans/s1.png")
    _resolution(conn, iid)
    resolve_manual(conn, iid, "2001-07-01")
    row = _row(conn, iid)
    assert (row["resolved_date"], row["resolved_source"], row["status"]) == (
        "2001-07-01", "review", "reviewed"
    )


@pytest.mark.parametrize(
    ("entered", "date", "precision"),
    [
        ("2000", "2000-01-01", "year"),
        ("2000-12", "2000-12-01", "month"),
        ("2000-12-24", "2000-12-24", "day"),
        ("2000-12-24T21:34:13", "2000-12-24T21:34:13", "second"),
        ("  2000-12  ", "2000-12-01", "month"),
    ],
)
def test_resolve_manual_infers_precision(
    conn: sqlite3.Connection, entered: str, date: str, precision: str
) -> None:
    iid = _instance(conn, "scans/s1.png")
    _resolution(conn, iid)
    resolve_manual(conn, iid, entered)
    row = _row(conn, iid)
    assert (row["resolved_date"], row["resolved_precision"]) == (date, precision)


def test_resolve_manual_validation(conn: sqlite3.Connection) -> None:
    iid = _instance(conn, "scans/s1.png")
    _resolution(conn, iid)
    with pytest.raises(ReviewError, match="enter a year"):
        resolve_manual(conn, iid, "01/07/2001")
    with pytest.raises(ReviewError, match="not a real date"):
        resolve_manual(conn, iid, "2001-02-31")
    with pytest.raises(ReviewError, match="not a real date"):
        resolve_manual(conn, iid, "2001-13")
    with pytest.raises(ReviewError, match="no date resolution"):
        resolve_manual(conn, 9999, "2001-07-01")


def test_batch_apply_folder_scoped_to_directory(conn: sqlite3.Connection) -> None:
    a = _instance(conn, "1998/a.jpg")
    b = _instance(conn, "1998/b.jpg")
    c = _instance(conn, "1998/c.jpg")  # no folder candidate
    sub = _instance(conn, "1998/sub/d.jpg")  # different directory
    for iid in (a, b):
        _resolution(conn, iid, cand_folder="1998-01-01", folder_precision="year")
    _resolution(conn, c)
    _resolution(conn, sub, cand_folder="1998-01-01", folder_precision="year")

    assert batch_apply(conn, "LOCAL", "1998", "folder") == 2
    assert _row(conn, a)["status"] == "reviewed"
    assert _row(conn, b)["resolved_date"] == "1998-01-01"
    assert _row(conn, c)["status"] == "conflict"
    assert _row(conn, sub)["status"] == "conflict"
    # Already-reviewed rows are not touched by a second batch.
    assert batch_apply(conn, "LOCAL", "1998", "folder") == 0


def test_batch_apply_exif(conn: sqlite3.Connection) -> None:
    iid = _instance(conn, "roll/x.jpg")
    _resolution(conn, iid, cand_exif="2019-11-03")
    assert batch_apply(conn, "LOCAL", "roll", "exif") == 1
    row = _row(conn, iid)
    assert (row["resolved_source"], row["resolved_precision"]) == ("exif", "day")
    with pytest.raises(ReviewError, match="unknown batch action"):
        batch_apply(conn, "LOCAL", "roll", "takeout")


def test_batch_apply_filename(conn: sqlite3.Connection) -> None:
    a = _instance(conn, "Vacation/2004/a.jpg")
    b = _instance(conn, "Vacation/2004/b.jpg")
    for iid in (a, b):
        _resolution(
            conn, iid, cand_exif="2002-09-01T10:00:00", cand_folder="2004-01-01",
            folder_precision="year", cand_filename="2004-06-23",
        )
    assert batch_apply(conn, "LOCAL", "Vacation/2004", "filename") == 2
    row = _row(conn, a)
    assert (row["resolved_date"], row["resolved_source"], row["resolved_precision"]) == (
        "2004-06-23", "filename", "day"
    )
    assert row["status"] == "reviewed"


def test_batch_manual_precisions(conn: sqlite3.Connection) -> None:
    y = _instance(conn, "oldy/a.jpg")
    _resolution(conn, y)
    assert batch_manual(conn, "LOCAL", "oldy", "1998") == 1
    assert (_row(conn, y)["resolved_date"], _row(conn, y)["resolved_precision"],
            _row(conn, y)["resolved_source"]) == ("1998-01-01", "year", "review")

    m = _instance(conn, "oldm/a.jpg")
    _resolution(conn, m)
    assert batch_manual(conn, "LOCAL", "oldm", "1998-07") == 1
    assert (_row(conn, m)["resolved_date"], _row(conn, m)["resolved_precision"]) == (
        "1998-07-01", "month"
    )

    d = _instance(conn, "oldd/a.jpg")
    _resolution(conn, d)
    assert batch_manual(conn, "LOCAL", "oldd", "1998-07-15") == 1
    assert (_row(conn, d)["resolved_date"], _row(conn, d)["resolved_precision"]) == (
        "1998-07-15", "day"
    )


def test_batch_manual_validation(conn: sqlite3.Connection) -> None:
    iid = _instance(conn, "old/x.jpg")
    _resolution(conn, iid)
    with pytest.raises(ReviewError, match="year, year-month"):
        batch_manual(conn, "LOCAL", "old", "June 1998")
    with pytest.raises(ReviewError, match="not a real date"):
        batch_manual(conn, "LOCAL", "old", "1998-13")


def test_batch_skip_and_restore(conn: sqlite3.Connection) -> None:
    a = _instance(conn, "movies/a.jpg")
    b = _instance(conn, "movies/b.jpg")
    for iid in (a, b):
        _resolution(conn, iid)
    assert batch_skip(conn, "LOCAL", "movies", True) == 2
    assert _row(conn, a)["skipped"] == 1
    assert _row(conn, a)["status"] == "conflict"  # still unresolved, just hidden
    assert batch_skip(conn, "LOCAL", "movies", False) == 2
    assert _row(conn, a)["skipped"] == 0


def test_batch_bucket_pre_2000(conn: sqlite3.Connection) -> None:
    a = _instance(conn, "old-scans/a.jpg")
    b = _instance(conn, "old-scans/b.jpg")
    for iid in (a, b):
        _resolution(conn, iid)  # undated
    assert batch_bucket(conn, "LOCAL", "old-scans", "pre-2000") == 2
    row = _row(conn, a)
    assert (row["status"], row["bucket"], row["resolved_date"]) == (
        "reviewed", "pre-2000", None
    )
    assert row["resolved_source"] == "review"
    assert _last_decision(conn)["rule"] == "review.date.bucket"
    with pytest.raises(ReviewError, match="not a bucket"):
        batch_bucket(conn, "LOCAL", "old-scans", "medieval")


def test_batch_manual_year_range_files_into_bucket(conn: sqlite3.Connection) -> None:
    a = _instance(conn, "spans/2004-2009/a.jpg")
    b = _instance(conn, "spans/2004-2009/b.jpg")
    for iid in (a, b):
        _resolution(conn, iid)
    # A YYYY-YYYY entry in the manual field becomes a range bucket, not a date.
    assert batch_manual(conn, "LOCAL", "spans/2004-2009", "2004-2009") == 2
    row = _row(conn, a)
    assert (row["status"], row["bucket"], row["resolved_date"]) == (
        "reviewed", "2004-2009", None
    )


def test_year_range_validation(conn: sqlite3.Connection) -> None:
    iid = _instance(conn, "spans/x.jpg")
    _resolution(conn, iid)
    with pytest.raises(ReviewError, match="start after end"):
        batch_manual(conn, "LOCAL", "spans", "2009-2004")
    # A year-month (YYYY-MM) is still a date, not a range.
    assert batch_manual(conn, "LOCAL", "spans", "2005-06") == 1
    assert _row(conn, iid)["resolved_date"] == "2005-06-01"


def test_sequence_hint(conn: sqlite3.Connection) -> None:
    iid = _instance(conn, "scans/s1.png")
    _resolution(conn, iid)
    set_sequence_hint(conn, iid, 3)
    assert _row(conn, iid)["sequence_hint"] == 3
    set_sequence_hint(conn, iid, None)
    assert _row(conn, iid)["sequence_hint"] is None
    assert _last_decision(conn)["rule"] == "review.sequence_hint"


def _cluster(conn: sqlite3.Connection, winner: int, loser: int) -> int:
    cur = conn.execute(
        "INSERT INTO cluster (kind, status, winner_instance_id)"
        " VALUES ('near_image', 'pending', ?)",
        (winner,),
    )
    cluster_id = cur.lastrowid
    assert cluster_id is not None
    conn.execute(
        "INSERT INTO cluster_member (cluster_id, instance_id, role, score)"
        " VALUES (?, ?, 'winner', 5.0), (?, ?, 'loser', 3.0)",
        (cluster_id, winner, cluster_id, loser),
    )
    return cluster_id


def test_cluster_accept(conn: sqlite3.Connection) -> None:
    winner, loser = _instance(conn, "a.jpg"), _instance(conn, "b.jpg")
    cid = _cluster(conn, winner, loser)
    cluster_accept(conn, cid)
    row = conn.execute("SELECT * FROM cluster WHERE id = ?", (cid,)).fetchone()
    assert (row["status"], row["winner_instance_id"]) == ("reviewed", winner)
    assert _last_decision(conn)["actor"] == "review:user"


def test_cluster_swap_winner(conn: sqlite3.Connection) -> None:
    winner, loser = _instance(conn, "a.jpg"), _instance(conn, "b.jpg")
    cid = _cluster(conn, winner, loser)
    cluster_swap_winner(conn, cid, loser)
    row = conn.execute("SELECT * FROM cluster WHERE id = ?", (cid,)).fetchone()
    assert (row["status"], row["winner_instance_id"]) == ("reviewed", loser)
    roles = {
        r["instance_id"]: r["role"]
        for r in conn.execute(
            "SELECT instance_id, role FROM cluster_member WHERE cluster_id = ?", (cid,)
        )
    }
    assert roles == {winner: "loser", loser: "winner"}
    outsider = _instance(conn, "c.jpg")
    with pytest.raises(ReviewError, match="not in cluster"):
        cluster_swap_winner(conn, cid, outsider)


def test_cluster_not_duplicate(conn: sqlite3.Connection) -> None:
    winner, loser = _instance(conn, "a.jpg"), _instance(conn, "b.jpg")
    cid = _cluster(conn, winner, loser)
    cluster_not_duplicate(conn, cid)
    row = conn.execute("SELECT * FROM cluster WHERE id = ?", (cid,)).fetchone()
    assert (row["status"], row["winner_instance_id"]) == ("reviewed", None)
    roles = {
        r["role"]
        for r in conn.execute(
            "SELECT role FROM cluster_member WHERE cluster_id = ?", (cid,)
        )
    }
    assert roles == {"not_duplicate"}
    with pytest.raises(ReviewError, match="no cluster"):
        cluster_accept(conn, 999)


def test_cluster_split_member_out(conn: sqlite3.Connection) -> None:
    a, b, c = (_instance(conn, n) for n in ("a.jpg", "b.jpg", "c.jpg"))
    cid = _cluster(conn, a, b)
    conn.execute(
        "INSERT INTO cluster_member (cluster_id, instance_id, role, score)"
        " VALUES (?, ?, 'loser', 2.0)",
        (cid, c),
    )
    new_cid = cluster_split(conn, cid, c)
    new_cluster = conn.execute(
        "SELECT * FROM cluster WHERE id = ?", (new_cid,)
    ).fetchone()
    assert (new_cluster["status"], new_cluster["winner_instance_id"]) == ("reviewed", c)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM cluster_member WHERE cluster_id = ?", (cid,)
    ).fetchone()[0]
    assert remaining == 2
    original = conn.execute("SELECT * FROM cluster WHERE id = ?", (cid,)).fetchone()
    assert original["winner_instance_id"] == a  # winner untouched
    assert _last_decision(conn)["rule"] == "review.cluster.split"


def test_cluster_split_down_to_singletons(conn: sqlite3.Connection) -> None:
    a, b = _instance(conn, "a.jpg"), _instance(conn, "b.jpg")
    cid = _cluster(conn, a, b)
    cluster_split(conn, cid, a)  # split the winner out
    original = conn.execute("SELECT * FROM cluster WHERE id = ?", (cid,)).fetchone()
    assert (original["status"], original["winner_instance_id"]) == ("reviewed", b)
    roles = [
        r["role"]
        for r in conn.execute(
            "SELECT role FROM cluster_member WHERE cluster_id = ?", (cid,)
        )
    ]
    assert roles == ["winner"]
    with pytest.raises(ReviewError, match="not in cluster"):
        cluster_split(conn, cid, a)


def _img(conn: sqlite3.Connection, rel: str, trust: str, w: int, h: int) -> int:
    cur = conn.execute(
        "INSERT INTO instance (source, rel_path, size_bytes, sha256, kind,"
        " ingest_run_id, effective_trust, width, height)"
        " VALUES ('LOCAL', ?, 1, ?, 'image', 1, ?, ?, ?)",
        (rel, f"h-{rel}", trust, w, h),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_bulk_prefer_curated_swaps_to_original(conn: sqlite3.Connection) -> None:
    takeout = _img(conn, "google/x.jpg", "takeout", 4000, 3000)
    curated = _img(conn, "mine/x.jpg", "curated", 4000, 3000)
    with conn:
        cur = conn.execute(
            "INSERT INTO cluster (kind, status, winner_instance_id)"
            " VALUES ('exact', 'pending', ?)",
            (takeout,),
        )
        cid = cur.lastrowid
        conn.execute(
            "INSERT INTO cluster_member (cluster_id, instance_id, role)"
            " VALUES (?, ?, 'winner'), (?, ?, 'loser')",
            (cid, takeout, cid, curated),
        )
    assert bulk_prefer_curated(conn) == 1
    row = conn.execute("SELECT * FROM cluster WHERE id = ?", (cid,)).fetchone()
    assert (row["status"], row["winner_instance_id"]) == ("reviewed", curated)
    roles = {
        r["instance_id"]: r["role"]
        for r in conn.execute(
            "SELECT instance_id, role FROM cluster_member WHERE cluster_id = ?", (cid,)
        )
    }
    assert roles == {takeout: "loser", curated: "winner"}


def test_bulk_accept_pending_excludes_videos(conn: sqlite3.Connection) -> None:
    a, b = _instance(conn, "a.jpg"), _instance(conn, "b.jpg")
    with conn:
        img = conn.execute(
            "INSERT INTO cluster (kind, status, winner_instance_id)"
            " VALUES ('near_image', 'pending', ?)",
            (a,),
        ).lastrowid
        vid = conn.execute(
            "INSERT INTO cluster (kind, status, winner_instance_id)"
            " VALUES ('near_video', 'pending', ?)",
            (b,),
        ).lastrowid
    assert bulk_accept_pending(conn) == 1  # image cluster only
    status = {
        r["id"]: r["status"]
        for r in conn.execute("SELECT id, status FROM cluster")
    }
    assert status[img] == "reviewed"
    assert status[vid] == "pending"  # videos are never bulk-collapsed


def test_bulk_accept_pending_include_video(conn: sqlite3.Connection) -> None:
    a, b = _instance(conn, "a.jpg"), _instance(conn, "b.mov")
    with conn:
        img = conn.execute(
            "INSERT INTO cluster (kind, status, winner_instance_id)"
            " VALUES ('near_image', 'pending', ?)",
            (a,),
        ).lastrowid
        vid = conn.execute(
            "INSERT INTO cluster (kind, status, winner_instance_id)"
            " VALUES ('near_video', 'pending', ?)",
            (b,),
        ).lastrowid
    assert bulk_accept_pending(conn, include_video=True) == 2
    status = {
        r["id"]: r["status"]
        for r in conn.execute("SELECT id, status FROM cluster")
    }
    assert status[img] == "reviewed"
    assert status[vid] == "reviewed"  # opt-in includes videos
