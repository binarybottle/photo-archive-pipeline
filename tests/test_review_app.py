"""Review app integration tests over the fully resolved corpus."""

import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from conftest import PipelineEnv
from fastapi.testclient import TestClient

from archive_pipeline.dates import resolve_dates
from archive_pipeline.provenance import classify_local
from archive_pipeline.review.app import _placeholder_jpeg, create_app
from archive_pipeline.video import ffmpeg_available

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
    # Batch buttons for all three candidates, plus the manual date field.
    assert "Use folder date" in page.text
    assert "Use EXIF" in page.text
    assert "Use filename date" in page.text
    assert 'action="/dates/batch-manual"' in page.text
    # Candidate dates must be shown so batch decisions are informed.
    assert "2019-11-03" in page.text  # scans' CreateDate candidate, per-item + summary


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


def test_skip_folder_toggle(env: PipelineEnv, client: TestClient) -> None:
    resp = client.post(
        "/dates/skip",
        data={"source": "LOCAL", "dir_path": "scans", "skipped": "1", "show": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    active = client.get("/dates")
    assert "LOCAL / scans" not in active.text
    assert "/dates?show=skipped" in active.text  # toggle link appears
    skipped = client.get("/dates?show=skipped")
    assert "LOCAL / scans" in skipped.text
    assert "Un-skip" in skipped.text
    client.post(
        "/dates/skip",
        data={"source": "LOCAL", "dir_path": "scans", "skipped": "0", "show": "skipped"},
        follow_redirects=False,
    )
    assert "LOCAL / scans" in client.get("/dates").text


def test_conflict_items_label_video_and_corrupt(
    env: PipelineEnv, client: TestClient
) -> None:
    # The zero-byte image is labeled unreadable, not shown as a broken thumbnail.
    assert "unreadable" in client.get("/dates").text
    with env.conn:
        cur = env.conn.execute(
            "INSERT INTO instance (source, rel_path, size_bytes, sha256, kind,"
            " ingest_run_id) VALUES ('LOCAL', 'clips/v.mov', 9, ?, 'video', 1)",
            ("bb" * 32,),
        )
        env.conn.execute(
            "INSERT INTO date_resolution (instance_id, status) VALUES (?, 'conflict')",
            (cur.lastrowid,),
        )
    assert "video" in client.get("/dates").text


def test_video_thumbnail_extracted(env: PipelineEnv, client: TestClient) -> None:
    if not ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    root = Path(
        env.conn.execute("SELECT root FROM source_root WHERE source = 'LOCAL'")
        .fetchone()[0]
    )
    clip = root / "clips" / "v.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=2:size=160x120:rate=10", "-pix_fmt", "yuv420p",
         str(clip)],
        check=True, capture_output=True,
    )
    with env.conn:
        cur = env.conn.execute(
            "INSERT INTO instance (source, rel_path, size_bytes, sha256, kind,"
            " ingest_run_id) VALUES ('LOCAL', 'clips/v.mp4', ?, ?, 'video', 1)",
            (clip.stat().st_size, "cd" * 32),
        )
    resp = client.get(f"/thumb/{cur.lastrowid}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    # A real extracted frame, not the tiny gray placeholder.
    assert len(resp.content) > len(_placeholder_jpeg())


def test_pre_2000_bucket_via_form(env: PipelineEnv, client: TestClient) -> None:
    response = client.post(
        "/dates/batch-bucket",
        data={"source": "LOCAL", "dir_path": "scans", "bucket": "pre-2000"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    rows = env.conn.execute(
        "SELECT d.bucket, d.status FROM date_resolution d JOIN instance i"
        " ON i.id = d.instance_id WHERE i.rel_path LIKE 'scans/%'"
    ).fetchall()
    assert rows
    assert all((r["bucket"], r["status"]) == ("pre-2000", "reviewed") for r in rows)


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
