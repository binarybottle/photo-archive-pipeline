"""Stage 2b — Takeout-derived subtree detection within LOCAL (spec §8).

LOCAL contains prior Takeout extractions, so presence in LOCAL does not imply
human curation. Each LOCAL directory gets a derivation signal from: Google JSON
sidecars matching its media, Takeout folder-name signatures ("Photos from
YYYY", "Google Photos", "Takeout"), ``metadata.json`` album descriptors, and
Google-recompression flags. Directories at or above ``provenance.threshold``
are ``takeout_derived``: their instances get TAKEOUT-level trust, their
sidecars are parsed exactly as in Stage 2, and their media get the
``google_recompressed`` flag where the heuristic fires.

The classification is exported to ``reports/local_provenance.csv`` for the user
to spot-check; config path-prefix overrides (``provenance.curated_overrides`` /
``takeout_derived_overrides``) are honored and marked in the report.

Usage:
    >>> from archive_pipeline.provenance import classify_local
    >>> summary = classify_local(conn, cfg, wt, log)  # doctest: +SKIP
    >>> summary.dirs_total > 0  # doctest: +SKIP
    True
"""

from __future__ import annotations

import csv
import json
import posixpath
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path
from typing import Any

from archive_pipeline.config import Config
from archive_pipeline.takeout import (
    is_google_recompressed,
    match_directory,
    parse_sidecar_name,
    parse_sidecar_text,
)
from archive_pipeline.workingtree import WorkingTree

PROVENANCE_REPORT = "local_provenance.csv"

_TAKEOUT_NAME_RE = re.compile(r"^(Photos from \d{4}|Google Photos|Takeout)$")
_ALBUM_META_RE = re.compile(r"^metadata(\(\d+\))?\.json$")

#: Signal weights; any single strong signature crosses the default 0.5 threshold.
_W_SIDECARS = 0.6
_W_NAME = 0.5
_W_DESCRIPTOR = 0.3
_W_RECOMPRESSED = 0.2


class ProvenanceError(Exception):
    """Raised when classification cannot run (missing roots, bad overrides)."""


@dataclass(frozen=True)
class DirSignal:
    """One directory's derivation-signal components and outcome."""

    dir_path: str
    media_count: int
    sidecar_count: int
    matched_ratio: float
    name_signature: bool
    descriptor: bool
    recompressed_ratio: float
    signal: float
    classification: str  # curated | takeout_derived
    override: str | None


def _prefix_match(dir_path: str, prefixes: tuple[str, ...]) -> bool:
    return any(dir_path == p or dir_path.startswith(p + "/") for p in prefixes)


def compute_signal(
    matched_ratio: float, name_signature: bool, descriptor: bool, recompressed_ratio: float
) -> float:
    """Combine signal components into a 0..1 derivation score.

    Usage:
        >>> compute_signal(1.0, False, False, 0.0)
        0.6
        >>> compute_signal(0.0, True, False, 0.0)
        0.5
    """
    raw = (
        _W_SIDECARS * matched_ratio
        + _W_NAME * (1.0 if name_signature else 0.0)
        + _W_DESCRIPTOR * (1.0 if descriptor else 0.0)
        + _W_RECOMPRESSED * recompressed_ratio
    )
    return round(min(1.0, raw), 4)


@dataclass
class ProvenanceSummary:
    """Aggregate outcome of one classification run."""

    sources: list[str]
    dirs_total: int = 0
    derived: int = 0
    curated: int = 0
    overridden: int = 0
    sidecars_linked: int = 0
    recompressed_flagged: int = 0
    changed: bool = False


def _classify_dir(
    cfg: Config,
    dir_path: str,
    media_names: list[str],
    json_names: list[str],
    recompressed_count: int,
) -> DirSignal:
    """Compute the signal and classification for one directory."""
    google_jsons = [n for n in json_names if parse_sidecar_name(n)]
    descriptor = any(_ALBUM_META_RE.match(n) for n in json_names)
    matched = sum(
        1 for m in match_directory(google_jsons, media_names) if m.media_name is not None
    )
    if media_names:
        matched_ratio = matched / len(media_names)
        recompressed_ratio = recompressed_count / len(media_names)
    else:
        matched_ratio = 1.0 if google_jsons else 0.0
        recompressed_ratio = 0.0
    name_signature = any(_TAKEOUT_NAME_RE.match(c) for c in dir_path.split("/") if c)
    signal = compute_signal(matched_ratio, name_signature, descriptor, recompressed_ratio)

    if _prefix_match(dir_path, cfg.provenance.curated_overrides):
        classification, override = "curated", "config:curated"
    elif _prefix_match(dir_path, cfg.provenance.takeout_derived_overrides):
        classification, override = "takeout_derived", "config:takeout_derived"
    else:
        classification = (
            "takeout_derived" if signal >= cfg.provenance.threshold else "curated"
        )
        override = None
    return DirSignal(
        dir_path=dir_path,
        media_count=len(media_names),
        sidecar_count=len(google_jsons),
        matched_ratio=round(matched_ratio, 4),
        name_signature=name_signature,
        descriptor=descriptor,
        recompressed_ratio=round(recompressed_ratio, 4),
        signal=signal,
        classification=classification,
        override=override,
    )


def classify_local(
    conn: sqlite3.Connection, cfg: Config, wt: WorkingTree, log: Logger
) -> ProvenanceSummary:
    """Classify every directory of every non-TAKEOUT source; return a summary.

    Also sets ``instance.effective_trust`` for all sources (TAKEOUT instances
    are always ``takeout``), parses sidecars inside derived directories, and
    flags recompressed media there. Idempotent: identical state writes nothing.

    Usage:
        >>> summary = classify_local(conn, cfg, wt, log)  # doctest: +SKIP
    """
    overlap = set(cfg.provenance.curated_overrides) & set(
        cfg.provenance.takeout_derived_overrides
    )
    if overlap:
        raise ProvenanceError(
            f"path prefix(es) in both override lists: {', '.join(sorted(overlap))}"
        )
    local_sources = [
        row["source"]
        for row in conn.execute(
            "SELECT DISTINCT source FROM instance WHERE source NOT LIKE 'TAKEOUT:%'"
            " ORDER BY source"
        )
    ]
    summary = ProvenanceSummary(sources=local_sources)
    provenance_rows: list[tuple[Any, ...]] = []
    trust_by_id: dict[int, str] = {}
    sidecar_rows: list[dict[str, Any]] = []
    recompressed_ids: set[int] = set()
    report_lines: list[list[Any]] = []
    decisions: list[tuple[str, str, str]] = []

    for row in conn.execute("SELECT id FROM instance WHERE source LIKE 'TAKEOUT:%'"):
        trust_by_id[row["id"]] = "takeout"

    for source in local_sources:
        root_row = conn.execute(
            "SELECT root FROM source_root WHERE source = ?", (source,)
        ).fetchone()
        if root_row is None:
            raise ProvenanceError(f"no recorded root for {source}; re-run `archive ingest`")
        root = Path(root_row["root"])

        rows_by_rel: dict[str, sqlite3.Row] = {
            row["rel_path"]: row
            for row in conn.execute(
                "SELECT id, rel_path, kind, mime, exif_json FROM instance"
                " WHERE source = ?",
                (source,),
            )
        }
        by_dir: dict[str, list[str]] = defaultdict(list)
        for rel in rows_by_rel:
            by_dir[posixpath.dirname(rel)].append(posixpath.basename(rel))

        for dir_path, names in sorted(by_dir.items()):
            rel_of = {name: posixpath.join(dir_path, name) for name in names}

            def _rel(name: str, _rel_of: dict[str, str] = rel_of) -> str:
                return _rel_of[name]

            media_names = sorted(
                n for n in names if rows_by_rel[_rel(n)]["kind"] in ("image", "video")
            )
            json_names = sorted(
                n for n in names if rows_by_rel[_rel(n)]["kind"] == "sidecar_json"
            )
            recompressed_local = [
                n
                for n in media_names
                if is_google_recompressed(
                    json.loads(rows_by_rel[_rel(n)]["exif_json"] or "{}"),
                    rows_by_rel[_rel(n)]["mime"],
                )
            ]
            sig = _classify_dir(cfg, dir_path, media_names, json_names, len(recompressed_local))
            derived = sig.classification == "takeout_derived"
            summary.dirs_total += 1
            summary.derived += int(derived)
            summary.curated += int(not derived)
            summary.overridden += int(sig.override is not None)

            for name in names:
                trust_by_id[rows_by_rel[_rel(name)]["id"]] = (
                    "takeout" if derived else "curated"
                )
            if derived:
                for name in recompressed_local:
                    recompressed_ids.add(rows_by_rel[_rel(name)]["id"])
                google_jsons = [n for n in json_names if parse_sidecar_name(n)]
                for match in match_directory(google_jsons, media_names):
                    json_row = rows_by_rel[_rel(match.json_name)]
                    data = parse_sidecar_text(
                        (root / _rel(match.json_name)).read_text(
                            encoding="utf-8", errors="replace"
                        )
                    )
                    media_id = (
                        rows_by_rel[_rel(match.media_name)]["id"]
                        if match.media_name is not None
                        else None
                    )
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

            provenance_rows.append(
                (
                    source,
                    sig.dir_path,
                    sig.media_count,
                    sig.sidecar_count,
                    sig.signal,
                    json.dumps(
                        {
                            "matched_ratio": sig.matched_ratio,
                            "name_signature": sig.name_signature,
                            "descriptor": sig.descriptor,
                            "recompressed_ratio": sig.recompressed_ratio,
                        },
                        sort_keys=True,
                    ),
                    sig.classification,
                    sig.override,
                )
            )
            report_lines.append(
                [source, sig.dir_path, sig.media_count, sig.sidecar_count,
                 sig.signal, sig.classification, sig.override or ""]
            )
            decisions.append(
                (
                    f"dir:{source}:{sig.dir_path}",
                    f"provenance.{sig.classification}"
                    + (".override" if sig.override else ""),
                    json.dumps({"signal": sig.signal, "override": sig.override}),
                )
            )

    summary.sidecars_linked = sum(
        1 for r in sidecar_rows if r["media_instance_id"] is not None
    )
    summary.recompressed_flagged = len(recompressed_ids)

    existing_prov = {
        tuple(row)
        for row in conn.execute(
            "SELECT source, dir_path, media_count, sidecar_count, signal,"
            " signals_json, classification, override FROM local_provenance"
        )
    }
    existing_trust = {
        row["id"]: row["effective_trust"]
        for row in conn.execute(
            "SELECT id, effective_trust FROM instance WHERE effective_trust IS NOT NULL"
        )
    }
    marks = ",".join("?" for _ in local_sources) or "''"
    existing_sidecars = {
        tuple(row)
        for row in conn.execute(
            "SELECT instance_id, media_instance_id, photo_taken_time, creation_time,"
            " gps_lat, gps_lon, description, title, match_method FROM takeout_sidecar"
            f" WHERE instance_id IN (SELECT id FROM instance WHERE source IN ({marks}))",
            local_sources,
        )
    }
    existing_recompressed = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM instance WHERE google_recompressed = 1 AND source IN"
            f" ({marks})",
            local_sources,
        )
    }
    new_sidecars = {
        (r["instance_id"], r["media_instance_id"], r["photo_taken_time"],
         r["creation_time"], r["gps_lat"], r["gps_lon"], r["description"],
         r["title"], r["match_method"])
        for r in sidecar_rows
    }
    summary.changed = (
        existing_prov != set(provenance_rows)
        or existing_trust != trust_by_id
        or existing_sidecars != new_sidecars
        or existing_recompressed != recompressed_ids
    )
    if summary.changed:
        with conn:
            conn.execute("DELETE FROM local_provenance")
            conn.executemany(
                "INSERT INTO local_provenance (source, dir_path, media_count,"
                " sidecar_count, signal, signals_json, classification, override)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                provenance_rows,
            )
            conn.executemany(
                "UPDATE instance SET effective_trust = ? WHERE id = ?",
                [(trust, iid) for iid, trust in trust_by_id.items()],
            )
            conn.execute(
                "DELETE FROM takeout_sidecar WHERE instance_id IN"
                f" (SELECT id FROM instance WHERE source IN ({marks}))",
                local_sources,
            )
            conn.executemany(
                "INSERT INTO takeout_sidecar (instance_id, media_instance_id,"
                " photo_taken_time, creation_time, gps_lat, gps_lon, description,"
                " title, match_method) VALUES (:instance_id, :media_instance_id,"
                " :photo_taken_time, :creation_time, :gps_lat, :gps_lon,"
                " :description, :title, :match_method)",
                sidecar_rows,
            )
            conn.execute(
                f"UPDATE instance SET google_recompressed = 0 WHERE source IN ({marks})",
                local_sources,
            )
            conn.executemany(
                "UPDATE instance SET google_recompressed = 1 WHERE id = ?",
                [(i,) for i in sorted(recompressed_ids)],
            )
            now = datetime.now(tz=UTC).isoformat(timespec="seconds")
            conn.executemany(
                "INSERT INTO decision (ts, stage, subject, rule, detail, actor)"
                " VALUES (?, 'local-provenance', ?, ?, ?, 'auto')",
                [(now, subject, rule, detail) for subject, rule, detail in decisions],
            )

    report_path = wt.reports_dir / PROVENANCE_REPORT
    with report_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["source", "dir_path", "media", "sidecars", "signal", "classification",
             "override"]
        )
        writer.writerows(report_lines)

    log.info(
        "local provenance classified",
        extra={
            "dirs_total": summary.dirs_total,
            "derived": summary.derived,
            "curated": summary.curated,
            "overridden": summary.overridden,
            "sidecars_linked": summary.sidecars_linked,
            "recompressed_flagged": summary.recompressed_flagged,
            "changed": summary.changed,
        },
    )
    return summary
