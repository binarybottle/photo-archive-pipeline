# CLAUDE.md — photo-archive-pipeline

This repo implements the pipeline specified in `photo_archive_pipeline_spec.md`. That
document is the implementation contract; when in doubt, the spec wins. This file states
the rules that must never be violated and the conventions for all code written here.

## Ironclad invariants (spec section 3, verbatim)

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

## Absolute rules

- **Never write outside the working tree.** The only directories the pipeline may
  create, modify, or delete files in are the working tree
  (`catalog.db`, `archive/`, `quarantine/`, `review/`, `reports/`, `logs/`, staging)
  and explicitly pipeline-owned temp dirs. Source roots are sacrosanct (INV-1/INV-2).
- **All metadata writes go through exiftool.** No other library or hand-rolled code
  writes EXIF/XMP/IPTC to media files. Ever.

## Code style

- Python 3.12+, Poetry, `src/` layout, type hints throughout.
- Docstrings with usage examples at module and function level.
- No incremental-change comments (no "changed X to Y", "new:", "now handles Z" —
  comments describe the code as it is, not its history).
- Tests with pytest (+ hypothesis for property tests); `ruff` and `mypy` must pass.

## Development commands

```
poetry install                 # set up the environment
poetry run pytest              # run tests
poetry run ruff check src tests
poetry run mypy
poetry run archive --help      # the CLI
```

The CLI operates on a working tree selected with `--working-tree` (or the
`ARCHIVE_WORKING_TREE` env var). `archive init` creates the layout, a default
`config.toml`, and the catalog schema. `archive fixtures generate` builds the
deterministic synthetic test corpus (spec section 9).

## Milestones

Implementation follows spec section 10 (M1 skeleton → M8 verify/maintain). Stage
commands for future milestones exist as stubs that exit with code 2 and name their
milestone. `ingest` already enforces the Stage 0 preserve gate
(`preserve.confirmed = true` in `config.toml`).
