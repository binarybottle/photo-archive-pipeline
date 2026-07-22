# photo-archive-pipeline

Non-destructive consolidation of ~30 years of photos and videos from a locally
organized tree and Google Takeout exports into a single canonical, date-organized
archive with embedded metadata, controlled deduplication, and verifiable zero-loss
guarantees. The full design lives in `photo_archive_pipeline_spec.md`; the rules for
working in this repo live in `CLAUDE.md`.

## Status

**All milestones (M1–M8) complete** — the full pipeline is implemented. Working
today:

- `archive ingest` (M2): full source inventory — streamed SHA-256, signature MIME,
  exiftool batch EXIF, perceptual hashes, video keyframe signatures (needs ffmpeg),
  Takeout zip staging with disk-space preflight, size+mtime resumability.
- `archive takeout-normalize` (M3): sidecar-to-media matching (exact /
  supplemental-metadata, truncation, `(n)` numbering in both orderings), `-edited`
  pair linkage, album-folder memberships, `google_recompressed` heuristic, and a
  CSV report of everything unmatched. Idempotent re-runs.
- `archive local-provenance` (M4, Stage 2b): classifies every LOCAL directory as
  curated vs takeout-derived (sidecar/name/descriptor/recompression signals),
  exports `reports/local_provenance.csv` for review, honors config overrides,
  assigns effective trust, and parses sidecars inside derived subtrees.
- `archive date-resolve` (M4, Stage 3): EXIF/folder/Takeout/filename candidates,
  distrust heuristics (epoch defaults, mass-identical, camera era, scan-date,
  CreateDate-only scans), rules R1–R7 with full decision logging, reviewed rows
  preserved, and `reports/date_audit_sample.csv` for user audit.
- `archive review serve` (M5, Stage 4): local web UI on 127.0.0.1 — date-conflict
  queue grouped by folder with thumbnails, per-item candidate/flag detail with
  same-folder and same-camera filmstrips, accept-candidate / manual date with
  precision / SequenceHint, batch "apply folder date" and "trust EXIF", plus a
  duplicate-cluster queue (accept / swap winner / split out / not-a-duplicate).
  Every action appends to the decision log as `review:user`; reviewed rows
  survive re-resolution.
- `archive dedup` (M6, Stage 5): exact clusters by SHA-256, RAW+JPEG and
  Live-photo companion pairing, banded-pHash near-image clustering with dHash +
  aspect confirmation and a possible-duplicate review band, conservative
  near-video matching (never auto-discarded), the spec winner-score formula
  with logged breakdowns, guardrails (score margin, takeout-over-curated,
  crop aspect mismatch), field-level metadata merge planning (date provenance
  priority, GPS with camera-EXIF preference, description/title/keyword
  unions), review locking, and `reports/cluster_audit_sample.csv`.
- `archive materialize` (M7, Stage 6): dry-run by default (INV-4) with full
  manifests and zero writes; the keyword-map workflow
  (`reports/keyword_map.csv`, keep/rename/drop with hierarchies); on
  `--execute`: INV-9 space preflight, atomic copy-verify-rename into
  `archive/YYYY/YYYY-MM/<stem>__<sha8><ext>`, metadata written through
  exiftool in one batch per file (resolved dates, GPS, description,
  XMP-dc:Subject keywords, the full XMP-ArchivePipe provenance namespace,
  Rating=4 + `edited-preferred`/`has-edit` for edited pairs), `.xmp` sidecars
  with untouched bytes for RAW/video, content-addressed quarantine with a
  JSONL index (one copy per hash), exclusions with reasons, the `placement`
  ledger, resumable re-runs, and a post-execute hash sample check.
- `archive verify` (M8, Stage 7): proves the conservation law (INV-3) on disk —
  every instance placed exactly once, every archive file re-hashed against its
  recorded hash, every quarantine file byte-identical to its source, nothing
  unaccounted on disk; discrepancies enumerated exactly, nonzero exit on any;
  machine-readable `reports/verify_report.json` with full statistics.
- `archive report` (M8): human-readable summary — dispositions, date sources
  and precisions, cluster histogram, decision/review counts, storage totals,
  Takeout-only videos, last run per stage, last verify result.
- `archive maintain verify-checksums` (M8): cron-able re-hash of archive and
  quarantine against the ledger.
- `archive maintain import --root` (M8): incremental imports through the same
  ingest → provenance → date-resolve → dedup path; byte-identical newcomers
  never displace already-archived winners; review, then materialize as usual.
- `archive maintain purge-quarantine` (M8): manual destruction of quarantined
  losers, gated on a *passing* verify and a typed confirmation phrase; writes
  a purge marker so later verifies report the purge instead of failing.
  Recommended no sooner than 6 months after verify passes with backups in place.

## Setup

Requires Python 3.12+, [Poetry](https://python-poetry.org), and `exiftool`
(`brew install exiftool`). Later milestones also need `ffmpeg`.

```
poetry install
poetry run pytest
```

## Demo (M3)

```
poetry run archive --working-tree /tmp/archive-demo init
# ... make your Stage 0 backup, then set preserve.confirmed = true in config.toml ...
poetry run archive fixtures generate --dest /tmp/archive-fixtures --seed 42
poetry run archive --working-tree /tmp/archive-demo ingest --source LOCAL --root /tmp/archive-fixtures/LOCAL
poetry run archive --working-tree /tmp/archive-demo ingest --source TAKEOUT --root /tmp/archive-fixtures/TAKEOUT --export-id t2015
poetry run archive --working-tree /tmp/archive-demo takeout-normalize
poetry run archive --working-tree /tmp/archive-demo local-provenance
# ... review /tmp/archive-demo/reports/local_provenance.csv, set overrides ...
poetry run archive --working-tree /tmp/archive-demo date-resolve
poetry run archive --working-tree /tmp/archive-demo dedup
poetry run archive --working-tree /tmp/archive-demo review serve   # http://127.0.0.1:8765
poetry run archive --working-tree /tmp/archive-demo materialize    # dry-run
# ... review manifests + keyword_map.csv ...
poetry run archive --working-tree /tmp/archive-demo materialize --execute
poetry run archive --working-tree /tmp/archive-demo verify
poetry run archive --working-tree /tmp/archive-demo report
```

## After the pipeline: browsing and backups

- **Photo manager**: point digiKam (database on the internal SSD) read-mostly at
  `archive/`. It reads the embedded XMP keywords (`XMP-dc:Subject`) as tags and
  the `Rating=4` on edited-preferred versions; its Similarity search doubles as
  an independent second-pass audit of the pipeline's dedup. The archive tree
  stays the source of truth — the manager is a view. (immich remains an option
  later; nothing in the archive layout would change.)
- **Backups**: keep 3-2-1 copies of `archive/` + `catalog.db` + `reports/`
  (e.g. restic or borg to a second disk and one offsite target). Schedule
  `archive maintain verify-checksums` periodically (cron/launchd).
- **Purging quarantine**: only after `archive verify` passes, backups exist,
  and at least ~6 months have gone by: `archive maintain purge-quarantine`.

Ingest refuses to run until the Stage 0 preserve gate (`preserve.confirmed` in
`config.toml`) is set to `true` by you, after a verbatim backup of all sources
exists on a separate physical disk. A `--root` pointing at a Takeout `.zip` is
staged (extracted once, space-checked) into the working tree automatically.
Re-running an ingest is a no-op; every run ends with a random-sample hash
verification.
