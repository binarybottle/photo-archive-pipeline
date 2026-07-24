"""Stage 5 — Deduplication: clustering, winner scoring, metadata merge (spec §8).

Runs after date resolution. Pass A groups exact duplicates by SHA-256. Pass B
pairs companions (RAW+JPEG, Live/motion photo image+video) before near-dup
clustering so pairs are never falsely merged; ``-edited`` versions stay linked
via ``edited_pair`` and are excluded from near-dup edges. Pass C clusters
near-duplicate images with a banded index over pHash (Hamming <= T1 confirmed
by dHash <= T2 and 1% aspect agreement; distances in (T1, T1+band] queue for
review). Pass D matches videos conservatively; near-video clusters always
queue for review and no video is ever auto-discarded.

The winner score is the spec formula, logged with its full component
breakdown. Guardrails queue for review: top-two scores within the margin
(except byte-identical exact clusters, where winner choice is path-context
only), a takeout-trust winner while an equal-resolution curated instance
exists, and aspect-ratio mismatches (crops are different artistic objects).

Reviewed clusters are locked: their members are excluded from re-clustering
and their rows are never rewritten (INV-6). Identical re-runs write nothing.

Usage:
    >>> from archive_pipeline.dedup import run_dedup
    >>> summary = run_dedup(conn, cfg, wt, log)  # doctest: +SKIP
    >>> summary.by_kind["exact"] > 0  # doctest: +SKIP
    True
"""

from __future__ import annotations

import csv
import json
import math
import posixpath
import re
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging import Logger
from typing import Any

from archive_pipeline.config import Config
from archive_pipeline.workingtree import WorkingTree

CLUSTER_AUDIT_REPORT = "cluster_audit_sample.csv"

RAW_EXTENSIONS = frozenset(
    {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf", ".rw2"}
)
_ASPECT_TOLERANCE = 0.01  # 1% (spec Pass C / crop guardrail)
_COMPANION_TIME_WINDOW_S = 1.0
_SCAFFOLD_DIRS = frozenset({"Takeout", "Google Photos"})
_PHOTOS_FROM_RE = re.compile(r"^Photos from \d{4}$")

_DATE_SOURCE_PRIORITY = {
    "review": 5, "folder": 4, "exif": 3, "takeout_json": 2, "filename": 1,
}
_PRECISION_CHARS = {"second": 19, "day": 10, "month": 7, "year": 4}


class DedupError(Exception):
    """Raised when dedup cannot run (date resolution incomplete...)."""


# --- Small primitives ----------------------------------------------------------


def hamming(a: str | None, b: str | None) -> int | None:
    """Hamming distance between two hex-encoded perceptual hashes.

    Usage:
        >>> hamming("00", "03")
        2
        >>> hamming("00", None) is None
        True
    """
    if not a or not b or len(a) != len(b):
        return None
    return bin(int(a, 16) ^ int(b, 16)).count("1")


class UnionFind:
    """Union-find over instance ids.

    Usage:
        >>> uf = UnionFind()
        >>> uf.union(1, 2); uf.union(2, 3)
        >>> uf.find(1) == uf.find(3)
        True
    """

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        parent = self._parent.setdefault(x, x)
        if parent != x:
            self._parent[x] = self.find(parent)
        return self._parent[x]

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def components(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for x in self._parent:
            out[self.find(x)].append(x)
        return {root: sorted(members) for root, members in out.items()}


def aspect_close(
    w1: int | None, h1: int | None, w2: int | None, h2: int | None
) -> bool:
    """True when both aspect ratios are known and agree within 1%.

    Usage:
        >>> aspect_close(1920, 1080, 1280, 720)
        True
        >>> aspect_close(1920, 1080, 1080, 1080)
        False
    """
    if not w1 or not h1 or not w2 or not h2:
        return False
    r1, r2 = w1 / h1, w2 / h2
    return abs(r1 - r2) / max(r1, r2) <= _ASPECT_TOLERANCE


# --- Winner scoring (spec formula, pinned by table-driven tests) ---------------


def format_rank(rel_path: str, google_recompressed: bool) -> float:
    """RAW/original (2.0) > HEIC/JPEG/PNG (1.0) > Google-recompressed (0.0).

    Usage:
        >>> format_rank("a/x.dng", False)
        2.0
        >>> format_rank("a/x.jpg", True)
        0.0
    """
    if posixpath.splitext(rel_path)[1].lower() in RAW_EXTENSIONS:
        return 2.0
    return 0.0 if google_recompressed else 1.0


def score_instance(
    *,
    rel_path: str,
    width: int | None,
    height: int | None,
    size_bytes: int,
    exif_tag_count: int | None,
    has_trusted_dto: bool,
    curated_trust: bool,
    google_recompressed: bool,
) -> tuple[float, dict[str, float]]:
    """The spec winner score with its full component breakdown.

    Usage:
        >>> score, parts = score_instance(rel_path="a.jpg", width=4000, height=3000,
        ...     size_bytes=5_000_000, exif_tag_count=60, has_trusted_dto=True,
        ...     curated_trust=True, google_recompressed=False)
        >>> round(parts["resolution"], 2)
        10.75
    """
    megapixels = (width or 0) * (height or 0) / 1e6
    parts = {
        "resolution": 3.0 * math.log2(max(megapixels, 0.01)),
        "format": 1.5 * format_rank(rel_path, google_recompressed),
        "metadata_richness": 1.0 * min(exif_tag_count or 0, 60) / 60,
        "trusted_dto": 1.0 if has_trusted_dto else 0.0,
        "source_trust": 0.75 if curated_trust else 0.0,
        "size_tiebreak": 0.5 * math.log10(max(size_bytes, 1)),
        "recompression_penalty": -2.0 if google_recompressed else 0.0,
    }
    return round(sum(parts.values()), 4), {k: round(v, 4) for k, v in parts.items()}


# --- Media rows and companion pairing ------------------------------------------


@dataclass(frozen=True)
class Media:
    """The per-instance facts dedup needs (built from catalog rows)."""

    id: int
    source: str
    rel_path: str
    kind: str  # image | video
    sha256: str
    size_bytes: int
    width: int | None
    height: int | None
    phash: str | None
    dhash: str | None
    video_sig: str | None
    exif_tag_count: int | None
    exif_dto: str | None
    camera_model: str | None
    gps_lat: float | None
    gps_lon: float | None
    content_identifier: str | None
    effective_trust: str  # curated | takeout
    archived: bool  # already materialized to the archive (placement ledger)
    google_recompressed: bool
    trusted_dto: bool  # camera DTO present with no distrust flags
    resolved_date: str | None
    resolved_precision: str | None
    resolved_source: str | None

    @property
    def dirname(self) -> str:
        return posixpath.dirname(self.rel_path)

    @property
    def basename_stem(self) -> str:
        return posixpath.splitext(posixpath.basename(self.rel_path))[0].lower()

    @property
    def is_raw(self) -> bool:
        return posixpath.splitext(self.rel_path)[1].lower() in RAW_EXTENSIONS

    def score(self) -> tuple[float, dict[str, float]]:
        return score_instance(
            rel_path=self.rel_path,
            width=self.width,
            height=self.height,
            size_bytes=self.size_bytes,
            exif_tag_count=self.exif_tag_count,
            has_trusted_dto=self.trusted_dto,
            curated_trust=self.effective_trust == "curated",
            google_recompressed=self.google_recompressed,
        )


def _dto_seconds(dto: str | None) -> float | None:
    if not dto:
        return None
    try:
        return datetime.fromisoformat(dto).timestamp()
    except ValueError:
        return None


def pair_raw_jpeg(media: list[Media]) -> list[tuple[Media, Media]]:
    """RAW+JPEG pairs: same directory and (same stem, or same camera+time <=1s).

    Returns (raw, jpeg) pairs; each instance appears in at most one pair.

    Usage:
        >>> pair_raw_jpeg([])  # doctest: +ELLIPSIS
        []
    """
    pairs: list[tuple[Media, Media]] = []
    used: set[int] = set()
    by_dir: dict[str, list[Media]] = defaultdict(list)
    for m in media:
        if m.kind == "image":
            by_dir[m.dirname].append(m)
    for members in by_dir.values():
        raws = [m for m in members if m.is_raw]
        jpegs = [m for m in members if not m.is_raw]
        for raw in raws:
            if raw.id in used:
                continue
            match = next(
                (j for j in jpegs if j.id not in used
                 and j.basename_stem == raw.basename_stem),
                None,
            )
            if match is None:
                raw_t = _dto_seconds(raw.exif_dto)
                if raw_t is not None and raw.camera_model:
                    match = next(
                        (
                            j for j in jpegs
                            if j.id not in used
                            and j.camera_model == raw.camera_model
                            and (t := _dto_seconds(j.exif_dto)) is not None
                            and abs(t - raw_t) <= _COMPANION_TIME_WINDOW_S
                        ),
                        None,
                    )
            if match is not None:
                pairs.append((raw, match))
                used.update((raw.id, match.id))
    return pairs


def pair_live(media: list[Media]) -> list[tuple[Media, Media]]:
    """Live-photo pairs: image + video sharing an Apple ContentIdentifier.

    Google motion photos embed their video inside the JPEG (single file), so
    only paired-file Live Photos need pairing here.

    Usage:
        >>> pair_live([])
        []
    """
    by_cid: dict[str, dict[str, Media]] = defaultdict(dict)
    pairs: list[tuple[Media, Media]] = []
    for m in media:
        if m.content_identifier:
            slot = by_cid[m.content_identifier]
            if m.kind not in slot:
                slot[m.kind] = m
    for slot in by_cid.values():
        if "image" in slot and "video" in slot:
            pairs.append((slot["image"], slot["video"]))
    return pairs


def content_identifier(exif: dict[str, Any]) -> str | None:
    """Apple ContentIdentifier from any tag group, else None.

    Usage:
        >>> content_identifier({"MakerNotes:ContentIdentifier": "AB-12"})
        'AB-12'
    """
    for key, value in exif.items():
        if key.split(":")[-1] == "ContentIdentifier" and isinstance(value, str):
            return value
    return None


def parse_video_sig(sig: str | None) -> tuple[float, int, int, list[str]] | None:
    """Parse ``"<dur>s:<w>x<h>:<hash>:<hash>:<hash>"`` -> components.

    Usage:
        >>> parse_video_sig("12s:1920x1080:aa:bb:cc")
        (12.0, 1920, 1080, ['aa', 'bb', 'cc'])
    """
    if not sig:
        return None
    match = re.match(r"^(\d+)s:(\d+)x(\d+):(.+)$", sig)
    if not match:
        return None
    hashes = match.group(4).split(":")
    return float(match.group(1)), int(match.group(2)), int(match.group(3)), hashes


def videos_similar(sig_a: str | None, sig_b: str | None, t1: int) -> bool:
    """Conservative video match: duration <=1s apart, same aspect family,
    and a majority of keyframe hashes within T1 (spec Pass D).

    Usage:
        >>> videos_similar("10s:1920x1080:00:00:00", "10s:1280x720:00:00:00", 6)
        True
    """
    a, b = parse_video_sig(sig_a), parse_video_sig(sig_b)
    if a is None or b is None:
        return False
    dur_a, w_a, h_a, hashes_a = a
    dur_b, w_b, h_b, hashes_b = b
    if abs(dur_a - dur_b) > 1.0 or not aspect_close(w_a, h_a, w_b, h_b):
        return False
    close = sum(
        1
        for ha, hb in zip(hashes_a, hashes_b, strict=False)
        if ha != "?" and hb != "?" and (d := hamming(ha, hb)) is not None and d <= t1
    )
    return close >= 2


# --- Metadata merge -------------------------------------------------------------


def _truncate(date: str, precision: str | None) -> str:
    return date[: _PRECISION_CHARS.get(precision or "second", 19)]


def merge_dates(
    members: list[Media],
) -> tuple[dict[str, Any], bool]:
    """Pick the cluster date by provenance priority; flag disagreement.

    Priority: review > folder > trusted EXIF > takeout_json > filename. Every
    member must agree with the chosen date at the coarser of the two
    precisions (a 1998 year-folder brackets any 1998 EXIF second), else the
    cluster needs review.

    Usage:
        >>> merge_dates([])  # doctest: +ELLIPSIS
        ({'date': None, ...}, True)
    """
    dated = [m for m in members if m.resolved_date and m.resolved_source]
    if not dated:
        return (
            {"date": None, "precision": None, "source": None, "flags": ["date_unresolved"]},
            True,
        )

    def _reliable(m: Media) -> bool:
        # Two date sources are unreliable for detecting a genuine conflict: a
        # video's timestamp is often a re-encode time, not the capture moment,
        # and a takeout-derived folder date (rule R4b) is Google's *upload* year,
        # not when the photo was taken. Either contradicting a real capture date
        # is expected, not a decision for the user — so they never win the merge
        # and never raise a disagreement unless nothing better exists.
        if m.kind == "video":
            return False
        return not (m.resolved_source == "folder" and m.effective_trust != "curated")

    def _priority(m: Media) -> float:
        # A takeout-derived folder date (rule R4b) reflects Google's dating,
        # not the user's research (Stage 2b): rank it below takeout_json.
        if m.resolved_source == "folder" and m.effective_trust != "curated":
            return 1.5
        return _DATE_SOURCE_PRIORITY.get(m.resolved_source or "", 0)

    pool = [m for m in dated if _reliable(m)] or dated
    chosen = max(
        pool,
        key=lambda m: (
            _priority(m),
            _PRECISION_CHARS.get(m.resolved_precision or "year", 4),
            m.rel_path,
        ),
    )
    assert chosen.resolved_date is not None

    def _coarse_mismatch(m: Media) -> bool:
        # Compare at the coarser of the two precisions, but never finer than a
        # day: two copies from the same day (a Live Photo's image and video
        # seconds apart, or DateTimeOriginal vs CreateDate) are the same moment,
        # not a date disagreement. Only a different day/month/year is one.
        assert m.resolved_date is not None and chosen.resolved_date is not None
        chars = min(
            _PRECISION_CHARS["day"],
            _PRECISION_CHARS.get(m.resolved_precision or "day", 10),
            _PRECISION_CHARS.get(chosen.resolved_precision or "day", 10),
        )
        return m.resolved_date[:chars] != chosen.resolved_date[:chars]

    disagreement = any(_coarse_mismatch(m) for m in pool)
    flags = ["date_disagreement"] if disagreement else []
    if len(dated) < len(members):
        flags.append("date_unresolved_members")
    return (
        {
            "date": chosen.resolved_date,
            "precision": chosen.resolved_precision,
            "source": chosen.resolved_source,
            "flags": flags,
        },
        disagreement,
    )


def keyword_candidates(
    members: list[Media],
    albums: dict[int, list[str]],
    folder_patterns: tuple[str, ...],
) -> list[str]:
    """Topical keyword candidates: non-date folder components + album names.

    Usage:
        >>> m = Media(id=1, source="LOCAL", rel_path="topical/vacations/a.jpg",
        ...     kind="image", sha256="x", size_bytes=1, width=None, height=None,
        ...     phash=None, dhash=None, video_sig=None, exif_tag_count=0,
        ...     exif_dto=None, camera_model=None, gps_lat=None, gps_lon=None,
        ...     content_identifier=None, effective_trust="curated", archived=False,
        ...     google_recompressed=False, trusted_dto=False, resolved_date=None,
        ...     resolved_precision=None, resolved_source=None)
        >>> keyword_candidates([m], {}, ())
        ['topical', 'vacations']
    """
    compiled = [re.compile(p) for p in folder_patterns]
    candidates: set[str] = set()
    for m in members:
        for component in m.dirname.split("/"):
            if not component or component in _SCAFFOLD_DIRS:
                continue
            if _PHOTOS_FROM_RE.match(component):
                continue
            if any(p.match(component) for p in compiled):
                continue
            candidates.add(component)
        candidates.update(albums.get(m.id, []))
    return sorted(candidates)


# --- Orchestration --------------------------------------------------------------


@dataclass
class _ClusterPlan:
    kind: str
    members: list[Media]
    winner: Media
    scores: dict[int, tuple[float, dict[str, float]]]
    roles: dict[int, str]
    status: str  # auto | pending
    guardrails: list[str]
    merged: dict[str, Any]
    needs_review_merge: bool

    def content_key(self) -> tuple[Any, ...]:
        return (
            self.kind,
            self.status,
            self.winner.id,
            tuple(sorted((m.id, self.roles[m.id]) for m in self.members)),
        )


@dataclass
class DedupSummary:
    """Aggregate outcome of one dedup run."""

    clusters_total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    auto: int = 0
    pending_review: int = 0
    guardrails: dict[str, int] = field(default_factory=dict)
    locked_reviewed: int = 0
    singletons: int = 0
    changed: bool = False
    sample_size: int = 0


def _load_media(conn: sqlite3.Connection) -> list[Media]:
    rows = conn.execute(
        "SELECT i.id, i.source, i.rel_path, i.kind, i.sha256, i.size_bytes,"
        " i.width, i.height, i.phash, i.dhash, i.video_sig, i.exif_tag_count,"
        " i.exif_dto, i.camera_model, i.gps_lat, i.gps_lon, i.exif_json,"
        " i.effective_trust,"
        " i.google_recompressed, d.resolved_date, d.resolved_precision,"
        " d.resolved_source, d.exif_flags, d.status AS dr_status,"
        " (p.disposition = 'archive' AND p.verified_ok = 1) AS archived"
        " FROM instance i LEFT JOIN date_resolution d ON d.instance_id = i.id"
        " LEFT JOIN placement p ON p.instance_id = i.id"
        " WHERE i.kind IN ('image', 'video') ORDER BY i.source, i.rel_path"
    ).fetchall()
    missing = [r["rel_path"] for r in rows if r["dr_status"] is None]
    if missing:
        raise DedupError(
            f"{len(missing)} media instance(s) lack date resolution"
            " — run `archive date-resolve` first"
        )
    media = []
    for r in rows:
        exif = json.loads(r["exif_json"] or "{}")
        flags = json.loads(r["exif_flags"] or "[]")
        distrust = [f for f in flags if f != "from_create_date"]
        media.append(
            Media(
                id=r["id"], source=r["source"], rel_path=r["rel_path"],
                kind=r["kind"], sha256=r["sha256"], size_bytes=r["size_bytes"],
                width=r["width"], height=r["height"], phash=r["phash"],
                dhash=r["dhash"], video_sig=r["video_sig"],
                exif_tag_count=r["exif_tag_count"], exif_dto=r["exif_dto"],
                camera_model=r["camera_model"],
                gps_lat=r["gps_lat"], gps_lon=r["gps_lon"],
                content_identifier=content_identifier(exif),
                effective_trust=r["effective_trust"] or "curated",
                archived=bool(r["archived"]),
                google_recompressed=bool(r["google_recompressed"]),
                trusted_dto=bool(r["exif_dto"]) and not distrust,
                resolved_date=r["resolved_date"],
                resolved_precision=r["resolved_precision"],
                resolved_source=r["resolved_source"],
            )
        )
    return media


def _banded_candidates(reps: list[Media]) -> set[tuple[int, int]]:
    """Candidate near-dup pairs: 64-bit pHash split into 4 x 16-bit bands."""
    bands: dict[tuple[int, int], list[int]] = defaultdict(list)
    by_id = {m.id: m for m in reps}
    for m in reps:
        if not m.phash:
            continue
        try:
            value = int(m.phash, 16)
        except ValueError:
            continue
        for band in range(4):
            bands[(band, (value >> (band * 16)) & 0xFFFF)].append(m.id)
    pairs: set[tuple[int, int]] = set()
    for ids in bands.values():
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                pairs.add((min(a, b), max(a, b)))
    del by_id
    return pairs


def _tiebreak_key(m: Media) -> tuple[Any, ...]:
    # Already-archived instances win ties so incremental imports never churn
    # the archive with byte-identical newcomers (INV-5 stability).
    return (
        0 if m.archived else 1,
        0 if m.effective_trust == "curated" else 1,
        m.source,
        m.rel_path,
    )


def _plan_cluster(
    members: list[Media],
    kind: str,
    weak: bool,
    cfg: Config,
    albums: dict[int, list[str]],
    sidecars: dict[int, sqlite3.Row],
) -> _ClusterPlan:
    scores = {m.id: m.score() for m in members}
    ordered = sorted(members, key=lambda m: (-scores[m.id][0], *_tiebreak_key(m)))
    winner = ordered[0]
    guardrails: list[str] = []
    if kind != "exact":
        # Near-image clusters are near-identical by construction (pHash within
        # T1, dHash within T2, same aspect), so a close winner score is not a
        # decision worth a human: the higher-scored copy is taken and the rest
        # go to quarantine byte-identical (recoverable). The consequential cases
        # keep their own guardrails below — crops (aspect), Takeout-over-curated,
        # and the fuzzy possible-duplicate band (distances above T1). Exact
        # clusters are byte-identical, so they never flag on score either.
        if kind != "near_image" and len(ordered) > 1 and (
            scores[ordered[0].id][0] - scores[ordered[1].id][0]
            < cfg.dedup.guardrail_margin
        ):
            guardrails.append("score_margin")
        if not all(
            aspect_close(winner.width, winner.height, m.width, m.height)
            for m in members
            if m.id != winner.id and m.kind == winner.kind
        ):
            guardrails.append("aspect_mismatch")
    if winner.effective_trust == "takeout" and any(
        m.effective_trust == "curated"
        and (m.width, m.height) == (winner.width, winner.height)
        for m in members
        if m.id != winner.id
    ):
        guardrails.append("takeout_winner_over_curated")
    if weak:
        guardrails.append("possible_duplicate_band")
    if kind == "near_video":
        guardrails.append("video_never_auto")

    date_merge, date_review = merge_dates(members)
    merge_flags: list[str] = []
    # GPS: winner keeps its own; else any member's camera EXIF; else Takeout
    # JSON (user-added in Google Photos is acceptable but flagged).
    gps: dict[str, Any] = {"lat": None, "lon": None, "source": None}
    in_winner_order = [winner, *(m for m in members if m.id != winner.id)]
    for wanted in ("exif", "takeout"):
        for m in in_winner_order:
            source_kind, lat, lon = _instance_gps(sidecars, m)
            if source_kind == wanted and lat is not None:
                gps = {"lat": lat, "lon": lon, "source": source_kind}
                if source_kind == "takeout":
                    merge_flags.append("gps_from_takeout")
                break
        if gps["source"] is not None:
            break
    descriptions = sorted(
        {
            sidecars[m.id]["description"]
            for m in members
            if m.id in sidecars and sidecars[m.id]["description"]
        }
    )
    titles = sorted(
        {
            sidecars[m.id]["title"]
            for m in members
            if m.id in sidecars and sidecars[m.id]["title"]
        }
    )
    merged = {
        "date": date_merge,
        "gps": gps,
        "descriptions": descriptions,
        "titles": titles,
        "keyword_candidates": keyword_candidates(
            members, albums, cfg.dates.folder_patterns
        ),
        "flags": merge_flags,
    }
    status = "auto" if not guardrails and not date_review else "pending"
    roles = {
        m.id: ("winner" if m.id == winner.id else "loser") for m in members
    }
    return _ClusterPlan(
        kind=kind, members=ordered, winner=winner, scores=scores, roles=roles,
        status=status, guardrails=guardrails, merged=merged,
        needs_review_merge=date_review,
    )


def _instance_gps(
    sidecars: dict[int, sqlite3.Row], m: Media
) -> tuple[str | None, float | None, float | None]:
    """(source_kind, lat, lon) for one member: camera EXIF beats Takeout JSON."""
    if m.gps_lat is not None:
        return "exif", m.gps_lat, m.gps_lon
    row = sidecars.get(m.id)
    if row is not None and row["gps_lat"] is not None:
        return "takeout", row["gps_lat"], row["gps_lon"]
    return None, None, None


def _companion_plan(
    pair: tuple[Media, Media],
    kind: str,
    prefer_second_as_winner: bool,
    cfg: Config,
    albums: dict[int, list[str]],
    sidecars: dict[int, sqlite3.Row],
    exact_dups: Sequence[Media] = (),
) -> _ClusterPlan:
    """Plan a RAW+JPEG or Live Photo pair, absorbing any byte-identical copies.

    ``exact_dups`` are standalone instances that share a pair member's SHA-256
    (e.g. the same photo saved into a second album). They join as losers so
    their album keywords merge into the winner and they route to quarantine —
    otherwise each would archive to the identical file's path and collide.
    """
    first, second = pair
    winner, companion = (second, first) if prefer_second_as_winner else (first, second)
    plan = _plan_cluster(
        [winner, companion, *exact_dups], kind, False, cfg, albums, sidecars
    )
    plan.roles = {
        winner.id: "winner", companion.id: "companion",
        **{m.id: "loser" for m in exact_dups},
    }
    plan.winner = winner
    plan.guardrails = []
    plan.status = "auto" if not plan.needs_review_merge else "pending"
    return plan


def run_dedup(
    conn: sqlite3.Connection,
    cfg: Config,
    wt: WorkingTree,
    log: Logger,
    sample_size: int = 200,
) -> DedupSummary:
    """Cluster duplicates, score winners, and plan metadata merges.

    Usage:
        >>> summary = run_dedup(conn, cfg, wt, log)  # doctest: +SKIP
    """
    media = _load_media(conn)
    by_id = {m.id: m for m in media}
    locked_ids = {
        row["instance_id"]
        for row in conn.execute(
            "SELECT cm.instance_id FROM cluster_member cm"
            " JOIN cluster c ON c.id = cm.cluster_id WHERE c.status = 'reviewed'"
        )
    }
    pool = [m for m in media if m.id not in locked_ids]
    albums: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT media_instance_id, album FROM album_membership"):
        albums[row["media_instance_id"]].append(row["album"])
    sidecars = {
        row["media_instance_id"]: row
        for row in conn.execute(
            "SELECT media_instance_id, gps_lat, gps_lon, description, title"
            " FROM takeout_sidecar WHERE media_instance_id IS NOT NULL"
        )
    }
    edited_links = {
        frozenset((row["edited_instance_id"], row["original_instance_id"]))
        for row in conn.execute(
            "SELECT edited_instance_id, original_instance_id FROM edited_pair"
        )
    }

    all_by_sha: dict[str, list[Media]] = defaultdict(list)
    for m in pool:
        all_by_sha[m.sha256].append(m)

    plans: list[_ClusterPlan] = []
    companion_ids: set[int] = set()
    absorbed_ids: set[int] = set()  # exact dups pulled into a pair as losers

    def _exact_dups_of(*members: Media) -> list[Media]:
        out: dict[int, Media] = {}
        for pm in members:
            for d in all_by_sha.get(pm.sha256, []):
                if d.id not in {m.id for m in members} and d.id not in companion_ids \
                        and d.id not in absorbed_ids:
                    out[d.id] = d
        return sorted(out.values(), key=lambda m: m.id)

    for raw, jpeg in pair_raw_jpeg(pool):
        prefer_raw = cfg.policy.raw == "prefer_raw"
        dups = _exact_dups_of(raw, jpeg)
        plans.append(
            _companion_plan(
                (raw, jpeg), "pair_raw_jpeg", not prefer_raw, cfg, albums, sidecars, dups
            )
        )
        companion_ids.update((raw.id, jpeg.id))
        absorbed_ids.update(d.id for d in dups)
    for image, video in pair_live(
        [m for m in pool if m.id not in companion_ids]
    ):
        dups = _exact_dups_of(image, video)
        plans.append(
            _companion_plan(
                (image, video), "pair_live", False, cfg, albums, sidecars, dups
            )
        )
        companion_ids.update((image.id, video.id))
        absorbed_ids.update(d.id for d in dups)

    uf = UnionFind()
    weak_edges: set[int] = set()  # union-find members touched by a weak edge
    cluster_pool = [
        m for m in pool if m.id not in companion_ids and m.id not in absorbed_ids
    ]
    by_sha: dict[str, list[Media]] = defaultdict(list)
    for m in cluster_pool:
        by_sha[m.sha256].append(m)
    for group in by_sha.values():
        for other in group[1:]:
            uf.union(group[0].id, other.id)

    image_reps = [
        group[0]
        for group in by_sha.values()
        if group[0].kind == "image" and group[0].phash
    ]
    t1 = cfg.dedup.phash_threshold
    t2 = cfg.dedup.dhash_threshold
    band_hi = t1 + cfg.dedup.review_band
    for a_id, b_id in sorted(_banded_candidates(image_reps)):
        a, b = by_id[a_id], by_id[b_id]
        if frozenset((a_id, b_id)) in edited_links:
            continue
        d_p = hamming(a.phash, b.phash)
        d_d = hamming(a.dhash, b.dhash)
        if d_p is None or d_d is None or d_p > band_hi or d_d > t2:
            continue
        if not aspect_close(a.width, a.height, b.width, b.height):
            continue
        uf.union(a_id, b_id)
        if d_p > t1:
            weak_edges.update((a_id, b_id))

    video_reps = [
        group[0]
        for group in by_sha.values()
        if group[0].kind == "video" and group[0].video_sig
    ]
    for i, a in enumerate(video_reps):
        for b in video_reps[i + 1 :]:
            if frozenset((a.id, b.id)) in edited_links:
                continue
            if videos_similar(a.video_sig, b.video_sig, t1):
                uf.union(a.id, b.id)
                weak_edges.update((a.id, b.id))

    for _root, member_ids in sorted(uf.components().items()):
        expanded: list[Media] = []
        for mid in member_ids:
            expanded.extend(by_sha.get(by_id[mid].sha256, [by_id[mid]]))
        expanded = sorted({m.id: m for m in expanded}.values(), key=lambda m: m.id)
        if len(expanded) < 2:
            continue
        shas = {m.sha256 for m in expanded}
        if len(shas) == 1:
            kind = "exact"
        elif any(m.kind == "video" for m in expanded):
            kind = "near_video"
        else:
            kind = "near_image"
        weak = any(m.id in weak_edges for m in expanded)
        plans.append(_plan_cluster(expanded, kind, weak, cfg, albums, sidecars))

    summary = DedupSummary(locked_reviewed=len(locked_ids))
    clustered_ids = {m.id for p in plans for m in p.members} | locked_ids
    summary.singletons = sum(1 for m in media if m.id not in clustered_ids)
    for plan in plans:
        summary.clusters_total += 1
        summary.by_kind[plan.kind] = summary.by_kind.get(plan.kind, 0) + 1
        if plan.status == "auto":
            summary.auto += 1
        else:
            summary.pending_review += 1
        for g in plan.guardrails:
            summary.guardrails[g] = summary.guardrails.get(g, 0) + 1

    existing = {
        (
            row["kind"], row["status"], row["winner_instance_id"],
            tuple(sorted(
                (m["instance_id"], m["role"])
                for m in conn.execute(
                    "SELECT instance_id, role FROM cluster_member WHERE cluster_id = ?",
                    (row["id"],),
                )
            )),
        )
        for row in conn.execute("SELECT * FROM cluster WHERE status != 'reviewed'")
    }
    summary.changed = existing != {p.content_key() for p in plans}
    if summary.changed:
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        with conn:
            conn.execute(
                "DELETE FROM cluster_merge WHERE cluster_id IN"
                " (SELECT id FROM cluster WHERE status != 'reviewed')"
            )
            conn.execute(
                "DELETE FROM cluster_member WHERE cluster_id IN"
                " (SELECT id FROM cluster WHERE status != 'reviewed')"
            )
            conn.execute("DELETE FROM cluster WHERE status != 'reviewed'")
            for plan in plans:
                cur = conn.execute(
                    "INSERT INTO cluster (kind, status, winner_instance_id)"
                    " VALUES (?, ?, ?)",
                    (plan.kind, plan.status, plan.winner.id),
                )
                cluster_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO cluster_member (cluster_id, instance_id, score,"
                    " score_breakdown, role) VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            cluster_id, m.id, plan.scores[m.id][0],
                            json.dumps(plan.scores[m.id][1], sort_keys=True),
                            plan.roles[m.id],
                        )
                        for m in plan.members
                    ],
                )
                conn.execute(
                    "INSERT INTO cluster_merge (cluster_id, merged_json,"
                    " needs_review) VALUES (?, ?, ?)",
                    (
                        cluster_id,
                        json.dumps(plan.merged, sort_keys=True),
                        int(plan.needs_review_merge),
                    ),
                )
                conn.execute(
                    "INSERT INTO decision (ts, stage, subject, rule, detail, actor)"
                    " VALUES (?, 'dedup', ?, ?, ?, 'auto')",
                    (
                        now,
                        f"cluster:{cluster_id}",
                        f"dedup.{plan.kind}",
                        json.dumps(
                            {
                                "members": {
                                    m.rel_path: plan.scores[m.id][1]
                                    for m in plan.members
                                },
                                "winner": plan.winner.rel_path,
                                "status": plan.status,
                                "guardrails": plan.guardrails,
                                "merged": plan.merged,
                            },
                            sort_keys=True,
                        ),
                    ),
                )

    sample = sorted(plans, key=lambda p: (p.status != "pending", p.kind, p.winner.rel_path))
    sample = sample[:sample_size]
    summary.sample_size = len(sample)
    audit_path = wt.reports_dir / CLUSTER_AUDIT_REPORT
    with audit_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["kind", "status", "guardrails", "winner", "members", "score_gap"]
        )
        for plan in sample:
            ordered_scores = sorted(
                (plan.scores[m.id][0] for m in plan.members), reverse=True
            )
            gap = (
                round(ordered_scores[0] - ordered_scores[1], 3)
                if len(ordered_scores) > 1
                else ""
            )
            writer.writerow(
                [
                    plan.kind, plan.status, ";".join(plan.guardrails),
                    plan.winner.rel_path,
                    ";".join(m.rel_path for m in plan.members), gap,
                ]
            )

    log.info(
        "dedup complete",
        extra={
            "clusters": summary.clusters_total,
            "by_kind": summary.by_kind,
            "auto": summary.auto,
            "pending_review": summary.pending_review,
            "guardrails": summary.guardrails,
            "singletons": summary.singletons,
            "locked_reviewed": summary.locked_reviewed,
            "changed": summary.changed,
        },
    )
    return summary
