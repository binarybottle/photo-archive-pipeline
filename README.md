# photo-archive-pipeline

Non-destructive consolidation of ~30 years of photos and videos from a locally
organized tree and Google Takeout exports into a single canonical, date-organized
archive with embedded metadata, controlled deduplication, and verifiable zero-loss
guarantees. The full design lives in `photo_archive_pipeline_spec.md`; the rules for
working in this repo live in `CLAUDE.md`.

## Status

Milestone **M1 (skeleton)** complete: Poetry project, `archive` CLI scaffold, config
loading, catalog schema + migrations, structured JSONL logging, run bookkeeping, and
fixture generator v0. Stages ingest onward are stubs that name their milestone.

## Setup

Requires Python 3.12+, [Poetry](https://python-poetry.org), and `exiftool`
(`brew install exiftool`). Later milestones also need `ffmpeg`.

```
poetry install
poetry run pytest
```

## Demo (M1)

```
poetry run archive --working-tree /tmp/archive-demo init
poetry run archive fixtures generate --dest /tmp/archive-fixtures --seed 42
poetry run archive --working-tree /tmp/archive-demo ingest --source LOCAL --root /tmp/archive-fixtures/LOCAL
```

The last command refuses to run: the Stage 0 preserve gate
(`preserve.confirmed` in `config.toml`) must be set to `true` by you, after you
have made a verbatim backup of all sources on a separate physical disk.
