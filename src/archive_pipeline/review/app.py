"""FastAPI review app (spec Stage 4): 127.0.0.1-only catalog review.

The app reads and writes the working tree's catalog. Thumbnails are rendered
on demand into ``review/thumbs/`` (pipeline-owned; sources stay untouched).
All mutations go through :mod:`archive_pipeline.review.actions`, which appends
to the decision log with actor ``review:user``.

Usage:
    >>> from archive_pipeline.review.app import create_app
    >>> app = create_app(working_tree)  # doctest: +SKIP
    >>> # then: uvicorn.run(app, host="127.0.0.1", port=8765)
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pillow_heif
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps

from archive_pipeline.catalog import open_catalog
from archive_pipeline.review import actions
from archive_pipeline.video import extract_frame
from archive_pipeline.workingtree import WorkingTree

pillow_heif.register_heif_opener()

#: A folder literally named as a year span, e.g. "2004-2009" -> suggest the range.
_RANGE_FOLDER = re.compile(r"^(19|20)\d{2}-(19|20)\d{2}$")

_THUMB_EDGE = 480
_CAMERA_WINDOW = timedelta(hours=1)

#: 1x1 gray JPEG used when a thumbnail cannot be rendered (corrupt, video...).
_PLACEHOLDER: bytes | None = None


def _placeholder_jpeg() -> bytes:
    global _PLACEHOLDER
    if _PLACEHOLDER is None:
        buf = io.BytesIO()
        Image.new("RGB", (120, 90), (200, 200, 200)).save(buf, format="JPEG")
        _PLACEHOLDER = buf.getvalue()
    return _PLACEHOLDER


def create_app(wt: WorkingTree) -> FastAPI:
    """Build the review app bound to one working tree.

    Usage:
        >>> app = create_app(wt)  # doctest: +SKIP
    """
    app = FastAPI(title="Archive review", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    thumbs_dir = wt.review_dir / "thumbs"

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = open_catalog(wt.catalog_path, cross_thread=True)
        try:
            yield conn
        finally:
            conn.close()

    def _source_roots(conn: sqlite3.Connection) -> dict[str, Path]:
        return {
            row["source"]: Path(row["root"])
            for row in conn.execute("SELECT source, root FROM source_root")
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Response:
        counts = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM date_resolution GROUP BY status"
            ).fetchall()
        )
        clusters_pending = conn.execute(
            "SELECT COUNT(*) FROM cluster WHERE status = 'pending'"
        ).fetchone()[0]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "conflicts": counts.get("conflict", 0),
                "reviewed": counts.get("reviewed", 0),
                "clusters_pending": clusters_pending,
                "working_tree": str(wt.root),
            },
        )

    @app.get("/dates", response_class=HTMLResponse)
    def dates_list(
        request: Request,
        show: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        showing_skipped = show == "skipped"
        skip_filter = "d.skipped = 1" if showing_skipped else "COALESCE(d.skipped, 0) = 0"
        # After dedup, a cluster loser goes to quarantine byte-identical (its date
        # never reaches the archive), and a winner whose cluster already has a
        # merged date is settled — neither needs date review. Singletons and
        # members of still-undated clusters remain.
        dedup_settled = (
            "EXISTS (SELECT 1 FROM cluster_member cm"
            " LEFT JOIN cluster_merge cmg ON cmg.cluster_id = cm.cluster_id"
            " WHERE cm.instance_id = i.id AND (cm.role = 'loser' OR"
            " (cm.role = 'winner' AND json_extract(cmg.merged_json, '$.date.date')"
            " IS NOT NULL)))"
        )
        where = f"d.status = 'conflict' AND {skip_filter}"
        if not showing_skipped:
            where += f" AND NOT {dedup_settled}"
        rows = conn.execute(
            "SELECT i.id, i.source, i.rel_path, i.kind, i.flags, d.cand_exif,"
            " d.cand_folder, d.cand_takeout, d.cand_filename, d.folder_precision,"
            " d.exif_flags FROM date_resolution d JOIN instance i ON i.id = d.instance_id"
            f" WHERE {where} ORDER BY i.source, i.rel_path"
        ).fetchall()
        skipped_count = conn.execute(
            "SELECT COUNT(*) FROM date_resolution WHERE status = 'conflict'"
            " AND skipped = 1"
        ).fetchone()[0]
        dedup_hidden = conn.execute(
            "SELECT COUNT(*) FROM date_resolution d JOIN instance i"
            " ON i.id = d.instance_id WHERE d.status = 'conflict'"
            f" AND COALESCE(d.skipped, 0) = 0 AND {dedup_settled}"
        ).fetchone()[0]
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (row["source"], posixpath.dirname(row["rel_path"]))
            groups.setdefault(key, []).append(row)
        group_list = []
        for (source, dir_path), entries in sorted(groups.items()):
            folder = next((e["cand_folder"] for e in entries if e["cand_folder"]), None)
            precision = next(
                (e["folder_precision"] for e in entries if e["cand_folder"]), None
            )
            def summarize(column: str, rows: list[sqlite3.Row] = entries) -> str | None:
                vals = sorted({str(r[column])[:10] for r in rows if r[column]})
                if not vals:
                    return None
                if len(vals) == 1:
                    return vals[0]
                return f"{vals[0]} … {vals[-1]} ({len(vals)} distinct)"

            last = posixpath.basename(dir_path)
            group_list.append(
                {
                    "source": source, "dir": dir_path, "entries": entries,
                    "folder_date": folder, "folder_precision": precision,
                    "exif_summary": summarize("cand_exif"),
                    "filename_summary": summarize("cand_filename"),
                    "suggest": last if _RANGE_FOLDER.match(last) else "",
                }
            )
        return templates.TemplateResponse(
            request,
            "dates_list.html",
            {
                "total": len(rows), "groups": group_list,
                "showing_skipped": showing_skipped, "skipped_count": skipped_count,
                "dedup_hidden": dedup_hidden,
            },
        )

    @app.post("/dates/skip")
    def dates_skip(
        source: str = Form(...),
        dir_path: str = Form(""),
        skipped: str = Form("1"),
        show: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        actions.batch_skip(conn, source, dir_path, skipped == "1")
        target = "/dates?show=skipped" if show == "skipped" else "/dates"
        return RedirectResponse(target, status_code=303)

    @app.get("/dates/item/{instance_id}", response_class=HTMLResponse)
    def date_item(
        request: Request,
        instance_id: int,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        item = conn.execute(
            "SELECT i.*, d.cand_exif, d.cand_folder, d.cand_takeout, d.cand_filename,"
            " d.folder_precision, d.exif_flags, d.resolved_date, d.resolved_precision,"
            " d.status, d.sequence_hint FROM instance i"
            " JOIN date_resolution d ON d.instance_id = i.id WHERE i.id = ?",
            (instance_id,),
        ).fetchone()
        if item is None:
            raise HTTPException(404, "no such instance in the date queue")
        candidates = [
            {"key": "exif", "label": "EXIF", "value": item["cand_exif"], "note": None},
            {"key": "folder", "label": "Folder",
             "value": item["cand_folder"],
             "note": item["folder_precision"] and f"{item['folder_precision']} precision"},
            {"key": "takeout", "label": "Takeout JSON",
             "value": item["cand_takeout"], "note": None},
            {"key": "filename", "label": "Filename",
             "value": item["cand_filename"], "note": None},
        ]
        dir_path = posixpath.dirname(item["rel_path"])
        folder_strip = conn.execute(
            "SELECT i.id, i.rel_path FROM instance i WHERE i.source = ?"
            " AND i.rel_path LIKE ? AND i.rel_path NOT LIKE ?"
            " AND i.kind IN ('image', 'video') ORDER BY i.rel_path LIMIT 14",
            (
                item["source"],
                f"{dir_path}/%" if dir_path else "%",
                f"{dir_path}/%/%" if dir_path else "%/%",
            ),
        ).fetchall()
        camera_strip: list[sqlite3.Row] = []
        if item["camera_model"] and item["exif_dto"]:
            try:
                center = datetime.fromisoformat(item["exif_dto"])
                lo = (center - _CAMERA_WINDOW).isoformat()
                hi = (center + _CAMERA_WINDOW).isoformat()
                camera_strip = conn.execute(
                    "SELECT id, rel_path, exif_dto FROM instance"
                    " WHERE camera_model = ? AND exif_dto BETWEEN ? AND ?"
                    " AND id != ? ORDER BY exif_dto LIMIT 10",
                    (item["camera_model"], lo, hi, instance_id),
                ).fetchall()
            except ValueError:
                pass
        return templates.TemplateResponse(
            request,
            "date_item.html",
            {
                "item": item,
                "flags": json.loads(item["exif_flags"] or "[]"),
                "candidates": candidates,
                "folder_strip": folder_strip,
                "camera_strip": camera_strip,
            },
        )

    @app.post("/dates/item/{instance_id}/resolve")
    def date_resolve_action(
        instance_id: int,
        candidate: str | None = Form(None),
        manual_date: str | None = Form(None),
        precision: str = Form("day"),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        try:
            if candidate:
                actions.accept_candidate(conn, instance_id, candidate)
            elif manual_date:
                actions.resolve_manual(conn, instance_id, manual_date.strip(), precision)
            else:
                raise actions.ReviewError("choose a candidate or enter a date")
        except actions.ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/dates", status_code=303)

    @app.post("/dates/item/{instance_id}/sequence")
    def sequence_action(
        instance_id: int,
        hint: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        value: int | None
        if hint.strip() == "":
            value = None
        else:
            try:
                value = int(hint)
            except ValueError as exc:
                raise HTTPException(400, "sequence hint must be an integer") from exc
        try:
            actions.set_sequence_hint(conn, instance_id, value)
        except actions.ReviewError as exc:
            raise HTTPException(404, str(exc)) from exc
        return RedirectResponse(f"/dates/item/{instance_id}", status_code=303)

    @app.post("/dates/batch")
    def dates_batch(
        source: str = Form(...),
        dir_path: str = Form(""),
        action: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        try:
            actions.batch_apply(conn, source, dir_path, action)
        except actions.ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/dates", status_code=303)

    @app.post("/dates/batch-manual")
    def dates_batch_manual(
        source: str = Form(...),
        dir_path: str = Form(""),
        date: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        try:
            actions.batch_manual(conn, source, dir_path, date)
        except actions.ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/dates", status_code=303)

    @app.post("/dates/batch-bucket")
    def dates_batch_bucket(
        source: str = Form(...),
        dir_path: str = Form(""),
        bucket: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        try:
            actions.batch_bucket(conn, source, dir_path, bucket)
        except actions.ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/dates", status_code=303)

    @app.get("/clusters", response_class=HTMLResponse)
    def clusters_list(
        request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Response:
        rows = conn.execute(
            "SELECT c.id, c.kind, c.winner_instance_id, w.rel_path AS winner_path,"
            " (SELECT COUNT(*) FROM cluster_member m WHERE m.cluster_id = c.id)"
            "   AS members"
            " FROM cluster c LEFT JOIN instance w ON w.id = c.winner_instance_id"
            " WHERE c.status = 'pending' ORDER BY c.id LIMIT 200"
        ).fetchall()
        any_cluster = conn.execute("SELECT COUNT(*) FROM cluster").fetchone()[0]
        return templates.TemplateResponse(
            request,
            "clusters_list.html",
            {"clusters": rows, "total": len(rows), "none_yet": any_cluster == 0},
        )

    @app.get("/clusters/{cluster_id}", response_class=HTMLResponse)
    def cluster_detail(
        request: Request,
        cluster_id: int,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        cluster = conn.execute(
            "SELECT * FROM cluster WHERE id = ?", (cluster_id,)
        ).fetchone()
        if cluster is None:
            raise HTTPException(404, "no such cluster")
        members = conn.execute(
            "SELECT m.instance_id, m.role, m.score, m.score_breakdown, i.source,"
            " i.rel_path, i.width, i.height, i.size_bytes"
            " FROM cluster_member m JOIN instance i ON i.id = m.instance_id"
            " WHERE m.cluster_id = ? ORDER BY m.score DESC NULLS LAST",
            (cluster_id,),
        ).fetchall()
        return templates.TemplateResponse(
            request,
            "cluster_detail.html",
            {"cluster": cluster, "members": members},
        )

    @app.post("/clusters/{cluster_id}/action")
    def cluster_action(
        cluster_id: int,
        action: str = Form(...),
        winner: int | None = Form(None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        try:
            if action == "accept":
                actions.cluster_accept(conn, cluster_id)
            elif action == "swap":
                if winner is None:
                    raise actions.ReviewError("select a member to make the winner")
                actions.cluster_swap_winner(conn, cluster_id, winner)
            elif action == "split":
                if winner is None:
                    raise actions.ReviewError("select a member to split out")
                actions.cluster_split(conn, cluster_id, winner)
            elif action == "not_duplicate":
                actions.cluster_not_duplicate(conn, cluster_id)
            else:
                raise actions.ReviewError(f"unknown action: {action}")
        except actions.ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/clusters", status_code=303)

    @app.post("/clusters/bulk")
    def clusters_bulk(
        action: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        if action == "prefer_curated":
            actions.bulk_prefer_curated(conn)
        elif action == "accept_all":
            actions.bulk_accept_pending(conn)
        else:
            raise HTTPException(400, f"unknown bulk action: {action}")
        return RedirectResponse("/clusters", status_code=303)

    @app.get("/thumb/{instance_id}")
    def thumb(
        instance_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Response:
        cached = thumbs_dir / f"{instance_id}.jpg"
        if cached.is_file():
            return Response(cached.read_bytes(), media_type="image/jpeg")
        row = conn.execute(
            "SELECT source, rel_path, kind FROM instance WHERE id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no such instance")
        roots = _source_roots(conn)
        source_path = roots.get(row["source"], Path("/nonexistent")) / row["rel_path"]
        preview = _load_preview(source_path, row["kind"])
        if preview is None:
            return Response(_placeholder_jpeg(), media_type="image/jpeg")
        preview.thumbnail((_THUMB_EDGE, _THUMB_EDGE))
        buf = io.BytesIO()
        preview.save(buf, format="JPEG", quality=82)
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(buf.getvalue())
        return Response(buf.getvalue(), media_type="image/jpeg")

    return app


def _load_preview(source_path: Path, kind: str) -> Image.Image | None:
    """A display-ready RGB preview of an image or video, or None if unavailable.

    Images are EXIF-orientation-corrected; videos yield an extracted frame.
    """
    if not source_path.is_file():
        return None
    if kind == "image":
        try:
            with Image.open(source_path) as img:
                return ImageOps.exif_transpose(img).convert("RGB")
        except Exception:
            return None
    if kind == "video":
        frame = extract_frame(source_path)
        return frame.convert("RGB") if frame is not None else None
    return None
