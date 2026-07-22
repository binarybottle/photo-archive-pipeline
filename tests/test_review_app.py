"""Review app integration tests over the fully resolved corpus."""

import logging
import shutil
import sqlite3
from pathlib import Path

import pytest
from conftest import PipelineEnv
from fastapi.testclient import TestClient

from archive_pipeline.dates import resolve_dates
from archive_pipeline.provenance import classify_local
from archive_pipeline.review.app import create_app

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)

LOG = logging.getLogger("archive_pipeline.test")


@pytest.fixture
def env(tmp_path: Path) -> PipelineEnv:
    e = PipelineEnv(tmp_path)
    classify_local(e.conn, e.cfg, e.wt, LOG)
    resolve_dates(e.conn, e.cfg, e.wt, LOG)
    return e


@pytest.fixture
def client(env: PipelineEnv) -> TestClient:
    return TestClient(create_app(env.wt))


def _iid(conn: sqlite3.Connection, rel_path: str) -> int:
    return conn.execute(
        "SELECT id FROM instance WHERE source = 'LOCAL' AND rel_path = ?", (rel_path,)
    ).fetchone()["id"]


def test_dashboard_shows_counts(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "date conflict(s) awaiting review" in page.text


def test_dates_queue_groups_by_folder(client: TestClient) -> None:
    page = client.get("/dates")
    assert page.status_code == 200
    assert "LOCAL / scans" in page.text
    assert "Apply folder date to all" in page.text


def test_item_page_shows_candidates_and_flags(
    env: PipelineEnv, client: TestClient
) -> None:
    iid = _iid(env.conn, "scans/scan001.png")
    page = client.get(f"/dates/item/{iid}")
    assert page.status_code == 200
    assert "scanner_createdate" in page.text
    assert "2019-11-03T10:00:00" in page.text  # EXIF (CreateDate) candidate
    assert client.get("/dates/item/999999").status_code == 404


def test_manual_resolution_via_form(env: PipelineEnv, client: TestClient) -> None:
    iid = _iid(env.conn, "scans/scan001.png")
    response = client.post(
        f"/dates/item/{iid}/resolve",
        data={"manual_date": "2001-07-01", "precision": "month"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = env.conn.execute(
        "SELECT * FROM date_resolution WHERE instance_id = ?", (iid,)
    ).fetchone()
    assert (row["status"], row["resolved_date"], row["resolved_source"]) == (
        "reviewed", "2001-07-01", "review"
    )
    decision = env.conn.execute(
        "SELECT actor, rule FROM decision ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert (decision["actor"], decision["rule"]) == ("review:user", "review.date.manual")


def test_invalid_manual_date_is_400(env: PipelineEnv, client: TestClient) -> None:
    iid = _iid(env.conn, "scans/scan001.png")
    response = client.post(
        f"/dates/item/{iid}/resolve",
        data={"manual_date": "bogus", "precision": "day"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_batch_trust_exif_resolves_scan_batch(
    env: PipelineEnv, client: TestClient
) -> None:
    response = client.post(
        "/dates/batch",
        data={"source": "LOCAL", "dir_path": "scans", "action": "exif"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    statuses = [
        row["status"]
        for row in env.conn.execute(
            "SELECT d.status FROM date_resolution d JOIN instance i"
            " ON i.id = d.instance_id WHERE i.rel_path LIKE 'scans/%'"
        )
    ]
    assert statuses == ["reviewed", "reviewed", "reviewed"]


def test_sequence_hint_via_form(env: PipelineEnv, client: TestClient) -> None:
    iid = _iid(env.conn, "scans/scan002.png")
    response = client.post(
        f"/dates/item/{iid}/sequence", data={"hint": "7"}, follow_redirects=False
    )
    assert response.status_code == 303
    row = env.conn.execute(
        "SELECT sequence_hint FROM date_resolution WHERE instance_id = ?", (iid,)
    ).fetchone()
    assert row["sequence_hint"] == 7


def test_reviewed_rows_survive_reresolution(env: PipelineEnv, client: TestClient) -> None:
    iid = _iid(env.conn, "scans/scan001.png")
    client.post(
        f"/dates/item/{iid}/resolve",
        data={"manual_date": "2001-07-01", "precision": "month"},
        follow_redirects=False,
    )
    summary = resolve_dates(env.conn, env.cfg, env.wt, LOG)
    assert summary.reviewed_preserved == 1
    row = env.conn.execute(
        "SELECT resolved_date FROM date_resolution WHERE instance_id = ?", (iid,)
    ).fetchone()
    assert row["resolved_date"] == "2001-07-01"


def test_thumbnails(env: PipelineEnv, client: TestClient) -> None:
    good = client.get(f"/thumb/{_iid(env.conn, '1998/beach_001.jpg')}")
    assert good.status_code == 200
    assert good.headers["content-type"] == "image/jpeg"
    assert len(good.content) > 500
    # Cached on second hit inside the working tree, never next to sources.
    assert any((env.wt.review_dir / "thumbs").iterdir())
    corrupt = client.get(f"/thumb/{_iid(env.conn, 'misc/empty.jpg')}")
    assert corrupt.status_code == 200  # placeholder, still renders a page


def test_cluster_queue_empty_then_populated(
    env: PipelineEnv, client: TestClient
) -> None:
    empty = client.get("/clusters")
    assert empty.status_code == 200
    assert "archive dedup" in empty.text

    a = _iid(env.conn, "1998/beach_001.jpg")
    b = _iid(env.conn, "topical/vacations/beach_001.jpg")
    with env.conn:
        cur = env.conn.execute(
            "INSERT INTO cluster (kind, status, winner_instance_id)"
            " VALUES ('exact', 'pending', ?)",
            (a,),
        )
        cid = cur.lastrowid
        env.conn.execute(
            "INSERT INTO cluster_member (cluster_id, instance_id, role, score)"
            " VALUES (?, ?, 'winner', 5.0), (?, ?, 'loser', 4.0)",
            (cid, a, cid, b),
        )
    page = client.get("/clusters")
    assert f"cluster {cid}" in page.text
    detail = client.get(f"/clusters/{cid}")
    assert detail.status_code == 200
    assert "beach_001.jpg" in detail.text

    response = client.post(
        f"/clusters/{cid}/action",
        data={"action": "swap", "winner": str(b)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = env.conn.execute("SELECT * FROM cluster WHERE id = ?", (cid,)).fetchone()
    assert (row["status"], row["winner_instance_id"]) == ("reviewed", b)
