# photo-archive-pipeline

Consolidates ~30 years of photos and videos — a locally organized folder tree
plus one or more Google Takeout exports — into a single, canonical,
date-organized archive (`archive/YYYY/YYYY-MM/…`) with dates, GPS, captions, and
topical keywords embedded in the files themselves.

**The promise:** your source files are opened read-only and never modified,
nothing is ever deleted (duplicates and rejects go to a quarantine you purge
later, by hand), and a final `verify` step proves on disk that every original is
accounted for. Everything the pipeline writes lives under one working-tree
directory — delete it and your originals are untouched.

Design details are in `photo_archive_pipeline_spec.md`; contributor rules are in
`CLAUDE.md`.

---

## Prerequisites

- **macOS**, Python 3.12+, [Poetry](https://python-poetry.org)
- `brew install exiftool ffmpeg` (ffmpeg is needed to fingerprint videos —
  install it **before** ingesting if you have any)
- A **working-tree location on an APFS volume** with room for roughly **2× your
  collection** (it holds the archive *and* a quarantine copy of everything until
  you purge). It can be your internal SSD or an external APFS drive — but **not**
  the same drive as your backup.

```bash
git clone <this repo> && cd photo-archive-pipeline
poetry install
```

---

## One-time shell setup

Point the tool at your working tree once, so every command stays short:

```bash
cd /path/to/photo-archive-pipeline
export ARCHIVE_WORKING_TREE=/path/to/archive-project   # the pipeline-owned dir
alias archive="poetry run archive"                     # convenience for this shell
```

Every `archive …` command below is shorthand for `poetry run archive …`, and
each one operates on `ARCHIVE_WORKING_TREE`. (You can instead pass
`--working-tree /path/...` to any command.)

---

## The workflow — what to run, in order

Run these stages top to bottom. Each stage is **re-runnable**: running a
completed stage again with no new input does nothing, and re-runs **preserve any
review decisions** you've made. Stages that need your judgment are marked
**⟳ review**.

### 1 · Initialize the working tree

```bash
archive init
```

Creates `catalog.db`, `config.toml`, and empty `archive/ quarantine/ reports/
logs/`. Safe to run anytime.

### 2 · Back up your sources, then open the gate — **required, once**

Make a verbatim backup of every source on a **separate physical disk** (Time
Machine counts, as long as it isn't *excluding* your photos). Then edit
`config.toml` and set:

```toml
[preserve]
confirmed = true
```

`ingest` refuses to run until you do. The pipeline never touches your sources —
this backup is your insurance if a drive dies mid-run.

### 3 · Ingest your sources

```bash
# your local photo tree:
caffeinate -i archive ingest --source LOCAL --root /path/to/photos

# each Google Takeout export is a SEPARATE source (a .zip or an extracted folder):
caffeinate -i archive ingest --source TAKEOUT --root /path/to/takeout.zip --export-id 2024
```

Catalogs every file (SHA-256, EXIF, perceptual hash, video signature). This is
the **long** stage — expect an overnight run for a large library. It's
resumable: if it's interrupted, run the same command again and it continues.
`caffeinate -i` keeps the Mac awake; sources stay read-only throughout.

### 4 · Normalize Takeout sidecars — *only if you ingested a TAKEOUT source*

```bash
archive takeout-normalize
```

Matches Google's `.json` sidecars to their photos. **Skip this if you only have
LOCAL sources.** (Old Takeout content that's already merged *inside* your local
tree is handled by the next step, not here.)

### 5 · Classify your local folders — **⟳ review**

```bash
archive local-provenance
```

Decides which LOCAL folders are your own curation versus old Google dumps (which
shouldn't inherit your careful dating).

→ **Then:** open `reports/local_provenance.csv`. If a folder is misclassified,
add its path prefix under `[provenance]` in `config.toml`
(`curated_overrides` or `takeout_derived_overrides`) and re-run this step.

### 6 · Resolve capture dates — **⟳ review**

```bash
archive date-resolve
```

Assigns each photo a date using the trust hierarchy (your dated folders outrank
EXIF, which outranks Google's dates). Prints how many resolved automatically vs.
need review, and writes `reports/date_audit_sample.csv` to spot-check.

→ **Then:** clear the conflicts in the review UI (step 8). This is optional —
anything you leave unresolved is filed under `archive/undated/`, recoverable, not
lost.

### 7 · Find duplicates

```bash
archive dedup
```

Groups exact and near-duplicates, picks the best copy of each, and marks the
rest for quarantine. Uncertain cases queue for review.

→ **Then:** review the duplicate clusters in the UI (step 8). This review **is
required** — `materialize` won't run while any cluster is still pending.

### 8 · Review — the human UI (date conflicts + duplicate clusters)

```bash
archive review serve          # then open http://127.0.0.1:8765
```

Run this in its own terminal (it's a local web server, 127.0.0.1 only). Work the
**Date conflicts** queue (each folder group shows its candidate dates with batch
"apply folder date" / "trust EXIF" buttons) and the **Duplicate clusters** queue
(accept / swap winner / split / not-a-duplicate). Every action is saved and
survives re-running `date-resolve` or `dedup`. Stop and restart anytime.

### 9 · Preview the archive — dry run, **⟳ review**

```bash
archive materialize            # writes NOTHING
```

Produces `reports/archive_manifest.csv`, `quarantine_manifest.csv`, and
`keyword_map.csv`, and prints the **exact disk space** the real run will need.

→ **Then:** edit `reports/keyword_map.csv` to choose how topical folder names
become keywords (`keep` / `rename` / `drop`), and skim the manifests to confirm
where things will land.

### 10 · Build the archive — the only step that writes files

```bash
archive materialize --execute
```

Copies each winner into `archive/`, writes its metadata via exiftool, and copies
losers to `quarantine/`. Space-checked before it starts; resumable if
interrupted. Your sources are still never touched.

### 11 · Prove nothing was lost

```bash
archive verify                 # conservation law + checksums; fails loudly on any gap
archive report                 # human-readable summary of the whole archive
```

`verify` exits non-zero and enumerates the exact discrepancies if anything is
missing, altered, or unaccounted for.

---

## The rules that keep you safe

- **Sources are read-only** and never modified — metadata is written only to
  copies inside the working tree.
- **Nothing is deleted.** Duplicates and rejects sit in `quarantine/` until you
  purge them by hand, much later.
- **`materialize` is a dry run by default** — only `--execute` writes files, and
  it space-checks first.
- **Every stage is resumable and re-runnable**, and re-runs preserve your review
  decisions.
- **Everything is under the working tree.** `archive/` is a normal folder tree
  you can copy elsewhere (e.g. to your laptop) once `verify` passes.

## After you have an archive

- **Browse it:** point [digiKam](https://www.digikam.org) (its database on your
  internal SSD) read-mostly at `archive/`. It reads the embedded XMP keywords as
  tags and the `Rating=4` on preferred edits, and its duplicate finder is an
  independent second audit of the dedup.
- **Back it up:** keep 3-2-1 copies of `archive/` + `catalog.db` + `reports/`
  (e.g. restic/borg). Re-check integrity periodically:
  ```bash
  archive maintain verify-checksums
  ```
- **Add new photos later:**
  ```bash
  archive maintain import --root /path/to/new-photos
  ```
  then review and `materialize` as usual. Byte-identical newcomers never displace
  what's already archived.
- **Reclaim quarantine space — carefully, much later:** only after `verify`
  passes, backups exist, and you've lived with the archive for ~6 months:
  ```bash
  archive maintain purge-quarantine     # asks you to type a confirmation phrase
  ```

## Working-tree layout

```
archive-project/
  catalog.db        single source of truth (SQLite)
  config.toml       all your settings and overrides
  archive/          the canonical output: YYYY/YYYY-MM/<name>__<hash>.<ext>
  quarantine/       losers, kept byte-identical, until you purge
  reports/          manifests, audit samples, verify report (CSV/JSON)
  logs/             structured run logs (JSONL)
  staging/          extracted Takeout zips (pipeline-owned)
```

## Development

```bash
poetry run pytest          # test suite
poetry run ruff check src tests
poetry run mypy
poetry run archive fixtures generate --dest /tmp/corpus   # synthetic test corpus
```
