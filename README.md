# photo-archive-pipeline

Consolidates years of photos and videos — a locally organized folder tree
plus one or more Google Takeout exports — into a single, canonical,
date-organized archive (`archive/YYYY/YYYY-MM/…`) with dates, GPS, captions, and
topical keywords written into each file — or, for videos, RAW, and any format
whose metadata block can't be safely rewritten, into an `.xmp` sidecar beside
the file, leaving its bytes bit-identical.

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
- A **working-tree location on an APFS volume** with room for **roughly your
  whole collection again**: it holds the deduped `archive/` plus a `quarantine/`
  copy of the duplicate *losers*, which together come to about the size of your
  originals. If the working tree sits on the **same volume as your sources**, you
  therefore need about **2× your collection** free there. It can be your internal
  SSD or an external APFS drive — but **not** the same drive as your backup. The
  `materialize` dry-run prints the exact bytes required before you commit, and
  the real run pauses cleanly (resumable) if free space runs low.

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
"apply folder date" / "trust EXIF" buttons; the manual field accepts a year
`2007`, year-month `2007-08`, a full date, or a full timestamp and infers the
precision) and the **Duplicate clusters** queue (accept / swap winner / split /
not-a-duplicate). Bulk buttons clear the common cases in one click — "prefer my
curated copies over Takeout", "accept the rest", and a separate "accept video
clusters too" (videos are held back from the general accept for extra care).
Every action is saved and survives re-running `date-resolve` or `dedup`. Stop
and restart anytime.

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
losers to `quarantine/`. For JPEG/HEIC/PNG/TIFF the metadata is written into the
file (verified by re-hashing the copy); for videos, RAW, and any file whose
metadata block can't be safely rewritten, the bytes are left bit-identical and
the metadata goes to an `.xmp` sidecar next to it. It reports how many files
took a sidecar at the end. Space-checked before it starts; resumable if
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

- **Browse and manage it in [digiKam](https://www.digikam.org).** Point a
  digiKam collection at your `archive/` folder (only that folder), and configure
  it so it reads the pipeline's metadata and never modifies your originals. In
  **Settings → Configure digiKam → Metadata**:
  - *Sidecars* tab: **enable "Read from sidecar files"** (or digiKam shows no
    keywords/dates for any video or RAW — their metadata lives in `.xmp`
    sidecars). **Leave "Sidecar file names are compatible with commercial
    programs" unchecked** — the pipeline writes `name.ext.xmp`, and checking that
    box makes digiKam look for `name.xmp` and miss them all.
  - To keep the archive byte-for-byte intact (so `verify` stays valid, and so a
    synced folder isn't churned): enable **"Write to sidecar files"** and set the
    dropdown to **"Write to XMP sidecar only"** — every tag/rating/face you add
    goes to a sidecar, never into the photo. On the *Rotation* tab choose
    **"Rotate by only setting a flag"** (lossless), not "changing the content".
  - Put digiKam's **database on your local SSD, not inside the archive** (and
    never inside a synced folder — a live SQLite DB will corrupt/conflict).
  - Prefer **tags, ratings, labels, faces, and saved searches** over moving
    files: a photo takes one folder but many tags, and tagging only touches
    sidecars. Moving and deleting are still fine — just run `maintain reconcile`
    afterwards (below) so the catalog keeps up. Its duplicate finder is also a
    handy independent second audit of the dedup.
  - Leave `catalog.db` alone; it's the pipeline's ledger, still needed by
    `maintain import` and `verify`.
- **Moved, renamed, or deleted things in digiKam?** That's expected and safe —
  the pipeline never fights you for the tree — but you then run
  `maintain reconcile` so the catalog keeps up. See
  [Routine tasks](#routine-tasks) below for the exact steps.
- **Moving `archive/` out of the working tree?** You can keep the archive
  anywhere (a bigger drive, a synced folder). The pipeline's `verify` / `report`
  / `maintain` still expect it *at* `<working-tree>/archive`, so point them back
  with a symlink instead of copying it:
  ```bash
  ln -s /wherever/you/moved/archive /path/to/archive-project/archive
  ```
- **Back it up:** keep 3-2-1 copies of `archive/` + `catalog.db` + `reports/`
  (e.g. restic/borg). Re-check integrity periodically:
  ```bash
  archive maintain verify-checksums
  ```
- **Add new photos, or make your digiKam edits stick:** see
  [Routine tasks](#routine-tasks) below.
- **Reclaim quarantine space — carefully:** this permanently deletes the
  quarantined duplicates (often hundreds of GB), so do it only once `verify`
  passes and a **verbatim backup of your originals exists** — that backup, not
  the quarantine, is then your safety net. The command refuses unless the last
  `verify` passed, lists what it will destroy, and asks you to type
  `PURGE QUARANTINE`:
  ```bash
  archive maintain purge-quarantine
  ```
  If the interactive prompt won't accept your input (e.g. a wrapped or
  non-interactive terminal), pipe the phrase in instead:
  ```bash
  echo "PURGE QUARANTINE" | archive maintain purge-quarantine
  ```
  It leaves a `quarantine/.purged.json` marker so later `verify` /
  `maintain verify-checksums` runs know the quarantine was emptied on purpose and
  skip those file checks. (The built-in prompt also suggests waiting ~6 months if
  you'd rather keep the safety net local for a while.)

## Routine tasks

Two things come up over and over once the archive is live. Neither happens
automatically — the pipeline is a batch tool, not a daemon — so run these when
you want the catalog to catch up with what you did.

Every command below is a **dry run until you add `--execute`**, and every one is
safe to run twice.

### A. I want to import a new batch of photos

```bash
archive maintain import --root /path/to/new-photos    # ingest + date + dedup
archive review serve                                  # resolve anything ambiguous
archive materialize                                   # dry run: see the plan
archive materialize --execute                         # copy into archive/
archive verify                                        # prove nothing was lost
```

`maintain import` reads the new folder, dates each file, and dedups it against
what you already have; byte-identical newcomers never displace what's archived.
It stops short of copying so you can review first — check
`reports/date_audit_sample.csv` and the review queue, then materialize.

**Do not point `--root` at a folder inside your existing source root, or at
anything holding files the pipeline already archived** (including files you
exported *out* of `archive/`). They would be ingested a second time under a new
source id and re-materialized back into the archive. Keep exported and
still-to-import material in separate places.

### B. I moved photos around in digiKam and want the moves to count

Dragging a photo into a different `YYYY-MM` folder does **not** change its date
on its own — digiKam reads dates from metadata, not from the path. Two commands
make it real:

```bash
# 1. Quit digiKam first (it writes the same .xmp files).
archive maintain reconcile                  # dry run: explains every difference
archive maintain reconcile --execute        # adopt it — catalog only, no file touched
archive maintain apply-sidecars --execute   # write the dates/keywords into .xmp
archive verify                              # back to a clean proof
```

Then in digiKam: **Album → Reread Metadata From File** to see the changes.

Reconcile reads each destination folder as an instruction:

| You moved a photo into | It means |
|---|---|
| `2005/2005-01/` | date corrected to January 2005 |
| `caves/` | keyword `caves` |
| `caves/2006/` | keyword `caves` **and** year 2006 |
| `undated/`, `pre-2000/`, `2004-2006/` | filed coarsely, no date asserted |

A folder date that already *agrees* with the photo's timestamp confirms it and
changes nothing, so filing a photo from 14 March 2006 under `caves/2006` keeps
its precise time instead of flattening it to January 1st. When a date really is
replaced, the previous one is kept in `XMP-ArchivePipe:OriginalDate` and the
before/after pair goes in the decision log — nothing is lost, and you can always
see what a date used to be.

**Deletions:** reconcile confirms them against digiKam's `.dtrash` records, so
**reconcile before you empty digiKam's trash**. If you already emptied it, add
`--adopt-unaccounted` to record those files as deliberately deleted.

**Whole folders moved out of `archive/`:** those files still exist, so calling
them deleted would be a lie. Point reconcile at where they went and they're
recorded as *exported*, with the location they were found at:

```bash
archive maintain reconcile --exported-to /where/you/moved/them --execute
```

If reconcile ever reports files as **unaccounted**, it has adopted nothing for
them — that is the conservation law refusing to guess. Find them (they are
listed in `reports/reconcile_drift.csv`) and re-run with `--exported-to` or
`--adopt-unaccounted` once you know which it was.

## Working-tree layout

```
archive-project/
  catalog.db        single source of truth (SQLite)
  config.toml       all your settings and overrides
  archive/          the canonical output: YYYY/YYYY-MM/<name>__<hash>.<ext>
                    (+ a .xmp sidecar beside videos, RAW, and any file whose
                     metadata couldn't be written in place)
  quarantine/       losers, kept byte-identical, until you purge
                    (.purged.json marks a completed purge)
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
