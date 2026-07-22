"""Disk-space preflight (INV-9): refuse bulk writes without a safety margin.

Every stage that writes bulk data computes the bytes it will write, checks free
space on the destination volume, and refuses to start unless free space exceeds
the estimate by ``space.margin_pct``.

Usage:
    >>> from pathlib import Path
    >>> from archive_pipeline.space import preflight
    >>> check = preflight(Path("."), bytes_needed=1024, margin_pct=15.0)
    >>> check.ok
    True
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


class SpaceError(Exception):
    """Raised when a destination volume lacks the required free space."""


@dataclass(frozen=True)
class SpaceCheck:
    """Outcome of a preflight: what was needed (with margin) vs. what is free."""

    dest: Path
    bytes_needed: int
    bytes_required: int  # bytes_needed plus the safety margin
    bytes_free: int

    @property
    def ok(self) -> bool:
        return self.bytes_free >= self.bytes_required


def preflight(dest: Path, bytes_needed: int, margin_pct: float) -> SpaceCheck:
    """Check free space on ``dest``'s volume against ``bytes_needed`` + margin.

    Usage:
        >>> preflight(Path("."), 0, 15.0).ok
        True
    """
    free = shutil.disk_usage(dest).free
    required = int(bytes_needed * (1.0 + margin_pct / 100.0))
    return SpaceCheck(
        dest=dest, bytes_needed=bytes_needed, bytes_required=required, bytes_free=free
    )


def require_space(dest: Path, bytes_needed: int, margin_pct: float) -> SpaceCheck:
    """Like :func:`preflight` but raise :class:`SpaceError` when insufficient.

    Usage:
        >>> require_space(Path("."), 0, 15.0).ok
        True
    """
    check = preflight(dest, bytes_needed, margin_pct)
    if not check.ok:
        raise SpaceError(
            f"not enough free space on {dest}: need {check.bytes_required:,} bytes"
            f" ({check.bytes_needed:,} + {margin_pct}% margin),"
            f" only {check.bytes_free:,} free"
        )
    return check
