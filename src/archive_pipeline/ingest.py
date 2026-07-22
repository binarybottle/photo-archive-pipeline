"""Stage 1 — Ingest: complete inventory of every file in a source (spec §8).

For each file: size, streamed SHA-256, signature-based MIME, kind classification,
full EXIF dump (exiftool stay-open batch), perceptual hashes for images
(orientation-normalized, downscaled), and keyframe signatures for videos.

INV-1: sources are only ever read (``rb``); ingest refuses to run as root and
warns when the source root is writable. INV-5: resumable — files already
cataloged with matching size+mtime are skipped; changed files are re-processed
in place (same row id); each batch commits atomically.

Usage:
    >>> from archive_pipeline.ingest import ingest_source
    >>> summary = ingest_source(conn, cfg, "LOCAL", Path("/photos"), run_id, log)  # doctest: +SKIP
    >>> summary.catalog_count == summary.discovered  # doctest: +SKIP
    True
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import Any

from archive_pipeline.config import Config
from archive_pipeline.hashing import FileFacts, process_file, sha256_file
from archive_pipeline.metadata import ExtractedMeta, MetadataReader, extract_fields
from archive_pipeline.video import VideoFacts, video_signature

_BATCH_SIZE = 500
_SAMPLE_SIZE = 100

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".heic", ".heif", ".webp",
     ".bmp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf", ".rw2"}
)
VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".m4v", ".avi", ".mts", ".m2ts", ".3gp", ".mpg", ".mpeg",
     ".wmv", ".webm", ".mkv"}
)


class IngestError(Exception):
    """Raised for INV-1 violations, missing roots, or failed verification."""


@dataclass(frozen=True)
class WalkEntry:
    """One regular file found under the source root."""

    rel_path: str  # NFC-normalized POSIX relative path (catalog key)
    abs_path: Path  # as found on disk, used for all I/O
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class IngestSummary:
    """Outcome of one ingest run over one source."""

    source_id: str
    root: Path
    discovered: int
    processed: int
    skipped_unchanged: int
    missing_from_disk: int
    corrupt: int
    by_kind: dict[str, int]
    catalog_count: int
    sample_checked: int


def classify_kind(mime: str | None, rel_path: str) -> tuple[str, list[str]]:
    """Classify a file into the catalog's ``kind`` from signature MIME.

    Extension is only a fallback when the signature yields nothing (flagged).

    Usage:
        >>> classify_kind("image/jpeg", "a.jpg")
        ('image', [])
        >>> classify_kind(None, "a.jpg")
        ('image', ['mime_from_extension'])
        >>> classify_kind("application/json", "a.jpg.json")
        ('sidecar_json', [])
    """
    ext = Path(rel_path).suffix.lower()
    if mime is None:
        if ext == ".json":
            return "sidecar_json", ["mime_from_extension"]
        if ext == ".xmp":
            return "sidecar_xmp", ["mime_from_extension"]
        if ext in IMAGE_EXTENSIONS:
            return "image", ["mime_from_extension"]
        if ext in VIDEO_EXTENSIONS:
            return "video", ["mime_from_extension"]
        return "other", []
    if mime == "application/json":
        return "sidecar_json", []
    if ext == ".xmp" or mime == "application/rdf+xml":
        return "sidecar_xmp", []
    if mime.startswith("image/"):
        return "image", []
    if mime.startswith("video/"):
        return "video", []
    return "other", []


def walk_source(root: Path) -> list[WalkEntry]:
    """List every regular file under ``root``, following no symlinks (INV-1).

    Paths are stored NFC-normalized (edge case 14) but I/O uses them as found.

    Usage:
        >>> entries = walk_source(Path("/photos"))  # doctest: +SKIP
        >>> entries[0].rel_path  # doctest: +SKIP
        '1998/beach_001.jpg'
    """
    entries: list[WalkEntry] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            abs_path = Path(dirpath) / name
            if abs_path.is_symlink():
                continue
            stat = abs_path.stat()
            rel = unicodedata.normalize("NFC", abs_path.relative_to(root).as_posix())
            entries.append(
                WalkEntry(
                    rel_path=rel,
                    abs_path=abs_path,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                )
            )
    return entries


def _guard_inv1(root: Path, log: Logger) -> None:
    """Refuse to run as root; warn when the source root is writable."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise IngestError("refusing to run as root (INV-1: read-only sources)")
    if not root.is_dir():
        raise IngestError(f"source root is not a directory: {root}")
    if os.access(root, os.W_OK):
        log.warning(
            "source root is writable by this user; the pipeline never writes to it,"
            " but a read-only mount would enforce INV-1 mechanically",
            extra={"root": str(root)},
        )


def _build_row(
    source_id: str,
    entry: WalkEntry,
    meta: ExtractedMeta,
    facts: FileFacts,
    video: VideoFacts | None,
    run_id: int,
) -> dict[str, Any]:
    """Assemble one catalog `instance` row from the per-file results."""
    kind, flags = classify_kind(meta.mime, entry.rel_path)
    if meta.error:
        flags.append("unreadable_metadata")
    if entry.size_bytes == 0:
        flags.append("zero_byte")
    image = facts.image
    if image is not None and image.corrupt:
        flags.append("corrupt")
    if video is not None and video.flag:
        flags.append(video.flag)
    width = (image.width if image else None) or (video.width if video else None) or meta.width
    height = (image.height if image else None) or (video.height if video else None) or meta.height
    return {
        "source": source_id,
        "rel_path": entry.rel_path,
        "size_bytes": entry.size_bytes,
        "sha256": facts.sha256,
        "mime": meta.mime,
        "kind": kind,
        "width": width,
        "height": height,
        "duration_s": video.duration_s if video else None,
        "phash": image.phash if image else None,
        "dhash": image.dhash if image else None,
        "video_sig": video.video_sig if video else None,
        "exif_json": meta.exif_json,
        "exif_dto": meta.exif_dto,
        "gps_lat": meta.gps_lat,
        "gps_lon": meta.gps_lon,
        "exif_tag_count": meta.exif_tag_count,
        "camera_make": meta.camera_make,
        "camera_model": meta.camera_model,
        "ingest_run_id": run_id,
        "mtime_ns": entry.mtime_ns,
        "flags": json.dumps(flags) if flags else None,
    }


_UPSERT_SQL = """
INSERT INTO instance (
  source, rel_path, size_bytes, sha256, mime, kind, width, height, duration_s,
  phash, dhash, video_sig, exif_json, exif_dto, gps_lat, gps_lon,
  exif_tag_count, camera_make, camera_model, ingest_run_id, mtime_ns, flags
) VALUES (
  :source, :rel_path, :size_bytes, :sha256, :mime, :kind, :width, :height,
  :duration_s, :phash, :dhash, :video_sig, :exif_json, :exif_dto, :gps_lat,
  :gps_lon, :exif_tag_count, :camera_make, :camera_model, :ingest_run_id,
  :mtime_ns, :flags
)
ON CONFLICT(source, rel_path) DO UPDATE SET
  size_bytes = excluded.size_bytes, sha256 = excluded.sha256,
  mime = excluded.mime, kind = excluded.kind, width = excluded.width,
  height = excluded.height, duration_s = excluded.duration_s,
  phash = excluded.phash, dhash = excluded.dhash,
  video_sig = excluded.video_sig, exif_json = excluded.exif_json,
  exif_dto = excluded.exif_dto, gps_lat = excluded.gps_lat,
  gps_lon = excluded.gps_lon, exif_tag_count = excluded.exif_tag_count,
  camera_make = excluded.camera_make, camera_model = excluded.camera_model,
  ingest_run_id = excluded.ingest_run_id, mtime_ns = excluded.mtime_ns,
  flags = excluded.flags
"""


def _sample_verify(
    conn: sqlite3.Connection, source_id: str, entries: list[WalkEntry], run_id: int, log: Logger
) -> int:
    """Re-hash a random sample and compare against the catalog; raise on mismatch."""
    if not entries:
        return 0
    rng = random.Random(run_id)
    sample = rng.sample(entries, min(_SAMPLE_SIZE, len(entries)))
    mismatches: list[str] = []
    for entry in sample:
        row = conn.execute(
            "SELECT sha256 FROM instance WHERE source = ? AND rel_path = ?",
            (source_id, entry.rel_path),
        ).fetchone()
        if row is None or sha256_file(entry.abs_path) != row["sha256"]:
            mismatches.append(entry.rel_path)
    if mismatches:
        raise IngestError(
            f"sample hash verification failed for {len(mismatches)} file(s):"
            f" {', '.join(mismatches[:5])}"
        )
    log.info("sample hash verification ok", extra={"sample_size": len(sample)})
    return len(sample)


def _chunks(seq: list[WalkEntry], size: int) -> list[list[WalkEntry]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def ingest_source(
    conn: sqlite3.Connection,
    cfg: Config,
    source_id: str,
    root: Path,
    run_id: int,
    log: Logger,
) -> IngestSummary:
    """Inventory ``root`` into the catalog as ``source_id``; return a summary.

    Usage:
        >>> summary = ingest_source(conn, cfg, "TAKEOUT:t1", root, run_id, log)  # doctest: +SKIP
    """
    root = root.resolve()
    _guard_inv1(root, log)

    entries = walk_source(root)
    existing = {
        row["rel_path"]: row
        for row in conn.execute(
            "SELECT rel_path, size_bytes, mtime_ns FROM instance WHERE source = ?",
            (source_id,),
        )
    }
    to_process = [
        e
        for e in entries
        if e.rel_path not in existing
        or existing[e.rel_path]["size_bytes"] != e.size_bytes
        or existing[e.rel_path]["mtime_ns"] != e.mtime_ns
    ]
    on_disk = {e.rel_path for e in entries}
    missing = sorted(set(existing) - on_disk)
    if missing:
        log.warning(
            "cataloged files no longer present on disk (sources should never change)",
            extra={"count": len(missing), "examples": missing[:5]},
        )
    log.info(
        "walk complete",
        extra={
            "source": source_id,
            "discovered": len(entries),
            "to_process": len(to_process),
            "skipped_unchanged": len(entries) - len(to_process),
        },
    )

    corrupt = 0
    workers = cfg.runtime.parallelism or None
    sequential = cfg.runtime.parallelism == 1 or len(to_process) < 8
    with MetadataReader() as reader:
        for index, batch in enumerate(_chunks(to_process, _BATCH_SIZE)):
            metas = [extract_fields(d) for d in reader.read_batch([e.abs_path for e in batch])]
            kinds = [
                classify_kind(m.mime, e.rel_path)[0]
                for e, m in zip(batch, metas, strict=True)
            ]
            paths = [str(e.abs_path) for e in batch]
            is_image = [k == "image" for k in kinds]
            if sequential:
                facts = [process_file(p, im) for p, im in zip(paths, is_image, strict=True)]
            else:
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    facts = list(pool.map(process_file, paths, is_image, chunksize=16))
            videos = [
                video_signature(e.abs_path) if k == "video" else None
                for e, k in zip(batch, kinds, strict=True)
            ]
            rows = [
                _build_row(source_id, e, m, f, v, run_id)
                for e, m, f, v in zip(batch, metas, facts, videos, strict=True)
            ]
            corrupt += sum(1 for r in rows if r["flags"] and "corrupt" in r["flags"])
            with conn:
                conn.executemany(_UPSERT_SQL, rows)
            log.info(
                "batch ingested",
                extra={"batch": index + 1, "files": len(rows), "total_done": min(
                    (index + 1) * _BATCH_SIZE, len(to_process)
                )},
            )

    sample_checked = _sample_verify(conn, source_id, entries, run_id, log)
    catalog_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM instance WHERE source = ?", (source_id,)
        ).fetchone()[0]
    )
    by_kind = {
        row["kind"]: row["n"]
        for row in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM instance WHERE source = ? GROUP BY kind",
            (source_id,),
        )
    }
    if catalog_count != len(entries) + len(missing):
        raise IngestError(
            f"acceptance failure: catalog holds {catalog_count} rows for {source_id}"
            f" but the filesystem walk found {len(entries)}"
        )
    summary = IngestSummary(
        source_id=source_id,
        root=root,
        discovered=len(entries),
        processed=len(to_process),
        skipped_unchanged=len(entries) - len(to_process),
        missing_from_disk=len(missing),
        corrupt=corrupt,
        by_kind=by_kind,
        catalog_count=catalog_count,
        sample_checked=sample_checked,
    )
    log.info("ingest complete", extra={"summary": summary.__dict__ | {"root": str(root)}})
    return summary
