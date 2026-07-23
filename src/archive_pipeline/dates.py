"""Stage 3 — Date resolution: trust hierarchy over candidates (spec §8).

Candidates per media instance: EXIF DateTimeOriginal (falling back to
CreateDate, recorded), folder-path dates (deepest matching component; curated
trust only outside takeout-derived directories), Takeout sidecar
photoTakenTime, and filename patterns. File mtime is never a candidate.

Distrust heuristics remove EXIF from auto-trust: epoch/manufacturer defaults,
mass-identical timestamps within a folder, dates predating the camera model's
era, scan-date signatures, and CreateDate-only scanned formats.

Rules R1..R7 (plus R4b for Takeout-trust folder dates) resolve or queue each
instance; every firing is logged to the decision table. Reviewed rows are never
overwritten (INV-6). Naive local times stay naive (spec section 13.8).

Usage:
    >>> from archive_pipeline.dates import resolve_dates
    >>> summary = resolve_dates(conn, cfg, wt, log)  # doctest: +SKIP
    >>> summary.by_status.get("pending", 0)
    0
"""

from __future__ import annotations

import csv
import json
import posixpath
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging import Logger
from typing import Any

from archive_pipeline.config import Config
from archive_pipeline.metadata import exif_date_to_iso
from archive_pipeline.workingtree import WorkingTree

AUDIT_REPORT = "date_audit_sample.csv"

_EPOCH_DEFAULT_DATES = frozenset({"1970-01-01", "1980-01-01", "2000-01-01"})
_SCAN_DATE_YEARS = 2  # EXIF this many years after a day/month folder = scan date
_SCANNED_MIMES = frozenset({"image/tiff", "image/png"})
_CREATE_DATE_KEYS = ("EXIF:CreateDate", "QuickTime:CreateDate", "XMP:CreateDate")

_CONFIDENCE = {
    "R1": 0.95, "R2": 0.85, "R3": 0.8, "R3f": 0.82, "R4": 0.7,
    "R4b": 0.6, "R4bf": 0.68, "R5": 0.5,
}


class DateResolveError(Exception):
    """Raised when resolution cannot run (e.g. provenance not classified)."""


@dataclass(frozen=True)
class Candidates:
    """All date candidates for one instance, with their qualifiers."""

    exif: str | None = None
    exif_from_createdate: bool = False
    folder: str | None = None  # YYYY-MM-DD (padded to precision)
    folder_precision: str | None = None  # day | month | year
    folder_trusted: bool = False  # curated LOCAL dir (Stage 2b)
    takeout: str | None = None
    takeout_is_upload_artifact: bool = False
    filename: str | None = None
    filename_precision: str | None = None


@dataclass(frozen=True)
class Resolution:
    """Outcome for one instance: resolved date or a review-queue conflict."""

    date: str | None
    precision: str | None
    source: str | None
    status: str  # auto | conflict
    confidence: float | None
    rule: str


def _valid_date(year: str, month: str | None, day: str | None) -> bool:
    try:
        datetime(int(year), int(month or 1), int(day or 1))
    except ValueError:
        return False
    return True


def folder_candidate(
    rel_path: str, patterns: tuple[str, ...]
) -> tuple[str, str] | None:
    """Deepest path component matching a folder date pattern -> (date, precision).

    Patterns are searched, so a ``^``-anchored pattern still requires the whole
    component while an unanchored one (e.g. an embedded ``YYYYMMDD``) can match
    a date buried in a name like ``card-telling_20060624``.

    Usage:
        >>> folder_candidate("2003-07/park.jpg", (r"^(?P<year>\\d{4})-(?P<month>\\d{2})$",))
        ('2003-07-01', 'month')
        >>> folder_candidate("card-telling_20060624/v.mp4",
        ...     (r"(?<![\\d])(?P<year>(19|20)\\d{2})(?P<month>0[1-9]|1[0-2])"
        ...      r"(?P<day>0[1-9]|[12]\\d|3[01])(?![\\d])",))
        ('2006-06-24', 'day')
    """
    compiled = [re.compile(p) for p in patterns]
    components = rel_path.split("/")[:-1]
    for component in reversed(components):
        for pattern in compiled:
            match = pattern.search(component)
            if not match:
                continue
            groups = match.groupdict()
            year, month, day = groups.get("year"), groups.get("month"), groups.get("day")
            if not year or not _valid_date(year, month, day):
                continue
            if day:
                return f"{year}-{month}-{day}", "day"
            if month:
                return f"{year}-{month}-01", "month"
            return f"{year}-01-01", "year"
    return None


def filename_candidate(
    rel_path: str, patterns: tuple[str, ...]
) -> tuple[str, str] | None:
    """Filename date -> (ISO date/datetime, precision second|day), else None.

    Patterns are searched (not just anchored), so a ``^``-anchored pattern still
    only matches at the start while an unanchored one (e.g. an embedded
    ``YYYYMMDD``) can match anywhere in the name. First matching pattern wins.

    Usage:
        >>> filename_candidate("x/IMG_20150418_093000.jpg",
        ...     (r"^IMG_(?P<year>\\d{4})(?P<month>\\d{2})(?P<day>\\d{2})"
        ...      r"_(?P<hour>\\d{2})(?P<minute>\\d{2})(?P<second>\\d{2})",))
        ('2015-04-18T09:30:00', 'second')
        >>> filename_candidate("x/trip_20070408_PD.jpg",
        ...     (r"(?<![\\d])(?P<year>(19|20)\\d{2})(?P<month>0[1-9]|1[0-2])"
        ...      r"(?P<day>0[1-9]|[12]\\d|3[01])(?![\\d])",))
        ('2007-04-08', 'day')
    """
    name = posixpath.basename(rel_path)
    for pattern in patterns:
        match = re.search(pattern, name)
        if not match:
            continue
        g = match.groupdict()
        year, month, day = g.get("year"), g.get("month"), g.get("day")
        if not year or not month or not _valid_date(year, month, day):
            continue
        if not day:
            return f"{year}-{month}-01", "month"
        if g.get("hour") is not None:
            hour, minute, second = g["hour"], g.get("minute", "00"), g.get("second", "00")
            if int(hour) < 24 and int(minute) < 60 and int(second) < 60:
                return f"{year}-{month}-{day}T{hour}:{minute}:{second}", "second"
        return f"{year}-{month}-{day}", "day"
    return None


def exif_candidate(exif_dto: str | None, exif: dict[str, Any]) -> tuple[str | None, bool]:
    """EXIF candidate: DateTimeOriginal, falling back to CreateDate (recorded).

    Usage:
        >>> exif_candidate("1998-07-12T14:33:05", {})
        ('1998-07-12T14:33:05', False)
        >>> exif_candidate(None, {"XMP:CreateDate": "2019:11:03 10:00:00"})
        ('2019-11-03T10:00:00', True)
    """
    if exif_dto:
        return exif_dto, False
    for key in _CREATE_DATE_KEYS:
        iso = exif_date_to_iso(value) if isinstance((value := exif.get(key)), str) else None
        if iso:
            return iso, True
    return None, False


def compute_exif_flags(
    cand_exif: str | None,
    exif_from_createdate: bool,
    mime: str | None,
    camera_model: str | None,
    camera_era: dict[str, int],
    folder: str | None,
    folder_precision: str | None,
    mass_identical: bool,
) -> list[str]:
    """Distrust heuristics; any flag removes EXIF from auto-trust (spec Stage 3).

    Usage:
        >>> compute_exif_flags("2000-01-01T00:00:00", False, "image/jpeg",
        ...                    None, {}, None, None, False)
        ['epoch_default']
    """
    if cand_exif is None:
        return []
    flags: list[str] = []
    if cand_exif[:10] in _EPOCH_DEFAULT_DATES:
        flags.append("epoch_default")
    if mass_identical:
        flags.append("mass_identical")
    year = int(cand_exif[:4])
    if camera_model and camera_model in camera_era and year < camera_era[camera_model]:
        flags.append("predates_camera")
    if (
        folder is not None
        and folder_precision in ("day", "month")
        and year - int(folder[:4]) >= _SCAN_DATE_YEARS
    ):
        flags.append("scanner_date")
    if exif_from_createdate and mime in _SCANNED_MIMES:
        flags.append("scanner_createdate")
    return flags


def _within(exif_iso: str, folder_iso: str, precision: str | None) -> bool:
    """True when a trusted candidate date is close enough to the folder date to
    use the candidate rather than flag a conflict.

    A *day*-labeled folder tolerates any day in the same month: event folders
    named with a single date are approximate, and the camera timestamp is the
    more reliable date, so a photo from a nearby day in the folder resolves to
    its own EXIF rather than becoming a spurious conflict. Month and year
    folders bracket the same month and year respectively. A different month
    (for day/month folders) or year still conflicts — that signals a real clock
    error or misfile.
    """
    if precision in ("day", "month"):
        return exif_iso[:7] == folder_iso[:7]
    return exif_iso[:4] == folder_iso[:4]


def _folder_or_filename(
    c: Candidates,
    folder_date: str,
    folder_rule: str,
    refine_rule: str,
    conflict_rule: str | None,
) -> Resolution:
    """Resolve a folder-based date, letting a filename date refine or contest it.

    With no filename date the folder date stands at folder precision. With one,
    a filename date inside the folder's granularity refines the result to the
    filename's finer precision (a ``2003`` folder with ``20030916`` in the name
    becomes day-precise).

    ``conflict_rule`` controls a filename date *outside* the folder's
    granularity. A curated folder passes a rule so the disagreement is queued
    for review (the user's placement is deliberate). A Takeout "Photos from
    YYYY" folder passes ``None``: that is only an *upload*-year bucket, too weak
    to contest a capture date, so the filename simply wins.
    """
    if not c.filename:
        return Resolution(
            folder_date, c.folder_precision, "folder", "auto",
            _CONFIDENCE[folder_rule], folder_rule,
        )
    if conflict_rule is None or _within(c.filename, folder_date, c.folder_precision):
        return Resolution(
            c.filename, c.filename_precision, "filename", "auto",
            _CONFIDENCE[refine_rule], refine_rule,
        )
    return Resolution(None, None, None, "conflict", None, conflict_rule)


def resolve(c: Candidates, flags: list[str]) -> Resolution:
    """Apply the resolution rules (first match wins) to one instance's candidates.

    Rules: R1 (EXIF within curated folder), R6 (EXIF conflicts curated folder),
    R2 (trusted EXIF), R3/R3f/R3c (curated folder, optionally filename-refined
    or -contested), R4 (Takeout sidecar), R4b/R4bf (Takeout folder, optionally
    filename-refined — a filename date always wins over Google's weak
    upload-year bucket), R5 (filename only), R7 (none).

    Usage:
        >>> resolve(Candidates(exif="1998-07-12T14:33:05", folder="1998-01-01",
        ...                    folder_precision="year", folder_trusted=True), []).rule
        'R1'
        >>> resolve(Candidates(folder="2003-01-01", folder_precision="year",
        ...                    folder_trusted=True, filename="2003-09-16",
        ...                    filename_precision="day"), ["epoch_default"]).rule
        'R3f'
    """
    exif_trusted = c.exif is not None and not flags
    curated_folder = c.folder if c.folder_trusted else None
    if exif_trusted and curated_folder:
        assert c.exif is not None
        if _within(c.exif, curated_folder, c.folder_precision):
            return Resolution(c.exif, "second", "exif", "auto", _CONFIDENCE["R1"], "R1")
        return Resolution(None, None, None, "conflict", None, "R6")
    if exif_trusted:
        return Resolution(c.exif, "second", "exif", "auto", _CONFIDENCE["R2"], "R2")
    if curated_folder:
        return _folder_or_filename(c, curated_folder, "R3", "R3f", "R3c")
    if c.takeout and not c.takeout_is_upload_artifact:
        return Resolution(c.takeout, "second", "takeout_json", "auto", _CONFIDENCE["R4"], "R4")
    if c.folder:
        return _folder_or_filename(c, c.folder, "R4b", "R4bf", None)
    if c.filename:
        return Resolution(
            c.filename, c.filename_precision, "filename", "auto", _CONFIDENCE["R5"], "R5"
        )
    return Resolution(None, None, None, "conflict", None, "R7")


@dataclass
class DateResolveSummary:
    """Aggregate outcome of one date-resolution run."""

    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    reviewed_preserved: int = 0
    sample_size: int = 0
    changed: bool = False

    @property
    def conflict_rate(self) -> float:
        return self.by_status.get("conflict", 0) / self.total if self.total else 0.0


def resolve_dates(
    conn: sqlite3.Connection,
    cfg: Config,
    wt: WorkingTree,
    log: Logger,
    sample_size: int = 200,
) -> DateResolveSummary:
    """Resolve dates for every media instance; export an audit sample.

    Usage:
        >>> summary = resolve_dates(conn, cfg, wt, log)  # doctest: +SKIP
    """
    provenance = {
        (row["source"], row["dir_path"]): row["classification"]
        for row in conn.execute("SELECT source, dir_path, classification FROM local_provenance")
    }
    has_local = conn.execute(
        "SELECT 1 FROM instance WHERE source NOT LIKE 'TAKEOUT:%' LIMIT 1"
    ).fetchone()
    if has_local and not provenance:
        raise DateResolveError(
            "LOCAL instances exist but no provenance classification; run"
            " `archive local-provenance` and review reports/local_provenance.csv first"
        )

    sidecar_by_media = {
        row["media_instance_id"]: row
        for row in conn.execute(
            "SELECT media_instance_id, photo_taken_time, creation_time"
            " FROM takeout_sidecar WHERE media_instance_id IS NOT NULL"
        )
    }
    edited_to_original = {
        row["edited_instance_id"]: row["original_instance_id"]
        for row in conn.execute("SELECT edited_instance_id, original_instance_id FROM edited_pair")
    }
    media = conn.execute(
        "SELECT id, source, rel_path, mime, exif_dto, exif_json, camera_model"
        " FROM instance WHERE kind IN ('image', 'video') ORDER BY source, rel_path"
    ).fetchall()

    exif_cands: dict[int, tuple[str | None, bool]] = {}
    timestamp_groups: Counter[tuple[str, str, str]] = Counter()
    for row in media:
        exif = json.loads(row["exif_json"] or "{}")
        cand = exif_candidate(row["exif_dto"], exif)
        exif_cands[row["id"]] = cand
        if cand[0]:
            timestamp_groups[(row["source"], posixpath.dirname(row["rel_path"]), cand[0])] += 1

    existing = {
        row["instance_id"]: row
        for row in conn.execute("SELECT * FROM date_resolution")
    }
    rows: list[dict[str, Any]] = []
    decisions: list[tuple[str, str, str]] = []
    summary = DateResolveSummary(total=len(media))
    audit_pool: list[dict[str, Any]] = []

    for row in media:
        prior = existing.get(row["id"])
        if prior is not None and prior["status"] == "reviewed":
            summary.reviewed_preserved += 1
            summary.by_status["reviewed"] = summary.by_status.get("reviewed", 0) + 1
            continue
        cand_exif, from_createdate = exif_cands[row["id"]]
        dir_path = posixpath.dirname(row["rel_path"])
        if row["source"].startswith("TAKEOUT:") or (
            provenance.get((row["source"], dir_path)) == "takeout_derived"
        ):
            trust = "takeout"
        else:
            trust = "curated"
        folder = folder_candidate(row["rel_path"], cfg.dates.folder_patterns)
        sidecar = sidecar_by_media.get(row["id"]) or sidecar_by_media.get(
            edited_to_original.get(row["id"], -1)
        )
        takeout_time = sidecar["photo_taken_time"] if sidecar else None
        upload_artifact = bool(
            sidecar
            and takeout_time is not None
            and takeout_time == sidecar["creation_time"]
        )
        filename = filename_candidate(row["rel_path"], cfg.dates.filename_patterns)
        mass = bool(
            cand_exif
            and timestamp_groups[(row["source"], dir_path, cand_exif)]
            > cfg.dedup.mass_identical_n
        )
        flags = compute_exif_flags(
            cand_exif, from_createdate, row["mime"], row["camera_model"],
            cfg.dates.camera_era, folder[0] if folder else None,
            folder[1] if folder else None, mass,
        )
        if from_createdate and cand_exif:
            flags.append("from_create_date")
        distrust_flags = [f for f in flags if f != "from_create_date"]
        candidates = Candidates(
            exif=cand_exif,
            exif_from_createdate=from_createdate,
            folder=folder[0] if folder else None,
            folder_precision=folder[1] if folder else None,
            folder_trusted=folder is not None and trust != "takeout",
            takeout=takeout_time,
            takeout_is_upload_artifact=upload_artifact,
            filename=filename[0] if filename else None,
            filename_precision=filename[1] if filename else None,
        )
        resolution = resolve(candidates, distrust_flags)
        record = {
            "instance_id": row["id"],
            "cand_exif": cand_exif,
            "cand_folder": candidates.folder,
            "cand_takeout": takeout_time,
            "cand_filename": candidates.filename,
            "folder_precision": candidates.folder_precision,
            "exif_flags": json.dumps(sorted(flags)) if flags else None,
            "resolved_date": resolution.date,
            "resolved_precision": resolution.precision,
            "resolved_source": resolution.source,
            "status": resolution.status,
            "confidence": resolution.confidence,
        }
        rows.append(record)
        summary.by_status[resolution.status] = summary.by_status.get(resolution.status, 0) + 1
        summary.by_rule[resolution.rule] = summary.by_rule.get(resolution.rule, 0) + 1
        if resolution.source:
            summary.by_source[resolution.source] = summary.by_source.get(resolution.source, 0) + 1
        decisions.append(
            (
                f"instance:{row['id']}",
                f"date.{resolution.rule}",
                json.dumps(
                    {"candidates": {"exif": cand_exif, "folder": candidates.folder,
                                    "takeout": takeout_time, "filename": candidates.filename},
                     "flags": sorted(flags), "trust": trust,
                     "resolved": resolution.date, "status": resolution.status},
                    sort_keys=True,
                ),
            )
        )
        audit_pool.append(
            {"source": row["source"], "rel_path": row["rel_path"], **record,
             "rule": resolution.rule}
        )

    def _comparable(record: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(
            record[k]
            for k in ("instance_id", "cand_exif", "cand_folder", "cand_takeout",
                      "cand_filename", "folder_precision", "exif_flags",
                      "resolved_date", "resolved_precision", "resolved_source",
                      "status", "confidence")
        )

    existing_comparable = {
        _comparable(dict(prior))
        for prior in existing.values()
        if prior["status"] != "reviewed"
    }
    summary.changed = existing_comparable != {_comparable(r) for r in rows}
    if summary.changed:
        with conn:
            conn.executemany(
                "INSERT INTO date_resolution (instance_id, cand_exif, cand_folder,"
                " cand_takeout, cand_filename, folder_precision, exif_flags,"
                " resolved_date, resolved_precision, resolved_source, status,"
                " confidence) VALUES (:instance_id, :cand_exif, :cand_folder,"
                " :cand_takeout, :cand_filename, :folder_precision, :exif_flags,"
                " :resolved_date, :resolved_precision, :resolved_source, :status,"
                " :confidence) ON CONFLICT(instance_id) DO UPDATE SET"
                " cand_exif = excluded.cand_exif, cand_folder = excluded.cand_folder,"
                " cand_takeout = excluded.cand_takeout,"
                " cand_filename = excluded.cand_filename,"
                " folder_precision = excluded.folder_precision,"
                " exif_flags = excluded.exif_flags,"
                " resolved_date = excluded.resolved_date,"
                " resolved_precision = excluded.resolved_precision,"
                " resolved_source = excluded.resolved_source,"
                " status = excluded.status, confidence = excluded.confidence",
                rows,
            )
            now = datetime.now(tz=UTC).isoformat(timespec="seconds")
            conn.executemany(
                "INSERT INTO decision (ts, stage, subject, rule, detail, actor)"
                " VALUES (?, 'date-resolve', ?, ?, ?, 'auto')",
                [(now, subject, rule, detail) for subject, rule, detail in decisions],
            )

    sample = sorted(audit_pool, key=lambda r: (r["source"], r["rel_path"]))
    rng = random.Random(0)
    if len(sample) > sample_size:
        sample = rng.sample(sample, sample_size)
        sample.sort(key=lambda r: (r["source"], r["rel_path"]))
    summary.sample_size = len(sample)
    audit_path = wt.reports_dir / AUDIT_REPORT
    with audit_path.open("w", encoding="utf-8", newline="") as fh:
        fields = ["source", "rel_path", "cand_exif", "cand_folder", "cand_takeout",
                  "cand_filename", "exif_flags", "resolved_date", "resolved_precision",
                  "resolved_source", "status", "confidence", "rule"]
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sample)

    log.info(
        "date resolution complete",
        extra={
            "total": summary.total,
            "by_status": summary.by_status,
            "by_rule": summary.by_rule,
            "conflict_rate": round(summary.conflict_rate, 4),
            "reviewed_preserved": summary.reviewed_preserved,
            "changed": summary.changed,
        },
    )
    return summary
