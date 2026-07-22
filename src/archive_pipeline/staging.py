"""Takeout zip staging: one-time extraction into the working tree (spec Stage 1).

Zips are extracted into ``staging/<export-id>/`` after an INV-9 space preflight.
Extraction is crash-safe: content lands in a ``.partial`` directory that is
atomically renamed on success and discarded (pipeline-owned) on re-run. The
extracted tree is thereafter treated as a read-only source.

Usage:
    >>> from pathlib import Path
    >>> from archive_pipeline.staging import stage_takeout_zip
    >>> root = stage_takeout_zip(Path("takeout.zip"), Path("staging"),
    ...                          export_id="takeout-2026-07", margin_pct=15.0)  # doctest: +SKIP
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from archive_pipeline.space import require_space


def complete_marker(staging_root: Path, export_id: str) -> Path:
    """Marker recording a finished extraction; lives *beside* the extracted
    tree so pipeline bookkeeping is never cataloged as source content."""
    return staging_root / f"{export_id}.extraction-complete"


class StagingError(Exception):
    """Raised for unreadable zips or staging-directory conflicts."""


def stage_takeout_zip(
    zip_path: Path, staging_root: Path, export_id: str, margin_pct: float
) -> Path:
    """Extract ``zip_path`` into ``staging_root/export_id``; return that root.

    Idempotent: a completed extraction (marker present) is returned as-is; a
    partial one from an interrupted run is discarded and redone.

    Usage:
        >>> stage_takeout_zip(Path("t.zip"), Path("staging"), "t", 15.0)  # doctest: +SKIP
        PosixPath('staging/t')
    """
    if not zipfile.is_zipfile(zip_path):
        raise StagingError(f"not a readable zip archive: {zip_path}")
    dest = staging_root / export_id
    marker = complete_marker(staging_root, export_id)
    if marker.exists() and dest.is_dir():
        return dest
    if dest.exists():
        raise StagingError(
            f"staging dir {dest} exists without a completion marker; remove it to re-extract"
        )
    partial = staging_root / f"{export_id}.partial"
    if partial.exists():
        shutil.rmtree(partial)

    staging_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        total_bytes = sum(info.file_size for info in zf.infolist())
        require_space(staging_root, total_bytes, margin_pct)
        partial.mkdir()
        zf.extractall(partial)
    partial.rename(dest)
    marker.touch()
    return dest
