# photo-archive-pipeline

Non-destructive consolidation of ~30 years of photos and videos from a locally
organized tree and Google Takeout exports into a single canonical, date-organized
archive with embedded metadata, controlled deduplication, and verifiable zero-loss
guarantees. The full design lives in `photo_archive_pipeline_spec.md`; the rules for
working in this repo live in `CLAUDE.md`.

## Status

Milestone **M4 (date resolution)** complete. Working today:

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

Stages review onward are stubs that name their milestone.

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
```

Ingest refuses to run until the Stage 0 preserve gate (`preserve.confirmed` in
`config.toml`) is set to `true` by you, after a verbatim backup of all sources
exists on a separate physical disk. A `--root` pointing at a Takeout `.zip` is
staged (extracted once, space-checked) into the working tree automatically.
Re-running an ingest is a no-op; every run ends with a random-sample hash
verification.
