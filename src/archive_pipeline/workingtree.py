"""Working-tree layout: the pipeline-owned directory tree (spec section 4).

Everything the pipeline writes lives under one root:

    catalog.db, config.toml, archive/, quarantine/, review/, reports/, logs/

Usage:
    >>> from pathlib import Path
    >>> from archive_pipeline.workingtree import WorkingTree
    >>> wt = WorkingTree(Path("/archive-project"))
    >>> wt.catalog_path.name
    'catalog.db'
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from archive_pipeline.config import default_config_toml


@dataclass(frozen=True)
class WorkingTree:
    """Path helpers for a working-tree root; creates nothing by itself."""

    root: Path

    @property
    def catalog_path(self) -> Path:
        return self.root / "catalog.db"

    @property
    def config_path(self) -> Path:
        return self.root / "config.toml"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def quarantine_dir(self) -> Path:
        return self.root / "quarantine"

    @property
    def review_dir(self) -> Path:
        return self.root / "review"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def subdirs(self) -> tuple[Path, ...]:
        return (
            self.archive_dir,
            self.quarantine_dir,
            self.review_dir,
            self.reports_dir,
            self.logs_dir,
        )


def init_working_tree(root: Path) -> tuple[WorkingTree, bool]:
    """Create the working-tree layout; return (tree, config_was_created).

    Idempotent: existing directories are kept and an existing ``config.toml`` is
    never overwritten (it may hold the user's edits).

    Usage:
        >>> wt, created = init_working_tree(Path("/archive-project"))  # doctest: +SKIP
        >>> created  # doctest: +SKIP
        True
    """
    wt = WorkingTree(root)
    wt.root.mkdir(parents=True, exist_ok=True)
    for sub in wt.subdirs:
        sub.mkdir(exist_ok=True)
    config_created = not wt.config_path.exists()
    if config_created:
        wt.config_path.write_text(default_config_toml(), encoding="utf-8")
    return wt, config_created
