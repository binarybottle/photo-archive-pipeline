# photo-archive-pipeline

Non-destructive consolidation of ~30 years of photos and videos from a locally
organized tree and Google Takeout exports into a single canonical, date-organized
archive with embedded metadata, controlled deduplication, and verifiable zero-loss
guarantees. The full design lives in `photo_archive_pipeline_spec.md`; the rules for
working in this repo live in `CLAUDE.md`.

## Status

Milestone **M2 (ingest)** complete: full source inventory with streamed SHA-256,
signature-based MIME + kind classification, exiftool batch EXIF extraction,
perceptual hashes (pHash/dHash, orientation-normalized), video keyframe signatures
(needs ffmpeg), Takeout zip staging with disk-space preflight, and size+mtime
resumability (re-runs are no-ops). Stages takeout-normalize onward are stubs that
name their milestone.

## Setup

Requires Python 3.12+, [Poetry](https://python-poetry.org), and `exiftool`
(`brew install exiftool`). Later milestones also need `ffmpeg`.

```
poetry install
poetry run pytest
```

## Demo (M2)

```
poetry run archive --working-tree /tmp/archive-demo init
# ... make your Stage 0 backup, then set preserve.confirmed = true in config.toml ...
poetry run archive fixtures generate --dest /tmp/archive-fixtures --seed 42
poetry run archive --working-tree /tmp/archive-demo ingest --source LOCAL --root /tmp/archive-fixtures/LOCAL
poetry run archive --working-tree /tmp/archive-demo ingest --source TAKEOUT --root /tmp/archive-fixtures/TAKEOUT --export-id t2015
```

Ingest refuses to run until the Stage 0 preserve gate (`preserve.confirmed` in
`config.toml`) is set to `true` by you, after a verbatim backup of all sources
exists on a separate physical disk. A `--root` pointing at a Takeout `.zip` is
staged (extracted once, space-checked) into the working tree automatically.
Re-running an ingest is a no-op; every run ends with a random-sample hash
verification.
