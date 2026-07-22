"""Dedup unit tests: scoring table, pairing, similarity, merge, guardrails."""

from typing import Any

from archive_pipeline.config import Config
from archive_pipeline.dedup import (
    Media,
    _plan_cluster,
    aspect_close,
    content_identifier,
    format_rank,
    hamming,
    keyword_candidates,
    merge_dates,
    pair_live,
    pair_raw_jpeg,
    parse_video_sig,
    score_instance,
    videos_similar,
)

CFG = Config()


def mk(id: int = 1, rel: str = "a/x.jpg", **kw: Any) -> Media:
    defaults: dict[str, Any] = {
        "id": id, "source": "LOCAL", "rel_path": rel, "kind": "image",
        "sha256": f"sha{id}", "size_bytes": 1_000_000, "width": 4000,
        "height": 3000, "phash": None, "dhash": None, "video_sig": None,
        "exif_tag_count": 30, "exif_dto": "2015-04-18T09:30:00",
        "camera_model": "Pixel", "gps_lat": None, "gps_lon": None,
        "content_identifier": None, "effective_trust": "curated", "archived": False,
        "google_recompressed": False, "trusted_dto": True,
        "resolved_date": "2015-04-18T09:30:00", "resolved_precision": "second",
        "resolved_source": "exif",
    }
    defaults.update(kw)
    return Media(**defaults)


# --- Primitives ----------------------------------------------------------------


def test_hamming() -> None:
    assert hamming("00", "00") == 0
    assert hamming("ff", "00") == 8
    assert hamming("8f00aa00bb00cc00", "8f00aa00bb00cc01") == 1
    assert hamming("00", None) is None
    assert hamming("00", "0000") is None  # length mismatch


def test_aspect_close() -> None:
    assert aspect_close(1920, 1080, 1280, 720)
    assert aspect_close(4000, 3000, 2000, 1500)
    assert not aspect_close(4000, 3000, 3000, 3000)
    assert not aspect_close(None, 3000, 2000, 1500)


def test_parse_video_sig_and_similarity() -> None:
    assert parse_video_sig("12s:1920x1080:aa:bb:cc") == (
        12.0, 1920, 1080, ["aa", "bb", "cc"]
    )
    assert parse_video_sig("garbage") is None
    same = "10s:1920x1080:00:11:22"
    assert videos_similar(same, "10s:1280x720:00:11:22", 6)  # same family
    assert not videos_similar(same, "13s:1920x1080:00:11:22", 6)  # duration
    assert not videos_similar(same, "10s:1080x1080:00:11:22", 6)  # aspect
    assert not videos_similar(same, "10s:1920x1080:ff:ee:22", 6)  # keyframes
    assert not videos_similar(same, None, 6)


# --- Scoring: table-driven pins of the spec formula ----------------------------


def test_score_formula_components() -> None:
    _, parts = score_instance(
        rel_path="a.jpg", width=4000, height=3000, size_bytes=5_000_000,
        exif_tag_count=60, has_trusted_dto=True, curated_trust=True,
        google_recompressed=False,
    )
    assert parts == {
        "resolution": 10.7549,       # 3.0 * log2(12 MP)
        "format": 1.5,               # JPEG original
        "metadata_richness": 1.0,    # 60/60 tags
        "trusted_dto": 1.0,
        "source_trust": 0.75,
        "size_tiebreak": 3.3495,     # 0.5 * log10(5e6)
        "recompression_penalty": 0.0,
    }


def test_score_recompressed_penalty_and_format() -> None:
    score_orig, _ = score_instance(
        rel_path="a.jpg", width=1000, height=1000, size_bytes=1000,
        exif_tag_count=0, has_trusted_dto=False, curated_trust=False,
        google_recompressed=False,
    )
    score_rec, parts = score_instance(
        rel_path="a.jpg", width=1000, height=1000, size_bytes=1000,
        exif_tag_count=0, has_trusted_dto=False, curated_trust=False,
        google_recompressed=True,
    )
    # Recompression costs the -2.0 penalty plus the format rank (1.5 -> 0).
    assert round(score_orig - score_rec, 4) == 3.5
    assert parts["recompression_penalty"] == -2.0
    assert parts["format"] == 0.0


def test_score_resolution_dominates() -> None:
    big, _ = score_instance(
        rel_path="a.jpg", width=4000, height=3000, size_bytes=1000,
        exif_tag_count=0, has_trusted_dto=False, curated_trust=False,
        google_recompressed=False,
    )
    small, _ = score_instance(
        rel_path="b.jpg", width=1000, height=750, size_bytes=10_000_000,
        exif_tag_count=60, has_trusted_dto=True, curated_trust=True,
        google_recompressed=False,
    )
    assert big > small - 3  # 16x pixels ~= 12 points; extras total < 9


def test_format_rank() -> None:
    assert format_rank("x/y.dng", False) == 2.0
    assert format_rank("x/y.CR2", False) == 2.0
    assert format_rank("x/y.jpg", False) == 1.0
    assert format_rank("x/y.jpg", True) == 0.0


def test_score_handles_missing_dimensions() -> None:
    score, parts = score_instance(
        rel_path="a.jpg", width=None, height=None, size_bytes=0,
        exif_tag_count=None, has_trusted_dto=False, curated_trust=False,
        google_recompressed=False,
    )
    assert parts["resolution"] < 0  # log2(0.01) floor
    assert isinstance(score, float)


# --- Companion pairing ---------------------------------------------------------


def test_raw_jpeg_pair_by_stem() -> None:
    raw = mk(1, "roll/IMG_0001.CR2")
    jpeg = mk(2, "roll/img_0001.jpg")
    other = mk(3, "roll/IMG_0002.jpg")
    assert pair_raw_jpeg([raw, jpeg, other]) == [(raw, jpeg)]


def test_raw_jpeg_pair_by_camera_and_time() -> None:
    raw = mk(1, "roll/DSC01.ARW", exif_dto="2015-04-18T09:30:00")
    jpeg = mk(2, "roll/photo.jpg", exif_dto="2015-04-18T09:30:01")
    late = mk(3, "roll/late.jpg", exif_dto="2015-04-18T09:30:05")
    assert pair_raw_jpeg([raw, jpeg, late]) == [(raw, jpeg)]
    assert pair_raw_jpeg([raw, late]) == []


def test_raw_jpeg_requires_same_directory() -> None:
    raw = mk(1, "roll/IMG_0001.CR2")
    jpeg = mk(2, "other/IMG_0001.jpg")
    assert pair_raw_jpeg([raw, jpeg]) == []


def test_live_pair_by_content_identifier() -> None:
    image = mk(1, "x/IMG_1.HEIC", content_identifier="AB-12")
    video = mk(2, "x/IMG_1.MOV", kind="video", content_identifier="AB-12")
    unrelated = mk(3, "x/IMG_2.MOV", kind="video", content_identifier="CD-34")
    assert pair_live([image, video, unrelated]) == [(image, video)]


def test_content_identifier_from_any_group() -> None:
    assert content_identifier({"MakerNotes:ContentIdentifier": "AB"}) == "AB"
    assert content_identifier({"QuickTime:ContentIdentifier": "CD"}) == "CD"
    assert content_identifier({"EXIF:Make": "Apple"}) is None


# --- Metadata merge ------------------------------------------------------------


def test_merge_dates_folder_beats_trusted_exif_and_year_brackets_it() -> None:
    exif = mk(1, resolved_source="exif", resolved_date="1998-07-12T14:33:05")
    folder = mk(2, resolved_source="folder", resolved_date="1998-01-01",
                resolved_precision="year")
    merged, needs_review = merge_dates([exif, folder])
    assert merged["source"] == "folder"
    assert merged["date"] == "1998-01-01"
    assert not needs_review  # 1998 folder brackets any 1998 EXIF second


def test_merge_dates_review_beats_folder_but_day_conflict_flags() -> None:
    exif = mk(1, resolved_source="exif", resolved_date="1998-07-12T14:33:05")
    review = mk(3, resolved_source="review", resolved_date="1998-07-14",
                resolved_precision="day")
    merged, needs_review = merge_dates([exif, review])
    assert merged["source"] == "review"
    assert merged["date"] == "1998-07-14"
    assert needs_review  # July 12 EXIF vs July 14 review disagree at day level


def test_merge_dates_disagreement_flags_review() -> None:
    a = mk(1, resolved_source="exif", resolved_date="1998-07-12T14:33:05")
    b = mk(2, resolved_source="takeout_json", resolved_date="2003-01-01T00:00:00")
    merged, needs_review = merge_dates([a, b])
    assert merged["source"] == "exif"
    assert needs_review
    assert "date_disagreement" in merged["flags"]


def test_merge_dates_takeout_derived_folder_ranks_below_sidecar() -> None:
    untrusted_folder = mk(
        1, resolved_source="folder", resolved_date="2015-01-01",
        resolved_precision="year", effective_trust="takeout",
    )
    sidecar = mk(
        2, resolved_source="takeout_json", resolved_date="2015-04-18T12:00:00",
        effective_trust="takeout",
    )
    merged, needs_review = merge_dates([untrusted_folder, sidecar])
    assert merged["source"] == "takeout_json"
    assert merged["date"] == "2015-04-18T12:00:00"
    assert not needs_review  # the 2015 year folder brackets the sidecar time


def test_merge_dates_nothing_resolved() -> None:
    merged, needs_review = merge_dates(
        [mk(1, resolved_date=None, resolved_source=None)]
    )
    assert needs_review
    assert merged["date"] is None
    assert "date_unresolved" in merged["flags"]


def test_keyword_candidates_excludes_dates_and_scaffolding() -> None:
    members = [
        mk(1, "topical/vacations/x.jpg"),
        mk(2, "1998/x.jpg"),
        mk(3, "Takeout/Google Photos/Photos from 2015/x.jpg"),
    ]
    albums = {3: ["Vacation 2015"]}
    assert keyword_candidates(members, albums, CFG.dates.folder_patterns) == [
        "Vacation 2015", "topical", "vacations",
    ]


# --- Guardrails (via cluster planning) -----------------------------------------


def _plan(members: list[Media], kind: str = "near_image", weak: bool = False) -> Any:
    return _plan_cluster(members, kind, weak, CFG, {}, {})


def test_margin_guardrail_fires_on_close_scores() -> None:
    a = mk(1, "a/x.jpg", sha256="s1")
    b = mk(2, "b/x.jpg", sha256="s2")  # identical facts -> identical score
    plan = _plan([a, b])
    assert plan.status == "pending"
    assert "score_margin" in plan.guardrails


def test_margin_guardrail_skipped_for_exact_clusters() -> None:
    a = mk(1, "a/x.jpg", sha256="same")
    b = mk(2, "b/x.jpg", sha256="same")
    plan = _plan([a, b], kind="exact")
    assert plan.status == "auto"
    assert plan.guardrails == []
    assert plan.winner.id == 1  # deterministic tiebreak: curated, then path


def test_takeout_winner_over_curated_guardrail() -> None:
    takeout = mk(
        1, "t/x.jpg", sha256="s1", effective_trust="takeout",
        exif_tag_count=60, trusted_dto=True,
    )
    curated = mk(
        2, "c/x.jpg", sha256="s2", effective_trust="curated",
        exif_tag_count=0, trusted_dto=False, exif_dto=None,
    )
    plan = _plan([takeout, curated])
    assert plan.winner.id == 1
    assert "takeout_winner_over_curated" in plan.guardrails
    assert plan.status == "pending"


def test_aspect_crop_guardrail() -> None:
    a = mk(1, "a/x.jpg", sha256="s1", width=4000, height=3000)
    b = mk(2, "b/x.jpg", sha256="s2", width=3000, height=3000,
           exif_tag_count=0)  # lower score, different aspect
    plan = _plan([a, b])
    assert "aspect_mismatch" in plan.guardrails
    assert plan.status == "pending"


def test_weak_band_queues_for_review() -> None:
    a = mk(1, "a/x.jpg", sha256="s1")
    b = mk(2, "b/x.jpg", sha256="s2", width=2000, height=1500, exif_tag_count=0)
    plan = _plan([a, b], weak=True)
    assert "possible_duplicate_band" in plan.guardrails
    assert plan.status == "pending"


def test_gps_prefers_camera_exif_over_takeout() -> None:
    a = mk(1, "a/x.jpg", sha256="s1")  # winner, no GPS
    b = mk(2, "b/x.jpg", sha256="s2", width=2000, height=1500, exif_tag_count=0,
           gps_lat=44.06, gps_lon=-71.29)
    sidecars = {1: {"gps_lat": 10.0, "gps_lon": 20.0, "description": "d", "title": "t"}}
    plan = _plan_cluster([a, b], "near_image", False, CFG, {}, sidecars)  # type: ignore[arg-type]
    assert plan.merged["gps"] == {"lat": 44.06, "lon": -71.29, "source": "exif"}
    assert plan.merged["descriptions"] == ["d"]


def test_gps_from_takeout_is_flagged() -> None:
    a = mk(1, "a/x.jpg", sha256="s1")
    b = mk(2, "b/x.jpg", sha256="s2", width=2000, height=1500, exif_tag_count=0)
    sidecars = {2: {"gps_lat": 10.0, "gps_lon": 20.0, "description": None, "title": None}}
    plan = _plan_cluster([a, b], "near_image", False, CFG, {}, sidecars)  # type: ignore[arg-type]
    assert plan.merged["gps"]["source"] == "takeout"
    assert "gps_from_takeout" in plan.merged["flags"]
