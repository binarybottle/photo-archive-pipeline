"""SQLite catalog: connection settings, schema, and migrations (spec section 7).

The catalog is the single source of truth for the pipeline. Connections use WAL
mode and enforce foreign keys. Schema changes are versioned migrations tracked in
``PRAGMA user_version``; applying migrations is idempotent.

Usage:
    >>> from pathlib import Path
    >>> from archive_pipeline.catalog import open_catalog, schema_version
    >>> conn = open_catalog(Path("catalog.db"))  # doctest: +SKIP
    >>> schema_version(conn)  # doctest: +SKIP
    1
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_V1 = """
-- One row per physical file discovered in any source.
CREATE TABLE instance (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,             -- 'LOCAL' | 'TAKEOUT:<export-id>'
  rel_path TEXT NOT NULL,           -- path relative to source root
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  mime TEXT,                        -- from file signature, not extension
  kind TEXT,                        -- image | video | sidecar_json | sidecar_xmp | other
  width INTEGER, height INTEGER,
  duration_s REAL,                  -- videos
  phash TEXT, dhash TEXT,           -- images; NULL for videos
  video_sig TEXT,                   -- videos: duration+resolution+keyframe-hash signature
  exif_json TEXT,                   -- full exiftool dump (JSON)
  exif_dto TEXT,                    -- DateTimeOriginal as found (ISO, may be NULL)
  gps_lat REAL, gps_lon REAL,
  exif_tag_count INTEGER,           -- populated-tag richness metric
  camera_make TEXT, camera_model TEXT,
  ingest_run_id INTEGER NOT NULL,
  UNIQUE(source, rel_path)
);
CREATE INDEX idx_instance_sha ON instance(sha256);
CREATE INDEX idx_instance_phash ON instance(phash);

-- Takeout JSON sidecar linkage and parsed content.
CREATE TABLE takeout_sidecar (
  instance_id INTEGER REFERENCES instance(id),   -- the JSON file
  media_instance_id INTEGER REFERENCES instance(id), -- matched media file (nullable until matched)
  photo_taken_time TEXT,            -- ISO from JSON
  gps_lat REAL, gps_lon REAL,
  description TEXT,
  title TEXT,                       -- original filename per Google
  match_method TEXT                 -- exact | truncation | numbered | manual | unmatched
);

-- Date candidates and resolution, one row per instance.
CREATE TABLE date_resolution (
  instance_id INTEGER PRIMARY KEY REFERENCES instance(id),
  cand_exif TEXT, cand_folder TEXT, cand_takeout TEXT, cand_filename TEXT,
  folder_precision TEXT,            -- day | month | year | NULL
  exif_flags TEXT,                  -- JSON list: epoch_default, camera_default,
                                    -- mass_identical, predates_camera, scanner_date, ...
  resolved_date TEXT,               -- ISO
  resolved_precision TEXT,          -- second | day | month | year
  resolved_source TEXT,             -- exif | folder | takeout_json | filename | review
  status TEXT NOT NULL DEFAULT 'pending', -- pending | auto | reviewed | conflict
  confidence REAL
);

-- Near-duplicate clustering.
CREATE TABLE cluster (
  id INTEGER PRIMARY KEY,
  kind TEXT,                        -- exact | near_image | near_video | pair_raw_jpeg | pair_live
  status TEXT NOT NULL DEFAULT 'pending', -- pending | auto | reviewed
  winner_instance_id INTEGER REFERENCES instance(id)
);
CREATE TABLE cluster_member (
  cluster_id INTEGER REFERENCES cluster(id),
  instance_id INTEGER REFERENCES instance(id),
  score REAL, score_breakdown TEXT, -- JSON of per-component scores
  role TEXT,                        -- winner | loser | companion (RAW/Live partner kept)
  PRIMARY KEY (cluster_id, instance_id)
);

-- Append-only decision log.
CREATE TABLE decision (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  stage TEXT NOT NULL,
  subject TEXT NOT NULL,            -- 'instance:<id>' | 'cluster:<id>'
  rule TEXT NOT NULL,               -- machine-readable rule identifier
  detail TEXT,                      -- JSON payload
  actor TEXT NOT NULL               -- 'auto' | 'review:user'
);

-- Materialization ledger.
CREATE TABLE placement (
  instance_id INTEGER PRIMARY KEY REFERENCES instance(id),
  disposition TEXT NOT NULL,        -- archive | quarantine | excluded
  dest_rel_path TEXT,               -- within archive/ or quarantine/
  dest_sha256 TEXT,                 -- post-metadata-write hash (archive)
  copied_ok INTEGER, verified_ok INTEGER
);

CREATE TABLE run (
  id INTEGER PRIMARY KEY, stage TEXT, started TEXT, finished TEXT,
  args_json TEXT, git_rev TEXT, status TEXT
);
"""


@dataclass(frozen=True)
class Migration:
    """One schema migration, applied when ``user_version`` is below ``version``."""

    version: int
    description: str
    sql: str


SCHEMA_V2 = """
-- Ingest resumability (skip on matching size+mtime) and per-instance flags
-- (JSON list: corrupt, zero_byte, mime_from_extension, no_ffprobe, ...).
ALTER TABLE instance ADD COLUMN mtime_ns INTEGER;
ALTER TABLE instance ADD COLUMN flags TEXT;
"""

SCHEMA_V3 = """
-- Takeout normalization (Stage 2).
ALTER TABLE takeout_sidecar ADD COLUMN creation_time TEXT;  -- upload-artifact heuristic (R4)
ALTER TABLE instance ADD COLUMN google_recompressed INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX idx_sidecar_instance ON takeout_sidecar(instance_id);

-- Where each source was ingested from; later stages read source files by this root.
CREATE TABLE source_root (
  source TEXT PRIMARY KEY,
  root TEXT NOT NULL,
  registered TEXT NOT NULL
);

-- Takeout album-folder memberships: topical keyword candidates (Stage 6).
CREATE TABLE album_membership (
  media_instance_id INTEGER NOT NULL REFERENCES instance(id),
  album TEXT NOT NULL,
  album_dir TEXT NOT NULL,
  PRIMARY KEY (media_instance_id, album_dir)
);

-- Google "-edited" versions linked to their originals (edge case 2).
CREATE TABLE edited_pair (
  edited_instance_id INTEGER PRIMARY KEY REFERENCES instance(id),
  original_instance_id INTEGER NOT NULL REFERENCES instance(id)
);
"""

SCHEMA_V4 = """
-- Stage 2b: per-directory Takeout-derivation classification of LOCAL.
ALTER TABLE instance ADD COLUMN effective_trust TEXT;  -- curated | takeout
CREATE TABLE local_provenance (
  source TEXT NOT NULL,
  dir_path TEXT NOT NULL,           -- relative directory ('' = source root)
  media_count INTEGER NOT NULL,
  sidecar_count INTEGER NOT NULL,
  signal REAL NOT NULL,             -- 0..1 Takeout-derivation signal
  signals_json TEXT,                -- component breakdown
  classification TEXT NOT NULL,     -- curated | takeout_derived
  override TEXT,                    -- NULL | config:curated | config:takeout_derived
  PRIMARY KEY (source, dir_path)
);
"""

SCHEMA_V5 = """
-- Review UI (Stage 4): intra-day ordering for scanned batches, written to
-- XMP-ArchivePipe:SequenceHint at materialize.
ALTER TABLE date_resolution ADD COLUMN sequence_hint INTEGER;
"""

SCHEMA_V6 = """
-- Stage 5: field-level metadata merge planned per cluster, applied by
-- materialize onto the winner.
CREATE TABLE cluster_merge (
  cluster_id INTEGER PRIMARY KEY REFERENCES cluster(id),
  merged_json TEXT NOT NULL,        -- date/gps/descriptions/titles/keywords + provenance
  needs_review INTEGER NOT NULL DEFAULT 0
);
"""

SCHEMA_V7 = """
-- Review UI: an explicit archive bucket for photos the user files coarsely
-- (e.g. 'pre-2000' for old, undateable images), distinct from unresolved
-- 'undated'. NULL for normal dated/undated resolutions.
ALTER TABLE date_resolution ADD COLUMN bucket TEXT;
"""

SCHEMA_V8 = """
-- Review UI: hide a conflict from the active queue without resolving it, so a
-- reviewer can defer folders they aren't ready to decide.
ALTER TABLE date_resolution ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0;
"""

SCHEMA_V9 = """
-- The date-review queue tests each conflict for cluster membership by
-- instance_id; the cluster_member primary key leads with cluster_id, so that
-- lookup was a full scan. Index the reverse direction. IF NOT EXISTS keeps the
-- statement safe if two connections cross this migration boundary at once (the
-- review app opens the catalog per request).
CREATE INDEX IF NOT EXISTS idx_cluster_member_instance ON cluster_member(instance_id);
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial schema (spec section 7)", SCHEMA_V1),
    Migration(2, "instance.mtime_ns and instance.flags for ingest", SCHEMA_V2),
    Migration(3, "takeout normalization tables (Stage 2)", SCHEMA_V3),
    Migration(4, "local provenance classification (Stage 2b)", SCHEMA_V4),
    Migration(5, "date_resolution.sequence_hint for review (Stage 4)", SCHEMA_V5),
    Migration(6, "cluster_merge for dedup metadata merging (Stage 5)", SCHEMA_V6),
    Migration(7, "date_resolution.bucket for coarse archive buckets", SCHEMA_V7),
    Migration(8, "date_resolution.skipped to hide conflicts from the queue", SCHEMA_V8),
    Migration(9, "index cluster_member(instance_id) for the date-review queue", SCHEMA_V9),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def connect(db_path: Path, *, cross_thread: bool = False) -> sqlite3.Connection:
    """Open the catalog with WAL mode and foreign keys enforced.

    ``cross_thread=True`` disables sqlite's same-thread check for consumers
    (the review app) whose framework may hand one request's connection between
    worker threads; each connection is still used by one request at a time.

    Usage:
        >>> conn = connect(Path("catalog.db"))  # doctest: +SKIP
        >>> conn.execute("PRAGMA foreign_keys").fetchone()[0]  # doctest: +SKIP
        1
    """
    conn = sqlite3.connect(db_path, check_same_thread=not cross_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the catalog's current schema version (0 for a fresh database)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending migrations atomically; return the versions applied.

    Re-running against an up-to-date catalog is a no-op (INV-5).

    Usage:
        >>> conn = connect(Path("catalog.db"))  # doctest: +SKIP
        >>> migrate(conn)  # doctest: +SKIP
        [1]
        >>> migrate(conn)  # doctest: +SKIP
        []
    """
    applied: list[int] = []
    for migration in MIGRATIONS:
        if schema_version(conn) >= migration.version:
            continue
        conn.executescript(
            "BEGIN;\n"
            f"{migration.sql}\n"
            f"PRAGMA user_version = {migration.version};\n"
            "COMMIT;"
        )
        applied.append(migration.version)
    return applied


def open_catalog(db_path: Path, *, cross_thread: bool = False) -> sqlite3.Connection:
    """Connect to the catalog and bring its schema up to date.

    Usage:
        >>> conn = open_catalog(Path("catalog.db"))  # doctest: +SKIP
    """
    conn = connect(db_path, cross_thread=cross_thread)
    migrate(conn)
    return conn
