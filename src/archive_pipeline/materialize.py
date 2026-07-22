"""Stage 6 — Materialize: write the archive and quarantine (spec §8).

The only stage that mutates the filesystem (beyond staging extraction), and it
only ever writes inside the working tree. Dry-run (the default, INV-4)
produces the manifests and keyword map with zero writes outside ``reports/``;
``--execute`` performs atomic copy-verify-rename operations, writes metadata
through exiftool (the only tool allowed to touch metadata; RAW and other risky
formats get an ``.xmp`` sidecar with untouched bytes, INV-7), quarantines
every loser byte-identically under its content hash, and records everything in
the ``placement`` ledger. Interrupted runs resume: verified placements are
skipped, stale temp files are discarded (INV-5). INV-9 disk-space preflight
runs before bulk copying and re-checks periodically.

Usage:
    >>> from archive_pipeline.materialize import run_materialize
    >>> summary = run_materialize(conn, cfg, wt, log, execute=False)  # doctest: +SKIP
    >>> summary.executed
    False
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import posixpath
import random
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path
from typing import Any

from exiftool import ExifToolHelper

from archive_pipeline import __version__
from archive_pipeline.config import Config
from archive_pipeline.space import require_space
from archive_pipeline.workingtree import WorkingTree

ARCHIVE_MANIFEST = "archive_manifest.csv"
QUARANTINE_MANIFEST = "quarantine_manifest.csv"
EXCLUDED_MANIFEST = "excluded_manifest.csv"
KEYWORD_MAP = "keyword_map.csv"
QUARANTINE_INDEX = "index.jsonl"

#: Formats exiftool rewrites metadata-only, safely, in place (INV-7). Anything
#: else (RAW, videos, unknown) keeps its bytes untouched + an .xmp sidecar.
SAFE_WRITE_MIMES = frozenset(
    {"image/jpeg", "image/png", "image/tiff", "image/heic", "image/heif"}
)
_PHOTOS_FROM_RE = re.compile(r"^Photos from \d{4}$")
_SCAFFOLD_DIRS = frozenset({"Takeout", "Google Photos"})
_SAMPLE_FRACTION = 0.01
_SPACE_RECHECK_MIN_ITEMS = 25


class MaterializeError(Exception):
    """Raised for unmet gates, copy verification failures, or write errors."""


def exiftool_config_path() -> Path:
    """Path to the shipped ArchivePipe exiftool config file.

    Usage:
        >>> exiftool_config_path().name
        'archivepipe.config'
    """
    return Path(__file__).parent / "data" / "archivepipe.config"


def dest_rel_path(
    resolved_date: str | None,
    precision: str | None,
    undated_dir: str,
    rel_path: str,
    sha256: str,
) -> str:
    """Archive-relative destination: ``YYYY/YYYY-MM/<stem>__<sha8><ext>``.

    Year-precision dates land in ``YYYY/`` directly; unknown dates in the
    configured undated directory (spec Stage 6.2).

    Usage:
        >>> dest_rel_path("1998-07-12T14:33:05", "second", "undated",
        ...               "a/beach.jpg", "ab12cd34" * 8)
        '1998/1998-07/beach__ab12cd34.jpg'
        >>> dest_rel_path("1998-01-01", "year", "undated", "a/b.png", "ff" * 32)
        '1998/b__ffffffff.png'
        >>> dest_rel_path(None, None, "undated", "a/b.png", "ff" * 32)
        'undated/b__ffffffff.png'
    """
    stem, ext = posixpath.splitext(posixpath.basename(rel_path))
    name = f"{stem}__{sha256[:8]}{ext.lower()}"
    if not resolved_date:
        return f"{undated_dir}/{name}"
    year = resolved_date[:4]
    if precision == "year":
        return f"{year}/{name}"
    return f"{year}/{resolved_date[:7]}/{name}"


def quarantine_rel_path(sha256: str, rel_path: str) -> str:
    """Quarantine-relative path: ``<sha[:2]>/<sha>__<basename>``.

    Usage:
        >>> quarantine_rel_path("ab" * 32, "x/y.jpg")[:6]
        'ab/aba'
    """
    return f"{sha256[:2]}/{sha256}__{posixpath.basename(rel_path)}"


def exiftool_date(resolved_iso: str, precision: str | None) -> str:
    """Resolved ISO date -> exiftool form, padded to a full timestamp.

    Coarser precisions are padded (recorded in ArchivePipe:DatePrecision, so
    nothing is lost); timezone offsets are dropped (naive-time policy).

    Usage:
        >>> exiftool_date("1998-07-12T14:33:05", "second")
        '1998:07:12 14:33:05'
        >>> exiftool_date("1998-01-01", "year")
        '1998:01:01 00:00:00'
    """
    date_part = resolved_iso[:10].replace("-", ":")
    time_part = resolved_iso[11:19] if len(resolved_iso) >= 19 else "00:00:00"
    return f"{date_part} {time_part}"


# --- Keyword map workflow (spec Stage 6.1) --------------------------------------


def folder_keyword_candidates(
    rel_path: str, folder_patterns: tuple[str, ...]
) -> list[str]:
    """Topical (non-date, non-scaffolding) folder components of one path.

    Usage:
        >>> folder_keyword_candidates("topical/vacations/x.jpg", ())
        ['topical', 'vacations']
    """
    compiled = [re.compile(p) for p in folder_patterns]
    out = []
    for component in posixpath.dirname(rel_path).split("/"):
        if not component or component in _SCAFFOLD_DIRS:
            continue
        if _PHOTOS_FROM_RE.match(component) or any(p.match(component) for p in compiled):
            continue
        out.append(component)
    return out


def write_keyword_map(path: Path, candidates: set[str]) -> None:
    """Create the editable keyword map with default keep-as-is proposals.

    Usage:
        >>> write_keyword_map(Path("keyword_map.csv"), {"vacations"})  # doctest: +SKIP
    """
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["folder_name", "proposed_keyword", "action"])
        for name in sorted(candidates):
            writer.writerow([name, name, "keep"])


def load_keyword_map(path: Path) -> dict[str, str | None]:
    """Parse the user-edited keyword map: folder name -> keyword (None = drop).

    ``keep`` and ``rename`` both apply ``proposed_keyword`` (the user edits it
    for renames); hierarchies like ``Travel/Vietnam`` pass through verbatim.

    Usage:
        >>> load_keyword_map(Path("keyword_map.csv"))  # doctest: +SKIP
        {'vacations': 'Travel/Vacations'}
    """
    mapping: dict[str, str | None] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            action = (row.get("action") or "keep").strip().lower()
            name = (row.get("folder_name") or "").strip()
            if not name:
                continue
            if action == "drop":
                mapping[name] = None
            elif action in ("keep", "rename"):
                mapping[name] = (row.get("proposed_keyword") or name).strip() or name
            else:
                raise MaterializeError(
                    f"keyword map: unknown action {action!r} for {name!r}"
                )
    return mapping


# --- Planning -------------------------------------------------------------------


@dataclass
class PlanItem:
    """One instance's materialization plan."""

    instance_id: int
    source: str
    rel_path: str
    sha256: str
    size_bytes: int
    mime: str | None
    disposition: str  # archive | quarantine | excluded
    reason: str
    dest_rel: str | None = None  # within archive/ or quarantine/
    use_sidecar: bool = False
    cluster_id: int | None = None
    winner_dest: str | None = None  # for quarantined losers
    resolved_date: str | None = None
    resolved_precision: str | None = None
    resolved_source: str | None = None
    sequence_hint: int | None = None
    gps: tuple[float, float] | None = None
    gps_flagged: bool = False
    description: str | None = None
    keywords: list[str] = field(default_factory=list)
    rating: int | None = None
    merged_from: list[str] = field(default_factory=list)


@dataclass
class MaterializeSummary:
    """Aggregate outcome of one materialize run."""

    executed: bool = False
    archived: int = 0
    quarantined: int = 0
    quarantine_copies: int = 0  # distinct hashes physically copied
    excluded: int = 0
    sidecars_written: int = 0
    undated: int = 0
    skipped_done: int = 0
    bytes_planned: int = 0
    keyword_map_created: bool = False
    sample_checked: int = 0


def _gates(conn: sqlite3.Connection) -> None:
    pending = conn.execute(
        "SELECT COUNT(*) FROM cluster WHERE status = 'pending'"
    ).fetchone()[0]
    if pending:
        raise MaterializeError(
            f"{pending} cluster(s) still await review — resolve them in"
            " `archive review serve` before materializing"
        )
    unresolved = conn.execute(
        "SELECT COUNT(*) FROM instance i LEFT JOIN date_resolution d"
        " ON d.instance_id = i.id WHERE i.kind IN ('image', 'video')"
        " AND d.instance_id IS NULL"
    ).fetchone()[0]
    if unresolved:
        raise MaterializeError(
            f"{unresolved} media instance(s) lack date resolution — run"
            " `archive date-resolve` first"
        )


def build_plan(
    conn: sqlite3.Connection, cfg: Config, keyword_map: dict[str, str | None]
) -> list[PlanItem]:
    """Compute every instance's disposition, destination, and metadata.

    Usage:
        >>> items = build_plan(conn, cfg, {})  # doctest: +SKIP
    """
    _gates(conn)
    dr = {
        row["instance_id"]: row
        for row in conn.execute("SELECT * FROM date_resolution")
    }
    membership = {
        row["instance_id"]: row
        for row in conn.execute(
            "SELECT m.instance_id, m.role, c.id AS cluster_id, c.kind,"
            " c.winner_instance_id FROM cluster_member m"
            " JOIN cluster c ON c.id = m.cluster_id"
        )
    }
    merges = {
        row["cluster_id"]: json.loads(row["merged_json"])
        for row in conn.execute("SELECT cluster_id, merged_json FROM cluster_merge")
    }
    sidecars = {
        row["media_instance_id"]: row
        for row in conn.execute(
            "SELECT media_instance_id, gps_lat, gps_lon, description"
            " FROM takeout_sidecar WHERE media_instance_id IS NOT NULL"
        )
    }
    albums: dict[int, list[str]] = {}
    for row in conn.execute("SELECT media_instance_id, album FROM album_membership"):
        albums.setdefault(row["media_instance_id"], []).append(row["album"])
    edited_ids = set()
    original_ids = set()
    for row in conn.execute(
        "SELECT edited_instance_id, original_instance_id FROM edited_pair"
    ):
        edited_ids.add(row["edited_instance_id"])
        original_ids.add(row["original_instance_id"])
    cluster_members: dict[int, list[int]] = {}
    for iid, m in membership.items():
        cluster_members.setdefault(m["cluster_id"], []).append(iid)

    def _cluster_has(cluster_id: int | None, ids: set[int]) -> bool:
        if cluster_id is None:
            return False
        return any(i in ids for i in cluster_members.get(cluster_id, []))

    def _map_keywords(raw: list[str]) -> list[str]:
        mapped = []
        for kw in raw:
            value = keyword_map.get(kw, kw)
            if value:
                mapped.append(value)
        return sorted(set(mapped))

    items: list[PlanItem] = []
    winner_dest_by_cluster: dict[int, str] = {}
    rows = conn.execute(
        "SELECT * FROM instance ORDER BY source, rel_path"
    ).fetchall()
    for row in rows:
        iid = row["id"]
        flags = json.loads(row["flags"] or "[]")
        base = dict(
            instance_id=iid, source=row["source"], rel_path=row["rel_path"],
            sha256=row["sha256"], size_bytes=row["size_bytes"], mime=row["mime"],
        )
        if row["kind"] == "sidecar_json":
            items.append(
                PlanItem(**base, disposition="excluded", reason="takeout_sidecar")
            )
            continue
        if row["kind"] == "other":
            items.append(PlanItem(**base, disposition="excluded", reason="non_media"))
            continue
        if row["kind"] == "sidecar_xmp":
            items.append(
                PlanItem(
                    **base, disposition="quarantine", reason="prior_xmp_sidecar",
                    dest_rel=quarantine_rel_path(row["sha256"], row["rel_path"]),
                )
            )
            continue
        if "corrupt" in flags or "zero_byte" in flags:
            items.append(
                PlanItem(
                    **base, disposition="quarantine", reason="corrupt",
                    dest_rel=quarantine_rel_path(row["sha256"], row["rel_path"]),
                )
            )
            continue

        member = membership.get(iid)
        role = member["role"] if member else None
        cluster_id = member["cluster_id"] if member else None
        if role == "loser":
            items.append(
                PlanItem(
                    **base, disposition="quarantine", reason="cluster_loser",
                    dest_rel=quarantine_rel_path(row["sha256"], row["rel_path"]),
                    cluster_id=cluster_id,
                )
            )
            continue

        # Archive path: winner, companion, not_duplicate member, or singleton.
        resolution = dr.get(iid)
        resolved_date = resolution["resolved_date"] if resolution else None
        resolved_precision = resolution["resolved_precision"] if resolution else None
        resolved_source = resolution["resolved_source"] if resolution else None
        merge = merges.get(cluster_id) if role == "winner" else None
        if merge and merge["date"]["date"]:
            resolved_date = merge["date"]["date"]
            resolved_precision = merge["date"]["precision"]
            resolved_source = merge["date"]["source"]

        gps: tuple[float, float] | None = None
        gps_flagged = False
        if merge and merge["gps"]["lat"] is not None:
            gps = (merge["gps"]["lat"], merge["gps"]["lon"])
            gps_flagged = merge["gps"]["source"] == "takeout"
        elif row["gps_lat"] is not None:
            gps = (row["gps_lat"], row["gps_lon"])
        elif iid in sidecars and sidecars[iid]["gps_lat"] is not None:
            gps = (sidecars[iid]["gps_lat"], sidecars[iid]["gps_lon"])
            gps_flagged = True

        if merge:
            descriptions = merge["descriptions"]
            raw_keywords = list(merge["keyword_candidates"])
            merged_from = sorted(
                f"{by['source']}:{by['rel']}"
                for by in (
                    {"source": r["source"], "rel": r["rel_path"]}
                    for r in conn.execute(
                        "SELECT i.source, i.rel_path FROM cluster_member m"
                        " JOIN instance i ON i.id = m.instance_id"
                        " WHERE m.cluster_id = ?",
                        (cluster_id,),
                    )
                )
            )
        else:
            descriptions = (
                [sidecars[iid]["description"]]
                if iid in sidecars and sidecars[iid]["description"]
                else []
            )
            raw_keywords = folder_keyword_candidates(
                row["rel_path"], cfg.dates.folder_patterns
            ) + albums.get(iid, [])
            merged_from = []

        keywords = _map_keywords(raw_keywords)
        rating: int | None = None
        if iid in edited_ids or _cluster_has(cluster_id, edited_ids):
            keywords = sorted({*keywords, "edited-preferred"})
            rating = 4
        elif iid in original_ids or _cluster_has(cluster_id, original_ids):
            keywords = sorted({*keywords, "has-edit"})

        if cfg.policy.edited == "quarantine_edits" and (
            iid in edited_ids or _cluster_has(cluster_id, edited_ids)
        ):
            items.append(
                PlanItem(
                    **base, disposition="quarantine", reason="edited_version",
                    dest_rel=quarantine_rel_path(row["sha256"], row["rel_path"]),
                    cluster_id=cluster_id,
                )
            )
            continue

        dest = dest_rel_path(
            resolved_date, resolved_precision, cfg.policy.undated_placement,
            row["rel_path"], row["sha256"],
        )
        item = PlanItem(
            **base, disposition="archive",
            reason=role or "singleton",
            dest_rel=dest,
            use_sidecar=(row["mime"] not in SAFE_WRITE_MIMES),
            cluster_id=cluster_id,
            resolved_date=resolved_date,
            resolved_precision=resolved_precision,
            resolved_source=resolved_source,
            sequence_hint=resolution["sequence_hint"] if resolution else None,
            gps=gps, gps_flagged=gps_flagged,
            description="; ".join(descriptions) if descriptions else None,
            keywords=keywords,
            rating=rating,
            merged_from=merged_from,
        )
        items.append(item)
        if role == "winner" and cluster_id is not None:
            winner_dest_by_cluster[cluster_id] = dest

    for item in items:
        if item.disposition == "quarantine" and item.cluster_id is not None:
            item.winner_dest = winner_dest_by_cluster.get(item.cluster_id)
    return items


# --- Execution ------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(src: Path, dest: Path, expected_sha: str) -> Path:
    """Copy to a temp file beside ``dest`` and verify byte-identity (INV-7).

    Returns the temp path; the caller finishes with an atomic rename.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.parent / f".tmp-{dest.name}"
    shutil.copyfile(src, temp)
    actual = _sha256_file(temp)
    if actual != expected_sha:
        temp.unlink(missing_ok=True)
        raise MaterializeError(
            f"copy verification failed for {src}: {actual} != {expected_sha}"
        )
    return temp


def _metadata_args(item: PlanItem) -> list[str]:
    """The exiftool assignments for one archived item (one batch per file)."""
    args: list[str] = []
    if item.resolved_date:
        stamp = exiftool_date(item.resolved_date, item.resolved_precision)
        args += [
            f"-DateTimeOriginal={stamp}",
            f"-CreateDate={stamp}",
            f"-ModifyDate={stamp}",
        ]
    if item.gps is not None:
        lat, lon = item.gps
        args += [
            f"-GPSLatitude={abs(lat)}",
            f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(lon)}",
            f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
        ]
    if item.description:
        args.append(f"-XMP-dc:Description={item.description}")
    for keyword in item.keywords:
        args.append(f"-XMP-dc:Subject+={keyword}")
    if item.rating is not None:
        args.append(f"-XMP:Rating={item.rating}")
    if item.resolved_source:
        args.append(f"-XMP-ArchivePipe:DateSource={item.resolved_source}")
    if item.resolved_precision:
        args.append(f"-XMP-ArchivePipe:DatePrecision={item.resolved_precision}")
    for merged in item.merged_from:
        args.append(f"-XMP-ArchivePipe:MergedFrom+={merged}")
    args.append(f"-XMP-ArchivePipe:SourcePath={item.source}:{item.rel_path}")
    args.append(f"-XMP-ArchivePipe:PipelineVersion={__version__}")
    if item.sequence_hint is not None:
        args.append(f"-XMP-ArchivePipe:SequenceHint={item.sequence_hint}")
    return args


def _exiftool_write(et: ExifToolHelper, args: list[str], target: Path) -> None:
    output = et.execute("-overwrite_original", *args, str(target))
    if "1 image files updated" not in output and "1 files updated" not in output:
        raise MaterializeError(f"exiftool write failed for {target}: {output.strip()}")


def _exiftool_sidecar(et: ExifToolHelper, args: list[str], media: Path) -> Path:
    sidecar = media.with_suffix(media.suffix + ".xmp")
    sidecar.unlink(missing_ok=True)
    output = et.execute("-o", str(sidecar), *args, str(media))
    if not sidecar.exists():
        raise MaterializeError(
            f"exiftool sidecar write failed for {media}: {output.strip()}"
        )
    return sidecar


def _write_manifests(
    wt: WorkingTree, items: list[PlanItem], dest_shas: dict[int, str]
) -> None:
    with (wt.reports_dir / ARCHIVE_MANIFEST).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["dest_path", "source", "source_path", "source_sha256", "dest_sha256",
             "resolved_date", "precision", "date_source", "keywords", "sidecar"]
        )
        for item in items:
            if item.disposition != "archive":
                continue
            writer.writerow(
                [f"archive/{item.dest_rel}", item.source, item.rel_path,
                 item.sha256, dest_shas.get(item.instance_id, ""),
                 item.resolved_date or "", item.resolved_precision or "",
                 item.resolved_source or "", ";".join(item.keywords),
                 "yes" if item.use_sidecar else ""]
            )
    with (wt.reports_dir / QUARANTINE_MANIFEST).open(
        "w", encoding="utf-8", newline=""
    ) as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["quarantine_path", "source", "source_path", "sha256", "reason",
             "cluster_id", "winner_dest"]
        )
        for item in items:
            if item.disposition != "quarantine":
                continue
            writer.writerow(
                [f"quarantine/{item.dest_rel}", item.source, item.rel_path,
                 item.sha256, item.reason, item.cluster_id or "",
                 item.winner_dest or ""]
            )
    with (wt.reports_dir / EXCLUDED_MANIFEST).open(
        "w", encoding="utf-8", newline=""
    ) as fh:
        writer = csv.writer(fh)
        writer.writerow(["source", "source_path", "sha256", "reason"])
        for item in items:
            if item.disposition == "excluded":
                writer.writerow([item.source, item.rel_path, item.sha256, item.reason])


def _cleanup_temps(*roots: Path) -> None:
    for root in roots:
        if root.is_dir():
            for temp in root.rglob(".tmp-*"):
                temp.unlink(missing_ok=True)


def run_materialize(
    conn: sqlite3.Connection,
    cfg: Config,
    wt: WorkingTree,
    log: Logger,
    execute: bool = False,
) -> MaterializeSummary:
    """Plan (and with ``execute=True`` perform) the materialization.

    Usage:
        >>> summary = run_materialize(conn, cfg, wt, log)  # doctest: +SKIP
    """
    summary = MaterializeSummary(executed=execute)

    map_path = wt.reports_dir / KEYWORD_MAP
    if not map_path.exists():
        candidates: set[str] = set()
        for row in conn.execute(
            "SELECT rel_path FROM instance WHERE kind IN ('image', 'video')"
        ):
            candidates.update(
                folder_keyword_candidates(row["rel_path"], cfg.dates.folder_patterns)
            )
        for row in conn.execute("SELECT DISTINCT album FROM album_membership"):
            candidates.add(row["album"])
        write_keyword_map(map_path, candidates)
        summary.keyword_map_created = True
        if execute:
            raise MaterializeError(
                f"keyword map was missing; a default was written to {map_path}."
                " Review/edit it, then run --execute again."
            )
    keyword_map = load_keyword_map(map_path)

    items = build_plan(conn, cfg, keyword_map)
    source_roots = {
        row["source"]: Path(row["root"])
        for row in conn.execute("SELECT source, root FROM source_root")
    }
    done = {
        row["instance_id"]
        for row in conn.execute(
            "SELECT instance_id FROM placement WHERE verified_ok = 1"
        )
    }

    quarantine_shas_copied: set[str] = set()
    pending_bytes = 0
    for item in items:
        if item.disposition == "archive":
            summary.archived += 1
            if not item.resolved_date:
                summary.undated += 1
            if item.instance_id not in done:
                pending_bytes += item.size_bytes
        elif item.disposition == "quarantine":
            summary.quarantined += 1
            if item.sha256 not in quarantine_shas_copied:
                quarantine_shas_copied.add(item.sha256)
                summary.quarantine_copies += 1
                if item.instance_id not in done:
                    pending_bytes += item.size_bytes
        else:
            summary.excluded += 1
    summary.bytes_planned = pending_bytes

    dest_shas: dict[int, str] = {}
    if execute:
        require_space(wt.root, pending_bytes, cfg.space.margin_pct)
        _cleanup_temps(wt.archive_dir, wt.quarantine_dir)
        recheck_every = max(
            int(cfg.space.recheck_gb * 1e9), 1
        )
        written_since_check = 0
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        copied_quarantine: dict[str, str] = {}  # sha -> quarantine rel
        with ExifToolHelper(
            common_args=["-n"], config_file=str(exiftool_config_path())
        ) as et:
            for item in items:
                if item.instance_id in done:
                    summary.skipped_done += 1
                    continue
                if item.disposition == "excluded":
                    _record_placement(conn, item, None, now)
                    continue
                source_root = source_roots.get(item.source)
                if source_root is None:
                    raise MaterializeError(f"no recorded root for {item.source}")
                src = source_root / item.rel_path
                assert item.dest_rel is not None
                if item.disposition == "archive":
                    dest = wt.archive_dir / item.dest_rel
                    temp = _copy_verified(src, dest, item.sha256)
                    args = _metadata_args(item)
                    if item.use_sidecar:
                        os.replace(temp, dest)
                        _exiftool_sidecar(et, args, dest)
                        summary.sidecars_written += 1
                        dest_sha = item.sha256  # bytes untouched
                    else:
                        _exiftool_write(et, args, temp)
                        dest_sha = _sha256_file(temp)
                        os.replace(temp, dest)
                    dest_shas[item.instance_id] = dest_sha
                    _record_placement(conn, item, dest_sha, now)
                else:  # quarantine: byte-identical, once per hash
                    existing_rel = copied_quarantine.get(item.sha256)
                    if existing_rel is None:
                        dest = wt.quarantine_dir / item.dest_rel
                        if not dest.exists():
                            temp = _copy_verified(src, dest, item.sha256)
                            os.replace(temp, dest)
                        copied_quarantine[item.sha256] = item.dest_rel
                    else:
                        item.dest_rel = existing_rel
                    _record_placement(conn, item, item.sha256, now)
                written_since_check += item.size_bytes
                if (
                    written_since_check >= recheck_every
                    and summary.archived + summary.quarantined
                    >= _SPACE_RECHECK_MIN_ITEMS
                ):
                    require_space(wt.root, pending_bytes, cfg.space.margin_pct)
                    written_since_check = 0
        _write_quarantine_index(conn, wt, items)
        summary.sample_checked = _sample_verify(conn, wt, log)

    _write_manifests(wt, items, dest_shas)
    log.info(
        "materialize complete" if execute else "materialize dry-run complete",
        extra={
            "executed": execute,
            "archived": summary.archived,
            "quarantined": summary.quarantined,
            "quarantine_copies": summary.quarantine_copies,
            "excluded": summary.excluded,
            "undated": summary.undated,
            "sidecars": summary.sidecars_written,
            "skipped_done": summary.skipped_done,
            "bytes_planned": summary.bytes_planned,
        },
    )
    return summary


def _record_placement(
    conn: sqlite3.Connection, item: PlanItem, dest_sha: str | None, now: str
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO placement (instance_id, disposition, dest_rel_path,"
            " dest_sha256, copied_ok, verified_ok) VALUES (?, ?, ?, ?, 1, 1)"
            " ON CONFLICT(instance_id) DO UPDATE SET"
            " disposition = excluded.disposition,"
            " dest_rel_path = excluded.dest_rel_path,"
            " dest_sha256 = excluded.dest_sha256, copied_ok = 1, verified_ok = 1",
            (item.instance_id, item.disposition, item.dest_rel, dest_sha),
        )
        conn.execute(
            "INSERT INTO decision (ts, stage, subject, rule, detail, actor)"
            " VALUES (?, 'materialize', ?, ?, ?, 'auto')",
            (
                now,
                f"instance:{item.instance_id}",
                f"materialize.{item.disposition}",
                json.dumps(
                    {"reason": item.reason, "dest": item.dest_rel,
                     "sidecar": item.use_sidecar, "keywords": item.keywords},
                    sort_keys=True,
                ),
            ),
        )


def _write_quarantine_index(
    conn: sqlite3.Connection, wt: WorkingTree, items: list[PlanItem]
) -> None:
    by_sha: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.disposition != "quarantine":
            continue
        entry = by_sha.setdefault(
            item.sha256,
            {"sha256": item.sha256, "quarantine_path": item.dest_rel,
             "sources": [], "cluster_id": item.cluster_id,
             "winner_dest": item.winner_dest, "reason": item.reason},
        )
        entry["sources"].append(f"{item.source}:{item.rel_path}")
    with (wt.quarantine_dir / QUARANTINE_INDEX).open("w", encoding="utf-8") as fh:
        for sha in sorted(by_sha):
            entry = by_sha[sha]
            entry["sources"] = sorted(entry["sources"])
            fh.write(json.dumps(entry, sort_keys=True) + "\n")


def _sample_verify(conn: sqlite3.Connection, wt: WorkingTree, log: Logger) -> int:
    """Re-hash a random 1% sample (min 5) of materialized files (acceptance)."""
    rows = conn.execute(
        "SELECT instance_id, disposition, dest_rel_path, dest_sha256"
        " FROM placement WHERE disposition IN ('archive', 'quarantine')"
        " AND dest_rel_path IS NOT NULL"
    ).fetchall()
    if not rows:
        return 0
    rng = random.Random(0)
    count = max(5, int(len(rows) * _SAMPLE_FRACTION))
    sample = rng.sample(rows, min(count, len(rows)))
    for row in sample:
        base = wt.archive_dir if row["disposition"] == "archive" else wt.quarantine_dir
        path = base / row["dest_rel_path"]
        if not path.is_file() or _sha256_file(path) != row["dest_sha256"]:
            raise MaterializeError(
                f"post-execute verification failed for {row['dest_rel_path']}"
            )
    log.info("post-execute sample verified", extra={"sample_size": len(sample)})
    return len(sample)
