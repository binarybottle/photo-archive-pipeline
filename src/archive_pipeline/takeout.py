"""Stage 2 — Takeout normalization: attach JSON sidecars to media (spec §8).

Catalog-only; no file writes outside ``reports/``. Sidecar matching runs per
directory in this order: exact (including ``.supplemental-metadata`` names),
truncation (Google truncates long base names in the JSON filename), numbered
duplicates (``IMG(1).JPG`` vs ``IMG.JPG(1).json``, both orderings), and edited
pairs (``-edited`` files share the original's sidecar). Unmatched sidecars and
media are reported, never silently dropped.

Also recorded per media instance: album-folder memberships (topical keyword
candidates for Stage 6) and a ``google_recompressed`` heuristic flag used by
the Stage 5 winner score.

Re-running is idempotent: if the computed state equals the catalog's, nothing
is rewritten and no decisions are appended.

Usage:
    >>> from archive_pipeline.takeout import normalize_takeout
    >>> summary = normalize_takeout(conn, wt, log)  # doctest: +SKIP
    >>> summary.match_rate > 0.99  # doctest: +SKIP
    True
"""

from __future__ import annotations

import csv
import json
import posixpath
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path
from typing import Any

from archive_pipeline.workingtree import WorkingTree

SUPPLEMENTAL_SUFFIX = ".supplemental-metadata"
_MIN_SUPPLEMENTAL_PREFIX = 4  # shortest recognized truncation: ".sup"
EDITED_SUFFIXES = ("-edited",)
UNMATCHED_REPORT = "takeout_unmatched.csv"

_PHOTOS_FROM_RE = re.compile(r"^Photos from \d{4}$")
_ALBUM_META_RE = re.compile(r"^metadata(\(\d+\))?\.json$")
_TRAILING_NUM_RE = re.compile(r"^(?P<base>.+)\((?P<n>\d+)\)$")


class TakeoutError(Exception):
    """Raised when normalization cannot run (no sources, missing roots)."""


@dataclass(frozen=True)
class NameParts:
    """A sidecar filename reduced to its media base name and ``(n)`` number."""

    base: str
    number: int | None


def parse_sidecar_name(name: str) -> NameParts | None:
    """Reduce a sidecar JSON filename to (media base name, duplicate number).

    Returns None for non-sidecar names (album ``metadata.json`` descriptors).
    Handles the ``(n)`` suffix after ``.json``-stripping and the
    ``.supplemental-metadata`` suffix including Google's truncations of it.

    Usage:
        >>> parse_sidecar_name("IMG_1234.JPG.json")
        NameParts(base='IMG_1234.JPG', number=None)
        >>> parse_sidecar_name("IMG_1234.JPG.supplemental-metadata.json")
        NameParts(base='IMG_1234.JPG', number=None)
        >>> parse_sidecar_name("IMG_1234.JPG(1).json")
        NameParts(base='IMG_1234.JPG', number=1)
        >>> parse_sidecar_name("metadata.json") is None
        True
    """
    if not name.endswith(".json") or _ALBUM_META_RE.match(name):
        return None
    core = name[: -len(".json")]
    number: int | None = None
    numbered = _TRAILING_NUM_RE.match(core)
    if numbered:
        number = int(numbered["n"])
        core = numbered["base"]
    for length in range(len(SUPPLEMENTAL_SUFFIX), _MIN_SUPPLEMENTAL_PREFIX - 1, -1):
        if core.endswith(SUPPLEMENTAL_SUFFIX[:length]):
            core = core[:-length]
            break
    return NameParts(base=core, number=number)


@dataclass(frozen=True)
class SidecarMatch:
    """One sidecar's matching outcome within its directory."""

    json_name: str
    media_name: str | None
    method: str  # exact | truncation | numbered | unmatched
    reason: str | None = None  # no_media_match | ambiguous_truncation


def match_directory(json_names: list[str], media_names: list[str]) -> list[SidecarMatch]:
    """Match sidecar JSONs to media files within one directory (spec Stage 2).

    Usage:
        >>> [m.method for m in match_directory(["a.jpg.json"], ["a.jpg"])]
        ['exact']
    """
    media = set(media_names)
    matches: list[SidecarMatch] = []
    for json_name in sorted(json_names):
        parts = parse_sidecar_name(json_name)
        if parts is None:
            continue
        base, number = parts.base, parts.number
        if number is not None:
            stem, ext = posixpath.splitext(base)
            candidate = f"{stem}({number}){ext}"
            if candidate in media:
                matches.append(SidecarMatch(json_name, candidate, "numbered"))
            else:
                # Never fall back to the un-numbered base: it has its own sidecar.
                matches.append(
                    SidecarMatch(json_name, None, "unmatched", reason="no_media_match")
                )
            continue
        if base in media:
            matches.append(SidecarMatch(json_name, base, "exact"))
            continue
        prefixed = sorted(m for m in media if m.startswith(base))
        if len(prefixed) == 1:
            matches.append(SidecarMatch(json_name, prefixed[0], "truncation"))
        elif len(prefixed) > 1:
            matches.append(
                SidecarMatch(json_name, None, "unmatched", reason="ambiguous_truncation")
            )
        else:
            matches.append(SidecarMatch(json_name, None, "unmatched", reason="no_media_match"))
    return matches


def edited_original(media_name: str) -> str | None:
    """Name of the original for a Google ``-edited`` file, else None.

    Usage:
        >>> edited_original("IMG_1234-edited.JPG")
        'IMG_1234.JPG'
        >>> edited_original("IMG_1234.JPG") is None
        True
    """
    stem, ext = posixpath.splitext(media_name)
    for suffix in EDITED_SUFFIXES:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)] + ext
    return None


def is_google_recompressed(exif: dict[str, Any], mime: str | None) -> bool:
    """Heuristic for Google recompression / re-encoding (edge case 6).

    Requires a *positive* Google-processing signal: a JPEG with maker notes
    stripped and a Google ``Software`` tag (e.g. Google Photos edits, which are
    re-encoded). Mere absence of camera Make/Model is deliberately NOT used —
    old low-resolution phone photos, scans, screenshots, and app exports
    legitimately lack it without having been recompressed, so that fallback
    produced large numbers of false positives on Original-quality libraries.
    This is a scoring penalty and review signal, never a hard decision.

    Usage:
        >>> is_google_recompressed({"EXIF:Software": "Google Photos"}, "image/jpeg")
        True
        >>> is_google_recompressed({}, "image/jpeg")
        False
        >>> is_google_recompressed({"EXIF:Make": "Canon"}, "image/jpeg")
        False
    """
    if mime != "image/jpeg":
        return False
    if any(key.startswith("MakerNotes") for key in exif):
        return False
    return "google" in str(exif.get("EXIF:Software", "")).lower()


@dataclass(frozen=True)
class SidecarData:
    """Parsed content of one Takeout sidecar JSON."""

    title: str | None
    description: str | None
    photo_taken_time: str | None  # ISO, UTC
    creation_time: str | None  # ISO, UTC (upload time; R4 heuristic)
    gps_lat: float | None
    gps_lon: float | None


def _epoch_iso(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    try:
        ts = int(node["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def parse_sidecar_text(text: str) -> SidecarData | None:
    """Parse a sidecar JSON body; None if it is not valid JSON.

    Google writes ``geoData`` of exactly (0.0, 0.0) when there is no GPS; that
    is treated as absent.

    Usage:
        >>> data = parse_sidecar_text('{"photoTakenTime": {"timestamp": "1429349400"}}')
        >>> data.photo_taken_time
        '2015-04-18T09:30:00+00:00'
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    geo = payload.get("geoData") or {}
    lat = geo.get("latitude") if isinstance(geo, dict) else None
    lon = geo.get("longitude") if isinstance(geo, dict) else None
    if not isinstance(lat, int | float) or not isinstance(lon, int | float) or (
        lat == 0.0 and lon == 0.0
    ):
        lat = lon = None
    title = payload.get("title")
    description = payload.get("description")
    return SidecarData(
        title=title if isinstance(title, str) and title else None,
        description=description if isinstance(description, str) and description else None,
        photo_taken_time=_epoch_iso(payload.get("photoTakenTime")),
        creation_time=_epoch_iso(payload.get("creationTime")),
        gps_lat=float(lat) if lat is not None else None,
        gps_lon=float(lon) if lon is not None else None,
    )


@dataclass
class NormalizeSummary:
    """Aggregate outcome across all normalized TAKEOUT sources."""

    sources: list[str] = field(default_factory=list)
    media_total: int = 0
    sidecars_total: int = 0
    matched_by_method: dict[str, int] = field(default_factory=dict)
    unmatched_sidecars: int = 0
    unmatched_media: int = 0
    edited_pairs: int = 0
    album_memberships: int = 0
    albums: int = 0
    recompressed: int = 0
    changed: bool = False

    @property
    def matched_media(self) -> int:
        return self.media_total - self.unmatched_media

    @property
    def match_rate(self) -> float:
        return self.matched_media / self.media_total if self.media_total else 1.0


def _album_name_for_dir(
    dir_path: str, descriptor_text: str | None
) -> str | None:
    """Album name if this directory is a Takeout album folder, else None.

    A directory is an album when it has a ``metadata.json`` descriptor, or when
    its basename is not a "Photos from YYYY" dated folder (heuristic).
    """
    basename = posixpath.basename(dir_path)
    if descriptor_text is not None:
        parsed = parse_sidecar_text(descriptor_text)
        if parsed and parsed.title:
            return parsed.title
        return basename or None
    if not basename or _PHOTOS_FROM_RE.match(basename) or basename == "Google Photos":
        return None
    return basename


def _existing_state(
    conn: sqlite3.Connection, sources: list[str]
) -> tuple[set[tuple[Any, ...]], set[tuple[Any, ...]], set[tuple[int, int]], set[int]]:
    """Current catalog state for the target sources, as comparable sets."""
    marks = ",".join("?" for _ in sources)
    in_sources = f"(SELECT id FROM instance WHERE source IN ({marks}))"
    sidecars = {
        tuple(row)
        for row in conn.execute(
            "SELECT instance_id, media_instance_id, photo_taken_time, creation_time,"
            " gps_lat, gps_lon, description, title, match_method"
            f" FROM takeout_sidecar WHERE instance_id IN {in_sources}",
            sources,
        )
    }
    albums = {
        tuple(row)
        for row in conn.execute(
            "SELECT media_instance_id, album, album_dir FROM album_membership"
            f" WHERE media_instance_id IN {in_sources}",
            sources,
        )
    }
    edited = {
        (row["edited_instance_id"], row["original_instance_id"])
        for row in conn.execute(
            "SELECT edited_instance_id, original_instance_id FROM edited_pair"
            f" WHERE edited_instance_id IN {in_sources}",
            sources,
        )
    }
    recompressed = {
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM instance WHERE google_recompressed = 1 AND id IN {in_sources}",
            sources,
        )
    }
    return sidecars, albums, edited, recompressed


def normalize_takeout(
    conn: sqlite3.Connection,
    wt: WorkingTree,
    log: Logger,
    sources: list[str] | None = None,
) -> NormalizeSummary:
    """Normalize all (or the given) TAKEOUT sources; return a summary.

    Usage:
        >>> summary = normalize_takeout(conn, wt, log)  # doctest: +SKIP
    """
    cataloged = [
        row["source"]
        for row in conn.execute(
            "SELECT DISTINCT source FROM instance WHERE source LIKE 'TAKEOUT:%'"
            " ORDER BY source"
        )
    ]
    targets = sources if sources is not None else cataloged
    unknown = sorted(set(targets) - set(cataloged))
    if unknown:
        raise TakeoutError(f"source(s) not in catalog: {', '.join(unknown)}")
    if not targets:
        raise TakeoutError("no TAKEOUT sources in the catalog; run `archive ingest` first")

    summary = NormalizeSummary(sources=list(targets))
    sidecar_rows: list[dict[str, Any]] = []
    album_rows: list[tuple[int, str, str]] = []
    edited_rows: list[tuple[int, int]] = []
    recompressed_ids: set[int] = set()
    report_rows: list[tuple[str, str, str, str]] = []  # source, rel_path, kind, reason
    decisions: list[tuple[str, str, str]] = []  # subject, rule, detail-json

    for source in targets:
        root_row = conn.execute(
            "SELECT root FROM source_root WHERE source = ?", (source,)
        ).fetchone()
        if root_row is None:
            raise TakeoutError(f"no recorded root for {source}; re-run `archive ingest`")
        root = Path(root_row["root"])

        media_by_rel: dict[str, sqlite3.Row] = {}
        json_by_rel: dict[str, sqlite3.Row] = {}
        for row in conn.execute(
            "SELECT id, rel_path, kind, mime, exif_json FROM instance WHERE source = ?",
            (source,),
        ):
            if row["kind"] in ("image", "video"):
                media_by_rel[row["rel_path"]] = row
            elif row["kind"] == "sidecar_json":
                json_by_rel[row["rel_path"]] = row

        by_dir: dict[str, tuple[list[str], list[str]]] = defaultdict(lambda: ([], []))
        for rel in media_by_rel:
            by_dir[posixpath.dirname(rel)][0].append(posixpath.basename(rel))
        for rel in json_by_rel:
            by_dir[posixpath.dirname(rel)][1].append(posixpath.basename(rel))

        matched_media_rels: set[str] = set()
        for dir_path, (media_names, json_names) in sorted(by_dir.items()):
            descriptor = next(
                (n for n in sorted(json_names) if _ALBUM_META_RE.match(n)), None
            )
            descriptor_text: str | None = None
            if descriptor is not None:
                descriptor_text = (root / posixpath.join(dir_path, descriptor)).read_text(
                    encoding="utf-8", errors="replace"
                )
            album = _album_name_for_dir(dir_path, descriptor_text)
            if album is not None:
                for name in sorted(media_names):
                    media_row = media_by_rel[posixpath.join(dir_path, name)]
                    album_rows.append((media_row["id"], album, dir_path))

            for match in match_directory(json_names, media_names):
                json_rel = posixpath.join(dir_path, match.json_name)
                json_row = json_by_rel[json_rel]
                data = parse_sidecar_text(
                    (root / json_rel).read_text(encoding="utf-8", errors="replace")
                )
                media_id = None
                if match.media_name is not None:
                    media_rel = posixpath.join(dir_path, match.media_name)
                    media_id = media_by_rel[media_rel]["id"]
                    matched_media_rels.add(media_rel)
                    summary.matched_by_method[match.method] = (
                        summary.matched_by_method.get(match.method, 0) + 1
                    )
                else:
                    summary.unmatched_sidecars += 1
                    report_rows.append((source, json_rel, "sidecar", match.reason or ""))
                if data is None and match.media_name is not None:
                    report_rows.append((source, json_rel, "sidecar", "parse_error"))
                sidecar_rows.append(
                    {
                        "instance_id": json_row["id"],
                        "media_instance_id": media_id,
                        "photo_taken_time": data.photo_taken_time if data else None,
                        "creation_time": data.creation_time if data else None,
                        "gps_lat": data.gps_lat if data else None,
                        "gps_lon": data.gps_lon if data else None,
                        "description": data.description if data else None,
                        "title": data.title if data else None,
                        "match_method": match.method,
                    }
                )
                decisions.append(
                    (
                        f"instance:{json_row['id']}",
                        f"sidecar.{match.method}",
                        json.dumps(
                            {"json": json_rel, "media": match.media_name,
                             "reason": match.reason},
                            sort_keys=True,
                        ),
                    )
                )

            for name in sorted(media_names):
                original = edited_original(name)
                if original is not None and original in media_names:
                    edited_rel = posixpath.join(dir_path, name)
                    original_rel = posixpath.join(dir_path, original)
                    edited_rows.append(
                        (media_by_rel[edited_rel]["id"], media_by_rel[original_rel]["id"])
                    )
                    if original_rel in matched_media_rels:
                        matched_media_rels.add(edited_rel)
                    decisions.append(
                        (
                            f"instance:{media_by_rel[edited_rel]['id']}",
                            "edited.link",
                            json.dumps({"edited": edited_rel, "original": original_rel}),
                        )
                    )

        for rel, row in sorted(media_by_rel.items()):
            exif = json.loads(row["exif_json"]) if row["exif_json"] else {}
            if is_google_recompressed(exif, row["mime"]):
                recompressed_ids.add(row["id"])
            if rel not in matched_media_rels:
                summary.unmatched_media += 1
                report_rows.append((source, rel, "media", "no_sidecar"))

        summary.media_total += len(media_by_rel)
        summary.sidecars_total += sum(
            1 for rel in json_by_rel if parse_sidecar_name(posixpath.basename(rel))
        )

    summary.edited_pairs = len(edited_rows)
    summary.album_memberships = len(album_rows)
    summary.albums = len({album for _, album, _ in album_rows})
    summary.recompressed = len(recompressed_ids)

    new_sidecars = {
        (r["instance_id"], r["media_instance_id"], r["photo_taken_time"],
         r["creation_time"], r["gps_lat"], r["gps_lon"], r["description"],
         r["title"], r["match_method"])
        for r in sidecar_rows
    }
    existing = _existing_state(conn, list(targets))
    summary.changed = existing != (
        new_sidecars, set(album_rows), set(edited_rows), recompressed_ids
    )
    if summary.changed:
        marks = ",".join("?" for _ in targets)
        in_sources = f"(SELECT id FROM instance WHERE source IN ({marks}))"
        with conn:
            conn.execute(
                f"DELETE FROM takeout_sidecar WHERE instance_id IN {in_sources}", targets
            )
            conn.execute(
                f"DELETE FROM album_membership WHERE media_instance_id IN {in_sources}",
                targets,
            )
            conn.execute(
                f"DELETE FROM edited_pair WHERE edited_instance_id IN {in_sources}", targets
            )
            conn.execute(
                f"UPDATE instance SET google_recompressed = 0 WHERE id IN {in_sources}",
                targets,
            )
            conn.executemany(
                "INSERT INTO takeout_sidecar (instance_id, media_instance_id,"
                " photo_taken_time, creation_time, gps_lat, gps_lon, description,"
                " title, match_method) VALUES (:instance_id, :media_instance_id,"
                " :photo_taken_time, :creation_time, :gps_lat, :gps_lon,"
                " :description, :title, :match_method)",
                sidecar_rows,
            )
            conn.executemany(
                "INSERT INTO album_membership (media_instance_id, album, album_dir)"
                " VALUES (?, ?, ?)",
                album_rows,
            )
            conn.executemany(
                "INSERT INTO edited_pair (edited_instance_id, original_instance_id)"
                " VALUES (?, ?)",
                edited_rows,
            )
            conn.executemany(
                "UPDATE instance SET google_recompressed = 1 WHERE id = ?",
                [(i,) for i in sorted(recompressed_ids)],
            )
            now = datetime.now(tz=UTC).isoformat(timespec="seconds")
            conn.executemany(
                "INSERT INTO decision (ts, stage, subject, rule, detail, actor)"
                " VALUES (?, 'takeout-normalize', ?, ?, ?, 'auto')",
                [(now, subject, rule, detail) for subject, rule, detail in decisions],
            )

    report_path = wt.reports_dir / UNMATCHED_REPORT
    with report_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["source", "rel_path", "kind", "reason"])
        writer.writerows(sorted(report_rows))

    if summary.media_total and summary.match_rate < 0.99:
        log.warning(
            "sidecar match rate below 99% acceptance threshold",
            extra={"match_rate": round(summary.match_rate, 4),
                   "unmatched_media": summary.unmatched_media},
        )
    log.info(
        "takeout normalization complete",
        extra={
            "sources": summary.sources,
            "media_total": summary.media_total,
            "matched_by_method": summary.matched_by_method,
            "unmatched_sidecars": summary.unmatched_sidecars,
            "unmatched_media": summary.unmatched_media,
            "edited_pairs": summary.edited_pairs,
            "albums": summary.albums,
            "recompressed": summary.recompressed,
            "changed": summary.changed,
        },
    )
    return summary
