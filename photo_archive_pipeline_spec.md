# Photo Archive Consolidation Pipeline: Design Specification

Version 1.0 (2026-07-22). This document is the implementation contract for Claude Code.
It specifies a non-destructive pipeline that consolidates ~30 years of photos and videos
from (a) a locally organized folder tree and (b) one or more Google Takeout exports into
a single canonical, date-organized archive with embedded metadata, controlled
deduplication, and verifiable zero-loss guarantees.

---

## 1. Objectives

1. Produce one canonical archive tree, organized by resolved capture date, containing
   exactly one "winner" copy of each distinct photo/video.
2. Preserve every byte of source material until the user explicitly authorizes purge:
   losers are quarantined, never deleted.
3. Reconcile dates using a trust hierarchy in which the user's painstaking folder-based
   dating outranks untrustworthy EXIF and Google metadata.
4. Embed all resolved metadata (dates, GPS, captions, topical keywords, provenance)
   into the archived files themselves via EXIF/XMP, so the curation survives any future
   migration or software change.
5. Convert topical folder organization into XMP keywords (and later, album references in
   a photo manager), eliminating copy-based duplication permanently.
6. Every automated decision is logged, auditable, reversible, and human-overridable.

## 2. Non-goals

- No editing of image content (no rotation baking, no re-encoding, no format conversion).
- No cloud services. Everything runs locally.
- No fully automatic resolution of ambiguous cases; those go to a review queue.
- The pipeline does not modify Google Photos itself; it only consumes Takeout exports.

## 3. Ironclad invariants (enforced in code, tested in CI)

These are hard requirements. Any implementation that violates one is wrong.

- **INV-1 Read-only sources.** Source directories are opened read-only. The pipeline
  never writes, renames, touches mtimes, or deletes anything under a source root. On
  POSIX, the ingest step verifies it cannot write (and refuses to run as root).
- **INV-2 Copy-then-modify.** Metadata is only ever written to copies inside the working
  tree, never to source files.
- **INV-3 Conservation law.** After materialization, for every file in the source
  inventory, its SHA-256 must be locatable in exactly one of: archive, quarantine, or
  the "intentionally excluded" table (e.g., Takeout JSON sidecars, thumbnails). The
  `verify` stage proves this programmatically and fails loudly otherwise.
- **INV-4 Dry-run first.** Every mutating stage supports `--dry-run` (default ON) that
  produces the full decision log without touching disk. Mutation requires an explicit
  `--execute` flag.
- **INV-5 Idempotent and resumable.** Every stage can be interrupted (power loss, Ctrl-C)
  and re-run without corruption or duplicated work. State lives in SQLite with WAL mode;
  file operations are copy-to-temp-then-atomic-rename.
- **INV-6 Append-only decision log.** Decisions (winner selection, date resolution,
  metadata merges) are recorded with inputs, rule fired, and timestamp. Overrides append;
  they never overwrite history.
- **INV-7 No silent quality loss.** Files are copied bit-identically (verified by
  re-hash after copy). Metadata writes to JPEG/HEIC/PNG/TIFF use exiftool in a mode that
  rewrites only metadata blocks; for RAW formats and any format where in-place metadata
  writing is risky, an XMP sidecar is written instead and the image bytes are untouched.
- **INV-8 Human gate on ambiguity.** Confidence thresholds are conservative. Anything
  below threshold queues for review rather than auto-resolving.
- **INV-9 Disk-space preflight.** Every stage that writes bulk data (Takeout zip
  extraction in ingest, materialize, quarantine copying) first computes the bytes it
  will write from the catalog, checks free space on the destination volume, and refuses
  to start unless free space exceeds the estimate by a configurable margin
  (`space.margin_pct`, default 15%). During long copy runs it re-checks periodically
  (every `space.recheck_gb`, default 25 GB written) and pauses cleanly, resumable, if
  the margin is breached. Dry-run reports the space estimate per destination volume so
  the user sees requirements before executing.

## 4. Inputs and terminology

- **LOCAL**: the user's existing organized tree on the local drive (dated and topical
  subfolders, possibly overlapping or inconsistent). Important: LOCAL contains material
  from multiple prior Google Takeout extractions ingested over the years in different
  forms, so parts of LOCAL are Google-derived and must not automatically receive
  curated-local trust (see Stage 2b).
- **TAKEOUT**: one or more Google Takeout exports of Google Photos (zip archives or
  extracted trees), containing media files, `*.json` sidecars, album folders (topical)
  and "Photos from YYYY" folders (dated), with known pathologies (section 11).
- **Item**: one logical photo or video, possibly represented by multiple physical files
  across sources (duplicates, near-duplicates, RAW+JPEG pairs, Live Photo pairs).
- **Instance**: one physical file.
- **Working tree**: pipeline-owned directory containing the catalog, staging area,
  archive, quarantine, and reports.

Working tree layout:

```
/archive-project/
  catalog.db              # SQLite, WAL mode. Single source of truth.
  config.toml             # All tunables (thresholds, paths, policies).
  archive/                # Canonical output: YYYY/YYYY-MM/<files>
  quarantine/             # Losers, preserved verbatim, mirrored by content hash
  review/                 # Exports for the review UI (thumbnails etc.)
  reports/                # Manifests, decision logs (CSV/JSONL), verify reports
  logs/                   # Structured run logs (JSONL)
```

## 5. System architecture

A staged CLI application. Each stage reads and writes the catalog; file mutation happens
only in the materialize stage. Stages are independently runnable and re-runnable.

```
ingest -> takeout-normalize -> date-resolve -> [review] -> dedup -> [review]
       -> materialize -> verify -> (maintain)
```

CLI sketch (Typer or argparse; Typer preferred):

```
archive ingest --source LOCAL --root /path/to/local/photos
archive ingest --source TAKEOUT --root /path/to/takeout
archive takeout-normalize
archive date-resolve
archive review serve          # local FastAPI review UI on 127.0.0.1
archive dedup
archive materialize --dry-run # default
archive materialize --execute
archive verify
archive report                # human-readable summary of everything
archive maintain verify-checksums
archive maintain import --root /path/to/new/photos   # future incremental imports
```

## 6. Technology stack

- Python 3.12+, Poetry for dependency management, `src/` layout, type hints throughout,
  docstrings with usage examples at module and function level.
- **exiftool** (system dependency) driven via `pyexiftool` in stay-open batch mode: all
  metadata read/write. This is the only tool trusted to write metadata.
- **Pillow** + **pillow-heif**: image decoding for perceptual hashing and thumbnails.
- **imagehash**: perceptual hashes (pHash primary, dHash secondary).
- **ffprobe/ffmpeg** (system dependency): video stream metadata, duration, and keyframe
  extraction for video near-dup detection.
- **SQLite** (stdlib `sqlite3`), WAL mode, foreign keys ON. No ORM required; thin typed
  data-access layer.
- **FastAPI + Jinja2** for the local review UI (user is fluent in FastAPI).
- **pytest** + **hypothesis** for tests. `ruff` + `mypy` in pre-commit.
- Logging: `structlog` or stdlib logging with JSONL handler.

Custom exiftool XMP namespace (via an exiftool `.config` file), prefix `ArchivePipe`:

```
XMP-ArchivePipe:DateSource       # exif | folder | takeout_json | filename | review
XMP-ArchivePipe:DatePrecision    # second | day | month | year
XMP-ArchivePipe:MergedFrom       # semicolon-joined source-relative paths of merged instances
XMP-ArchivePipe:SourcePath       # source-relative path of the winning instance
XMP-ArchivePipe:PipelineVersion  # semver of the pipeline that wrote this file
XMP-ArchivePipe:SequenceHint     # optional intra-day ordering for scanned batches
```

## 7. Data model (SQLite)

```sql
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
```

## 8. Pipeline stages

### Stage 0: Preserve (manual, gated)

Before any pipeline run, the user makes a verbatim backup of LOCAL and TAKEOUT to a
separate physical disk. The pipeline's `ingest` refuses to run unless
`config.toml` contains `preserve.confirmed = true` set by the user, and it prints a
reminder of what that assertion means.

### Stage 1: Ingest

Purpose: build a complete inventory of every file in every source.

Algorithm:
1. Walk each source root (follow no symlinks). For each file: size, SHA-256 (streamed),
   MIME by signature (`python-magic` or exiftool), classify `kind`.
2. For images: extract full EXIF via exiftool batch mode into `exif_json`; compute
   pHash/dHash on a decoded, EXIF-orientation-normalized, downscaled copy. HEIC via
   pillow-heif. Unreadable/corrupt images get hashes NULL and a `corrupt` flag
   (they still obey the conservation law).
3. For videos: ffprobe stream info; `video_sig` = (rounded duration, resolution,
   pHash of 3 keyframes at 10/50/90%).
4. Takeout zips are extracted once into a pipeline-owned staging dir (or read in place
   if already extracted); the extraction dir is then treated as a read-only source.

Performance: hashing and decoding parallelized (process pool), exiftool in stay-open
batch mode. Progress bars, resumable (skip instances already in catalog with matching
size+mtime, re-hash on mismatch).

Acceptance: instance count equals filesystem count per source; spot-check 100 random
hashes recompute identically; re-running ingest is a no-op.

### Stage 2: Takeout normalization

Purpose: attach Google's JSON sidecar metadata to the right media instance and flag
Google-specific pathologies. Catalog-only; no file writes.

Sidecar matching, in order:
1. Exact: `IMG_1234.JPG.json` or `IMG_1234.JPG.supplemental-metadata.json` next to
   `IMG_1234.JPG`.
2. Truncation: Google truncates long base names in the JSON filename; match by prefix
   within the same directory.
3. Numbered duplicates: `IMG_1234(1).JPG` matches `IMG_1234.JPG(1).json` (Google's
   inconsistent placement of the `(n)`); handle both orderings.
4. Edited pairs: `IMG_1234-edited.JPG` shares the original's sidecar; record the
   edited/original relationship.
5. Unmatched sidecars and unmatched media are reported; unmatched media proceeds with
   no Takeout date candidate.

Also record per media instance: album-folder memberships (each Takeout album folder the
same content hash appears in becomes a topical keyword candidate), and a
`google_recompressed` heuristic flag (e.g., JPEG with stripped maker notes and Google
signature quantization, or resolution capped at known "Storage saver" sizes).

Acceptance: >99% of Takeout media matched to a sidecar or explicitly reported;
zero sidecars silently dropped.

### Stage 2b: Takeout-derived subtree detection within LOCAL

Because LOCAL contains prior Takeout extractions, an instance's presence in LOCAL does
not by itself imply human curation. Per directory in LOCAL, compute a
Takeout-derivation signal from: presence of Google JSON sidecars, folder names matching
Takeout signatures ("Photos from YYYY", "Google Photos", album-export layouts),
`metadata.json` album descriptors, and Google recompression flags on the contained
media. Directories over threshold are marked `takeout_derived`; their instances get
`effective_source_trust = TAKEOUT` regardless of physical location, and any JSON
sidecars found there are parsed exactly as in Stage 2 (they are additional metadata,
often from older exports with content the newest export lacks).

Folder-date candidates from takeout_derived directories keep folder-level precision but
receive Takeout-level trust in Stage 3 (they reflect Google's dating, not the user's
research). Genuinely hand-curated LOCAL directories (no Takeout signatures) retain full
curated trust. The per-directory classification is exported to
`reports/local_provenance.csv` for the user to spot-check and override before Stage 3
runs; overrides are config-listed path prefixes.

Acceptance: every LOCAL directory classified; user has reviewed the provenance report;
classification overrides honored.

### Stage 3: Date resolution

Purpose: assign each instance a resolved date, precision, and source, honoring the
user's folder-based curation.

Candidate extraction:
- `cand_exif`: DateTimeOriginal, falling back to CreateDate (record which).
- `cand_folder`: parse the instance's LOCAL path with a configurable set of patterns
  (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`, `YYYY_event`, `YYYYMMDD_event`, month-name
  variants). Deepest matching path component wins; precision recorded. Takeout
  "Photos from YYYY" folders yield a year-precision candidate with lower trust.
- `cand_takeout`: photoTakenTime from sidecar.
- `cand_filename`: patterns like `IMG_YYYYMMDD_HHMMSS`, `PXL_...`, `VID_...`,
  `scan###` (no date), configurable.
- File mtime is recorded but never used as a candidate.

EXIF distrust heuristics (any hit sets a flag and removes EXIF from auto-trust):
- Epoch/near-epoch or manufacturer default dates (1970-01-01, 1980-01-01, 2000-01-01).
- Identical timestamp shared by >N files (N configurable, default 25) from the same
  folder: mass-copy or scanner batch signature.
- Date precedes the camera model's plausible era (small built-in table for the user's
  known cameras; extendable).
- Date is years after the folder date on a file whose folder has day/month precision
  (scan-date signature).
- DateTimeOriginal absent but CreateDate present on a scanned file type (TIFF/PNG).

Resolution rules (first match wins):
- R1: EXIF trusted AND folder candidate exists AND EXIF within folder granularity
  (a 1998 folder brackets any 1998 EXIF): resolve = EXIF, precision = second,
  status = auto, confidence high.
- R2: EXIF trusted AND no folder candidate: resolve = EXIF.
- R3: EXIF missing or distrusted AND folder candidate exists: resolve = folder date at
  folder precision (year precision stored as YYYY-01-01 + precision=year), source=folder.
- R4: EXIF missing/distrusted, no folder, Takeout candidate exists and is not itself an
  upload-date artifact (heuristic: differs from Takeout creationTime): resolve = takeout_json.
- R5: filename candidate as last resort, precision as parsed.
- R6: EXIF trusted but CONFLICTS with folder granularity: status = conflict, queue for
  review. Never auto-resolve. (This protects both the user's research and the cases
  where the user misfiled.)
- R7: nothing usable: status = conflict, queue for review with an "unknown date" batch
  workflow (these land in `archive/undated/` if the user chooses).

All rule firings logged to `decision`. Timezone policy: naive local times are kept
naive; no timezone inference in v1 (see Open Decisions).

Acceptance: every media instance has status auto/reviewed/conflict; zero pending;
conflict rate reported; random-sample audit sheet (200 items) generated for the user.

### Stage 4: Review (date conflicts)

Local FastAPI app (127.0.0.1 only) reading/writing the catalog.

Date-conflict queue features:
- Shows thumbnail, all candidates with sources and flags, folder path, camera, and a
  filmstrip of neighboring instances from the same folder and from the same
  camera+time window.
- Single-item resolution and batch resolution ("apply folder date to all 214 items in
  this folder", "trust EXIF for this whole camera roll sequence").
- Optional manual date entry with precision selector, and a SequenceHint editor for
  ordering scanned batches within a coarse date.
- Every action appends to `decision` with actor `review:user`.

### Stage 5: Deduplication

Runs after date resolution so metadata merging can respect curated dates.

Pass A, exact: group instances by sha256. All same-hash instances form one cluster;
winner selection here only decides which *path context* is primary (for keyword and
provenance purposes); bytes are identical.

Pass B, companion pairing (before near-dup clustering, to avoid false merges):
- RAW+JPEG: same basename or same camera+timestamp within 1s, one RAW one JPEG.
  Both are kept in the archive as companions (policy configurable).
- Live/motion photos: HEIC/JPG + MOV/MP4 pairs (Apple ContentIdentifier match, or
  Google motion-photo embedded video). Kept together as companions.
- Google `-edited` versions: linked to their original; policy: keep both, edited
  archived beside the original with `_edited` suffix (configurable to quarantine
  edits instead).

Pass C, near-duplicate images: BK-tree or banded index over pHash; candidate pairs with
Hamming distance <= T1 (default 6) confirmed by dHash distance <= T2 (default 8) and
aspect-ratio agreement within 1%. Union-find into clusters. Distances in
(T1, T1+4] queue as "possible duplicates" for review rather than auto-clustering.

Pass D, near-duplicate videos: match on duration within 1s AND resolution family AND
keyframe pHash distance <= T1. Video matching is conservative; anything uncertain goes
to review. Never auto-discard a video.

Winner scoring (per cluster, deterministic, logged with full breakdown):

```
score = 3.0 * log2(megapixels)                    # resolution dominates
      + 1.5 * format_rank                          # RAW/original > HEIC/JPEG > recompressed
      + 1.0 * min(exif_tag_count, 60) / 60         # metadata richness
      + 1.0 * has_trusted_original_dto             # camera-written DateTimeOriginal intact
      + 0.75 * effective_source_trust              # curated LOCAL > takeout_derived LOCAL = TAKEOUT (Stage 2b)
      + 0.5 * log10(size_bytes) [tiebreak]         # larger file at same resolution
      - 2.0 * google_recompressed_flag
```

Guardrails: if the top two scores are within 0.5, or the winner would have Takeout-level
trust while a curated-LOCAL instance exists at equal resolution, queue for review. Crops (aspect-ratio or
resolution mismatch beyond tolerance inside a cluster) always queue for review; a crop
is a different artistic object, not a duplicate.

Metadata merge onto the winner (field-level, logged):
- Date: the cluster's resolved date follows provenance priority
  review > folder > trusted EXIF > takeout_json > filename. If members disagree after
  resolution, review.
- GPS: winner keeps its own; if absent, take from any member (prefer camera EXIF over
  Takeout JSON; Takeout GPS can be user-added in Google Photos, which is acceptable,
  but is flagged in the log).
- Description/caption/title: union; Takeout `description` (user-typed in Google Photos)
  is valuable and is preserved.
- Keywords: union of topical keyword candidates from all members' folder contexts
  (Stage 6 maps folders to keywords).

Review queue for clusters shows side-by-side images, zoom, score breakdowns, and
per-field metadata diff; actions: accept, swap winner, split cluster, mark not-duplicate.

Acceptance: every cluster status auto/reviewed; no cluster crosses the guardrails
without review; sampled clusters (200) exported for user audit.

### Stage 6: Materialize

Purpose: write the archive and quarantine. The only stage that mutates the filesystem
(beyond staging extraction).

1. **Keyword mapping.** Generate `reports/keyword_map.csv` from all distinct topical
   folder names (LOCAL topical folders + Takeout album folders): columns
   `folder_name, proposed_keyword, action(keep|rename|drop)`. The user edits this once;
   the pipeline applies it. Hierarchies allowed (`Travel/Vietnam/Hanoi`).
2. **Destination layout.** `archive/YYYY/YYYY-MM/` from resolved date (year-precision
   items go to `archive/YYYY/`; unknown to `archive/undated/`). Filename:
   `<original_basename>__<sha256[:8]>.<ext>` to guarantee collision-freedom while
   preserving traceability. Companions share the basename stem.
3. **Copy + write metadata.** For each winner: copy to temp in destination dir, verify
   byte-identity by re-hash, then exiftool writes in one batch: resolved
   DateTimeOriginal (+CreateDate/ModifyDate coherently), GPS, description,
   XMP-dc:Subject keywords, and all ArchivePipe provenance tags; then atomic rename.
   RAW and any risky format: bytes untouched, sidecar `.xmp` written alongside.
   Record post-write sha256 in `placement` (the pre-write source hash remains the
   conservation-law key).
4. **Quarantine.** Every loser instance is copied (byte-identical, verified) to
   `quarantine/<sha256[:2]>/<sha256>__<original_basename>` with a JSONL index mapping
   back to source path, cluster, and winner. Exact-duplicate extras with identical
   bytes are recorded once in quarantine (hash-identical copies need no second copy;
   the index lists all source paths).
5. **Exclusions.** Takeout JSON sidecars, `.DS_Store`, thumbnails (`.thumbnails`,
   Picasa `.picasa.ini`), and other non-media are marked `excluded` with reasons.
6. **Manifests.** `reports/archive_manifest.csv` (dest path, source hash, dest hash,
   resolved date, precision, source, keywords) and `reports/quarantine_manifest.csv`.

Dry-run mode produces both manifests and the full decision log with zero writes.

Acceptance: dry-run manifest reviewed by user before `--execute`; post-execute re-hash
of a 1% random sample matches recorded hashes.

### Stage 7: Verify

- Conservation check (INV-3) over the full inventory; machine-readable pass/fail report.
- Full checksum verification of archive and quarantine.
- Statistics report: counts by disposition, date-source distribution, precision
  distribution, cluster-size histogram, review-decision summary, storage totals.
- Exit nonzero on any discrepancy; discrepancies enumerated exactly.

### Stage 8: Maintain (ongoing)

- `maintain verify-checksums`: periodic re-verification (cron-able).
- `maintain import --root`: incremental ingest of new photos through the same
  ingest -> date-resolve -> dedup(-against-archive) -> materialize path.
- Documented handoff to a photo manager: digiKam or immich pointed at `archive/`,
  reading embedded XMP keywords as albums/tags. The archive tree remains the source of
  truth; the manager is a view.
- Quarantine purge is a separate, explicitly manual command that requires typing a
  confirmation phrase and prints what will be destroyed; recommend running it no sooner
  than 6 months after verify passes and backups exist.
- 3-2-1 backup of `archive/` + `catalog.db` + `reports/` (tooling out of scope; document
  restic/borg as options).

## 9. Testing strategy

- **Fixture generator**: a script that builds a synthetic corpus exercising every edge
  case in section 11 (tiny generated images with crafted EXIF via exiftool, fake Takeout
  trees with sidecar pathologies, RAW stand-ins, Live Photo pairs, videos via ffmpeg
  color bars). Fixtures are deterministic (seeded).
- **Golden pipeline test**: run the full pipeline on the fixture corpus; assert exact
  expected dispositions, resolved dates, and winner choices.
- **Property tests (hypothesis)**:
  - Conservation: for arbitrary generated corpora, every source hash is accounted for.
  - Idempotency: running any stage twice yields identical catalog state.
  - Crash-resume: kill mid-stage at random points (fault injection), re-run, final
    state equals uninterrupted run.
- **Scoring unit tests**: table-driven cases pinning the score function and guardrails.
- **Date-rule unit tests**: one test per rule R1..R7 and per distrust heuristic.
- **No-write test**: mutating stages under `--dry-run` are run against a filesystem
  watcher asserting zero writes outside `reports/` and `logs/`.

## 10. Implementation milestones for Claude Code

Implement in this order; each milestone ends with passing tests and a short demo command.

1. **M1 Skeleton**: Poetry project, CLI scaffold, config loading, catalog schema +
   migrations, structured logging, `run` bookkeeping. Fixture generator v0.
2. **M2 Ingest**: hashing, EXIF extraction, perceptual hashing, video signatures,
   resumability. Golden ingest test.
3. **M3 Takeout normalization**: sidecar matching including pathologies; report of
   unmatched. Tests per pathology.
4. **M4 Date resolution**: candidates, heuristics, rules R1-R7, decision logging.
   Audit-sample exporter.
5. **M5 Review UI**: date-conflict queue with batch operations; then cluster queue.
6. **M6 Dedup**: exact, companion pairing, near-image, near-video, scoring, guardrails.
7. **M7 Materialize**: keyword map workflow, dry-run manifests, execute path with
   atomic copies and exiftool writes, quarantine.
8. **M8 Verify + report + maintain**: conservation checker, checksum verify, stats,
   incremental import.

CLAUDE.md for the repo should state: the invariants in section 3 verbatim; "never write
outside the working tree"; "all metadata writes go through exiftool"; the user's code
style (docstrings with examples, no incremental-change comments, type hints, Poetry).

## 11. Edge case registry (must be handled and tested)

1. Takeout JSON filename truncation and `(n)` numbering inconsistencies.
2. `-edited` versions sharing the original's sidecar.
3. Live Photos (HEIC+MOV, Apple ContentIdentifier) and Android motion photos
   (embedded MP4 inside JPEG); pixel-motion pairs must never be treated as independent
   duplicates of each other.
4. RAW+JPEG pairs from the same shutter press.
5. HEIC vs JPEG transcodes of the same shot (near-dup cluster, format_rank decides).
6. Google "Storage saver" recompression and stripped maker notes.
7. Scanner batches: shared EXIF dates, TIFF/PNG with only CreateDate.
8. Camera clock resets (default dates) and cameras with wrong-year clocks
   (systematic offset detectable within a folder; offer batch offset correction in
   review as a stretch feature).
9. Zero-byte, truncated, or undecodable files: cataloged, flagged `corrupt`,
   quarantined, reported; never block the pipeline.
10. Filename collisions across sources and within destination days.
11. Same photo in many Takeout album folders (exact-dup collapse + keyword union).
12. Videos re-muxed by Google (identical content, different container hash):
    caught by Pass D or left as flagged possible-dups for review; never auto-dropped.
13. Non-media files inside photo folders (docs, zips): `excluded` with reason, listed
    for the user (they may be wanted elsewhere, but they are not lost).
14. Extremely long paths / unicode normalization differences (NFC/NFD) between
    filesystems; store paths as NFC, compare accordingly.
15. Sidecar `.xmp` files already present in LOCAL (from prior tools): ingested, parsed
    as an additional metadata candidate for their paired image, preserved in quarantine.

## 12. Configuration surface (config.toml)

All thresholds and policies named here are config keys with the stated defaults:
phash/dhash thresholds, mass-identical N, guardrail margin, RAW policy
(companion|prefer_raw), edited policy (keep_both|quarantine_edits), undated placement,
folder date patterns (extendable regex list), camera-era table, keyword hierarchy
separator, parallelism, and the `preserve.confirmed` gate.

## 13. Resolved decisions (2026-07-22, from the user)

1. **Platform**: macOS on Apple Silicon (M3 Max, 36 GB). Destination filesystem APFS.
   Paths compared NFC-normalized (APFS stores NFD-ish; exercise edge case 14). The
   working tree lives on an external drive that MUST be formatted APFS (not
   exFAT/NTFS), since the pipeline depends on atomic renames and sane metadata
   performance. `pillow-heif` covers HEIC decode on macOS.
2. **Scale and storage**: ~285,000 items, ~1.12 TB in LOCAL, plus the new Takeout.
   Layout: (a) verbatim Phase-0 backup of LOCAL + all Takeout archives on external
   drive A; (b) working tree (staging, archive, quarantine, catalog) on external
   drive B or a separate APFS volume; internal disk stays out of the pipeline. With
   quarantine holding all losers, budget roughly 2 to 2.5 TB for the working tree.
   Hashing + pHash of 285k items is an overnight run on this hardware.
3. **RAW**: keep RAW+JPEG as companions in the archive.
4. **Live/motion photos** (mostly Android, so Google motion photos with embedded MP4):
   keep pairs/embeddings intact; never separate or auto-dedup across the pair.
5. **Edited versions**: keep both original and Google-edited in the archive. The edited
   file receives XMP `Rating=4` and keyword `edited-preferred`; the original receives
   keyword `has-edit`. Photo managers can then surface edits by default while
   originals remain adjacent.
6. **Multiple Takeouts**: yes, several historical exports ingested over the years, some
   already merged into LOCAL. Consequences: each distinct Takeout export is its own
   source ID; Stage 2b classifies Takeout-derived subtrees inside LOCAL and downgrades
   their trust; older exports are worth ingesting whole because they can hold
   originals or metadata absent from the newest export.
7. **Videos**: Google may hold videos that do not exist locally. Policy: video clusters
   have elevated review priority; no video is ever auto-quarantined; a Takeout-only
   video (no local counterpart) is a singleton and is archived normally. The report
   explicitly lists Takeout-only videos so the user can see what Google uniquely held.
8. **Timezones**: v1 keeps naive local times; UTC-offset backfill is out of scope.
9. **Long-term browser**: digiKam, pointed read-mostly at `archive/` after
   materialization, with its database on the internal SSD. Rationale: fully local, no
   server to maintain, first-class XMP read/write, and its Similarity/duplicate finder
   provides an independent second-pass audit of the pipeline's dedup. immich remains a
   later option; nothing in the archive design would change. Duplicate REVIEW during
   the pipeline itself uses the built-in FastAPI review UI (Stage 4/5 queues), not
   digiKam: the review decisions must write to the catalog and decision log, which an
   external browser cannot do.
