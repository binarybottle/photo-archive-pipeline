# photo-archive-pipeline

Turns years of scattered photos — a messy folder tree, plus any number of Google
Takeout exports — into one clean archive organized by date, with each photo's
date, location, and captions written into the file itself.

It finds duplicates, works out the real date of each photo, and proves at the end
that nothing was lost along the way.

```
Before                                After
──────                                ─────
Photos/                               archive/
  2015 vacation/                        2015/
  iphone backup old/                      2015-06/
  Takeout/Google Photos/                    2015-06-14 09.31.02__a3f9c1b2.jpg
  scans_untitled/                           2015-06-14 09.31.55__7c2e04da.jpg
  DCIM copy 2/                          2016/
  ...duplicates everywhere                  ...
                                        undated/     (dates it couldn't work out)
                                      quarantine/    (the duplicate copies)
```

**The safety promise, in one paragraph.** Your original photos are opened
read-only and never changed, moved, or deleted — not once, at any stage. Nothing
is thrown away: duplicates are set aside in a `quarantine/` folder that you
delete by hand, much later, only when you're ready. Everything the tool creates
lives inside one folder you choose; delete that folder and it's as if you never
ran it. At the end, a `verify` step re-reads every file on disk and proves every
single original is accounted for.

New here? Read [Before you start](#before-you-start), then
[Build your archive](#build-your-archive). Already have an archive? Jump to
[Everyday tasks](#everyday-tasks).

<sub>Design details live in `photo_archive_pipeline_spec.md`; contributor rules
in `CLAUDE.md`.</sub>

---

## Before you start

### What you need installed

Built and tested on **macOS**; it should work on Linux too, where only the
install commands differ.

```bash
brew install exiftool ffmpeg      # macOS
```

- **exiftool** — required. It's the only thing that reads or writes photo metadata.
- **ffmpeg** — required if you have any videos. It fingerprints them so duplicate
  videos can be found. **Install it before you start**, or videos get flagged as
  unfingerprintable.
- **Python 3.12+** and [Poetry](https://python-poetry.org).

```bash
git clone <this repo> && cd photo-archive-pipeline
poetry install
```

### How much disk space

You need room for **roughly your whole collection a second time**, in one place.

Here's why: the tool builds a deduplicated `archive/`, *and* keeps every
duplicate it set aside in `quarantine/`. Added together, those come to about the
size of what you started with.

> **If you put that folder on the same drive as your photos, you need about 2×
> your collection free on that drive.**

It can be your internal drive or an external one — but **not the same drive as
your backup**, which needs to survive this drive failing. Avoid FAT/exFAT
volumes. You'll get an exact number before anything is written: the `materialize`
step prints the precise space required and refuses to start without enough room.
If space runs low mid-run it pauses cleanly and you can resume.

---

## One-time setup

Tell the tool where its working folder is, once per terminal session, so every
command afterwards stays short:

```bash
cd /path/to/photo-archive-pipeline
export ARCHIVE_WORKING_TREE=/path/to/archive-project   # the tool's own folder
alias archive="poetry run archive"
```

Every `archive …` command in this README is shorthand for `poetry run archive …`,
and each one acts on `ARCHIVE_WORKING_TREE`. (You can also pass
`--working-tree /path/…` to any command instead.)

That "working folder" — `archive-project` above — is where everything the tool
creates lives. Pick a location with the space described above. It does **not**
have to be near your photos.

---

## Build your archive

Run these in order, top to bottom.

**Every step is safe to re-run.** Running a finished step again does nothing, and
re-running never throws away decisions you made. If a long step is interrupted,
run the exact same command again and it picks up where it stopped.

Steps marked **👤 you decide** need your input before you move on.

### 1. Create the working folder

```bash
archive init
```

Creates `catalog.db` (the tool's record of everything), `config.toml` (your
settings), and empty `archive/ quarantine/ review/ reports/ logs/ staging/`
folders. Safe to run anytime.

### 2. Back up your photos, then unlock the tool — **required, once**

Copy every source folder to a **separate physical disk** first. Time Machine
counts, as long as it isn't set to exclude your photos.

Then open `config.toml` and change:

```toml
[preserve]
confirmed = true
```

The next step refuses to run until you do. This tool genuinely never touches your
originals — but only a real backup protects you if the drive itself dies
halfway through.

### 3. Read in your photos

```bash
# your own photo folder:
caffeinate -i archive ingest --source LOCAL --root /path/to/photos

# each Google Takeout export is a SEPARATE source — a .zip or an unzipped folder:
caffeinate -i archive ingest --source TAKEOUT --root /path/to/takeout.zip --export-id 2024
```

Records every file: its fingerprint, its embedded photo info, and a visual
signature used later to spot near-duplicates.

**This is the slow one** — expect it to run overnight for a big collection.
`caffeinate -i` stops your Mac sleeping through it. If it's interrupted, just run
the same command again. Your photos are only ever read.

### 4. Match up Google's sidecar files — *only if you used Takeout*

```bash
archive takeout-normalize
```

Google exports each photo's date and caption in a separate `.json` file. This
pairs them back up with their photos.

**Skip this step entirely if you only have your own folders.** (Old Takeout
content already sitting *inside* your own photo folder is handled by step 5, not
here.)

### 5. Sort your own folders from old Google dumps — **👤 you decide**

```bash
archive local-provenance
```

Works out which of your folders you organized yourself, versus which are old
Google exports you dumped somewhere and forgot. This matters because your own
folder names are trustworthy evidence of a date, and Google's aren't.

**👤 Then:** open `reports/local_provenance.csv` and skim it. If something is
labelled wrong, add its path under `[provenance]` in `config.toml` — as
`curated_overrides` (it's yours) or `takeout_derived_overrides` (it's a Google
dump) — and run the step again.

### 6. Work out each photo's date — **👤 you decide**

```bash
archive date-resolve
```

Gives every photo a date, preferring the most trustworthy source available: a
date in one of *your* folder names beats the camera's own timestamp, which beats
Google's. It prints how many it settled automatically and how many need you, and
writes `reports/date_audit_sample.csv` so you can spot-check its work.

**👤 Then:** settle the conflicts in the review app (step 8). This is optional —
anything you leave undecided is filed under `archive/undated/`, where it's easy
to find later. Nothing is lost either way.

### 7. Find the duplicates

```bash
archive dedup
```

Groups identical and near-identical photos, picks the best copy of each group,
and marks the others to be set aside. Anything it isn't confident about is left
for you.

**👤 Then:** review those groups in the app (step 8). **This one is required** —
step 10 refuses to run while any group is still undecided.

### 8. Review — the part only you can do

```bash
archive review serve          # then open http://127.0.0.1:8765
```

Run this in its own terminal window. It's a small web app on your own machine
(nothing is exposed to the internet). You'll find two queues:

- **Date conflicts** — each group shows the competing dates with one-click "use
  the folder's date" / "trust the camera" buttons. The manual box accepts a year
  (`2007`), a year-month (`2007-08`), a full date, or a full timestamp, and
  works out how precise you were being.
- **Duplicate groups** — accept the suggested keeper, pick a different one, split
  the group, or say it isn't a duplicate at all.

Bulk buttons clear the common cases at once: "prefer my own copies over
Takeout's", "accept the rest", and a separate "accept video groups too" (videos
are held back from the general accept so they get a second look).

Everything you decide is saved immediately and survives re-running earlier steps.
Stop and come back whenever you like.

### 9. Preview the result — nothing is written yet

```bash
archive materialize            # writes NO photos
```

Shows you exactly what the real run will do, and prints **precisely how much disk
space** it needs. It produces three files in `reports/`:

- `archive_manifest.csv` — every photo and where it will land
- `quarantine_manifest.csv` — every duplicate being set aside, and why
- `keyword_map.csv` — your topical folder names, ready to become tags

**👤 Then:** open `reports/keyword_map.csv` and decide what each folder name
should become — `keep` it as a tag, `rename` it to something better, or `drop` it.
Skim the other two to confirm things are landing where you expect.

### 10. Build it — the only step that writes photos

```bash
archive materialize --execute
```

Copies each keeper into `archive/`, writes its date, location and tags into the
file, and copies the duplicates into `quarantine/`.

For JPEG, HEIC, PNG and TIFF the information goes inside the file, and the copy
is re-read afterwards to confirm it's intact. For videos, RAW files, and anything
whose internal structure is risky to rewrite, **the file's bytes are left
completely untouched** and the information goes into a small companion `.xmp`
file next to it. It tells you how many took that route.

Checks disk space before starting, resumable if interrupted, and your originals
still haven't been touched.

### 11. Prove nothing was lost

```bash
archive verify
archive report
```

`verify` re-reads every file in `archive/` and `quarantine/`, checks each one
against its recorded fingerprint, and confirms every original you started with is
accounted for. It's thorough, so it takes a while on a big archive. If anything
is missing or altered it says exactly what, and exits with an error.

`report` prints a plain summary — how many photos, from where, dated how, and
whether the last `verify` passed.

---

## Everyday tasks

Your archive is built. Now you just live with it. Four things come up:

1. [I have new photos to add](#1-i-have-new-photos-to-add)
2. [I moved photos into different folders](#2-i-moved-photos-into-different-folders)
3. [I deleted photos I don't want](#3-i-deleted-photos-i-dont-want)
4. [I moved a whole folder out of the archive](#4-i-moved-a-whole-folder-out-of-the-archive)

### First, the one idea behind all of them

The tool **does not watch your archive folder**. It has no idea you moved,
renamed, or deleted anything until you tell it. So whenever you change things —
usually in digiKam — you run a command afterwards to say "here's what I did."

That command is `archive maintain reconcile`. It looks at your archive, works out
what changed, and updates its records.

**None of this can hurt your photos.** The only command that writes photo files
is `materialize`, and it just shows you the plan until you add `--execute`.
`reconcile` never moves, edits, or deletes a photo at all — it only updates the
tool's own records. Running any of these twice is harmless.

### The two commands you'll use most

| Command | What it means |
|---|---|
| `archive maintain reconcile` | "Here's what I changed — write it down." |
| `archive maintain apply-sidecars` | "Now make those changes visible in digiKam." |

You usually run them in that order, one after the other.

---

### 1. I have new photos to add

**Do this:**

```bash
archive maintain import --root /path/to/new-photos
archive review serve            # open http://127.0.0.1:8765, clear the queues
archive materialize             # shows the plan, writes nothing
archive materialize --execute   # actually copies them in
archive verify                  # confirms nothing was lost
```

**What's happening:** the first command reads the new folder, works out each
photo's date, and checks it against everything you already have so you don't end
up with duplicates. It deliberately stops before copying anything, so you get to
look first. The review step settles anything it wasn't sure about. Then
`materialize --execute` copies the photos in.

⚠️ **One thing to avoid:** don't point `--root` at a folder that already contains
photos from your archive. The tool would treat them as brand-new photos and copy
them in a second time. Keep new, not-yet-imported photos in their own folder,
separate from anything the archive already knows about.

---

### 2. I moved photos into different folders

Say you noticed some photos are filed under the wrong month, so you dragged them
into `2005/2005-01/` in digiKam.

**Here's the catch:** moving a photo does **not** change its date. digiKam gets
dates from information stored *inside* each photo, not from which folder it sits
in. So the photo looks fixed in that one view, while its date is still wrong
everywhere else — and it will keep sorting into the wrong place.

These two commands make the move real:

```bash
# Quit digiKam first — it and this command write the same companion files.
archive maintain reconcile --execute
archive maintain apply-sidecars --execute
```

Then reopen digiKam and choose **Album → Reread Metadata From File** to see the
corrected dates.

**What folder names mean.** `reconcile` reads the folder you moved a photo into
and treats it as an instruction:

| You moved a photo into… | …and it means |
|---|---|
| `2005/2005-01/` | this photo is from January 2005 |
| `caves/` | tag this photo `caves` |
| `caves/2006/` | tag it `caves`, **and** it's from 2006 |
| `undated/` or `pre-2000/` | I don't know the date; file it loosely |

**It won't make your dates worse.** If a photo already has a precise timestamp —
say 14 March 2006, 3:42pm — and you file it under `caves/2006`, the folder simply
*agrees* with what's there, so the exact time is kept rather than being blurred to
"sometime in 2006". A date is only replaced when the folder genuinely disagrees.

**And you can always look up the old value.** Whenever a date is replaced, the
previous one is saved in the photo's companion `.xmp` file and written to the
tool's history log.

---

### 3. I deleted photos I don't want

When you delete a photo in digiKam it isn't really gone — it moves to a hidden
trash folder inside your archive, and digiKam records where it came from. That
record is how `reconcile` tells "the owner deleted this on purpose" apart from "a
file mysteriously vanished", which is exactly the sort of thing you'd want to be
warned about.

So **reconcile first, empty the trash second**:

```bash
archive maintain reconcile --execute    # 1. record the deletions
                                        # 2. now in digiKam: Delete → Empty Trash
archive verify                          # 3. confirms everything still adds up
```

Emptying the trash afterwards is worth doing — it sits inside your archive folder
and can grow to many gigabytes.

**Already emptied the trash? Nothing is broken.** The records are gone, but you
still know you meant to delete those photos. Say so:

```bash
archive maintain reconcile --adopt-unaccounted --execute
```

Either way, deleted photos are marked as deliberately removed, and `verify` keeps
passing.

---

### 4. I moved a whole folder out of the archive

Sometimes you decide a batch of photos doesn't belong in the archive at all, and
drag the folder somewhere else. Those photos **still exist**, so recording them
as deleted would be untrue. Tell the tool where they went:

```bash
archive maintain reconcile --exported-to /where/you/moved/them --execute
```

It finds them there and notes their new home, so the records stay honest about
where every photo ended up.

⚠️ Afterwards, don't run `maintain import --root` on that folder — see task 1. It
would pull those photos straight back into the archive you just took them out of.

---

### If it says "unaccounted"

Sometimes `reconcile` reports that photos are **unaccounted for**: they're missing
from the archive and it can't tell why. When that happens it changes nothing and
waits for you — it would rather ask than guess about your photos.

The missing files are listed in `reports/reconcile_drift.csv`. Look at a few,
work out what happened, and run the matching command:

- **You moved them somewhere** → `reconcile --exported-to /that/place --execute`
- **You deleted them** (and emptied the trash) → `reconcile --adopt-unaccounted --execute`
- **Neither?** Then something really is wrong. Restore those files from your
  backup before doing anything else.

---

## Looking after your archive

### Browse it in digiKam

[digiKam](https://www.digikam.org) is a free photo manager. Point a digiKam
collection at your `archive/` folder — that folder only — then set it up so it
reads what this tool wrote and never damages your files.

In **Settings → Configure digiKam → Metadata**:

**Sidecars tab**
- ✅ **Enable "Read from sidecar files."** Without this, digiKam shows no dates or
  tags at all for videos and RAW files, because theirs live in companion files.
- ❌ **Leave "Sidecar file names are compatible with commercial programs"
  unchecked.** This tool writes `photo.jpg.xmp`; ticking that box makes digiKam
  look for `photo.xmp` and miss every one of them.
- ✅ **Enable "Write to sidecar files"** and choose **"Write to XMP sidecar
  only."** Now every tag, rating and face you add goes into a companion file and
  never rewrites the photo — so your archive stays byte-for-byte intact, `verify`
  keeps passing, and a synced folder isn't constantly re-uploading photos.

**Rotation tab**
- ✅ Choose **"Rotate by only setting a flag"**, not "changing the content".

**Also**
- Keep digiKam's **own database on your local drive, never inside the archive**
  and never in a synced folder — a live database will corrupt if two machines
  touch it.
- Prefer **tags, ratings, labels, faces and saved searches** over moving files
  around. A photo lives in one folder but can carry any number of tags, and
  tagging never rewrites the photo. Moving and deleting are still perfectly fine —
  just run `maintain reconcile` afterwards, per [Everyday tasks](#everyday-tasks).
- Leave `catalog.db` alone. It's the tool's record of everything and is still
  needed by `maintain import` and `verify`.
- digiKam's own duplicate finder makes a good independent second opinion on the
  deduplication.

### Keep the archive somewhere else

You can move `archive/` anywhere — a bigger drive, a synced folder. The tool
still expects to find it at `<working-folder>/archive`, so leave a signpost
rather than copying it:

```bash
ln -s /wherever/you/moved/archive /path/to/archive-project/archive
```

### Back it up

Keep three copies of `archive/`, `catalog.db` and `reports/` on at least two
kinds of storage, one of them off-site (restic and borg are good tools for this).
Re-check that nothing has quietly rotted, now and then:

```bash
archive maintain verify-checksums
```

### Reclaim the quarantine space — carefully

This is the one command that permanently destroys files: the set-aside duplicates,
often hundreds of gigabytes. Do it only when **`verify` passes** and **a real
backup of your originals exists** — that backup, not the quarantine, is your
safety net afterwards.

It refuses unless the last `verify` passed, lists exactly what it will destroy,
and makes you type `PURGE QUARANTINE`:

```bash
archive maintain purge-quarantine
```

If the prompt won't take your typing (some terminals), pipe it in:

```bash
echo "PURGE QUARANTINE" | archive maintain purge-quarantine
```

It leaves a marker behind so later checks know the quarantine was emptied
deliberately and don't report it as damage. There's no rush — waiting six months
is a perfectly good plan.

---

## What's in the working folder

```
archive-project/
  catalog.db        the record of every file and every decision (SQLite)
  config.toml       all your settings
  archive/          the result: YYYY/YYYY-MM/<name>__<fingerprint>.<ext>
                    (+ a .xmp companion beside videos, RAW, and any file
                     whose metadata couldn't safely be written inside)
  quarantine/       the duplicate copies, kept byte-for-byte, until you purge
  reports/          manifests, spot-check samples, verify results (CSV/JSON)
  review/           thumbnails for the review app
  logs/             detailed run logs
  staging/          unzipped Takeout exports
```

Every photo filename ends in `__` plus a short fingerprint of its contents. That's
what lets the tool recognize a file after you've moved it, and guarantees two
different photos never collide over one name.

---

## Development

```bash
poetry run pytest          # test suite
poetry run ruff check src tests
poetry run mypy
poetry run archive fixtures generate --dest /tmp/corpus   # synthetic test photos
```
