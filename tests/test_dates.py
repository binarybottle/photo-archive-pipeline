"""Stage 3 unit tests: one per rule R1..R7 (+R4b) and per distrust heuristic."""

from archive_pipeline.config import (
    DEFAULT_FILENAME_DATE_PATTERNS,
    DEFAULT_FOLDER_DATE_PATTERNS,
)
from archive_pipeline.dates import (
    Candidates,
    compute_exif_flags,
    exif_candidate,
    filename_candidate,
    folder_candidate,
    resolve,
)

# --- Candidate extraction ------------------------------------------------------


def test_folder_candidate_precisions() -> None:
    p = DEFAULT_FOLDER_DATE_PATTERNS
    assert folder_candidate("1998/beach.jpg", p) == ("1998-01-01", "year")
    assert folder_candidate("2003-07/park.jpg", p) == ("2003-07-01", "month")
    assert folder_candidate("2010-06-15_wedding/w.jpg", p) == ("2010-06-15", "day")
    assert folder_candidate("2010_wedding/w.jpg", p) == ("2010-01-01", "year")
    assert folder_candidate("20100615_wedding/w.jpg", p) == ("2010-06-15", "day")
    assert folder_candidate("Photos from 2015/a.jpg", p) == ("2015-01-01", "year")
    assert folder_candidate("scans/a.jpg", p) is None


def test_folder_candidate_deepest_component_wins() -> None:
    p = DEFAULT_FOLDER_DATE_PATTERNS
    assert folder_candidate("1998/1998-07/a.jpg", p) == ("1998-07-01", "month")
    # The filename itself is never a folder component.
    assert folder_candidate("scans/1998.jpg", p) is None


def test_folder_candidate_rejects_invalid_dates() -> None:
    assert folder_candidate("2010-02-31/a.jpg", DEFAULT_FOLDER_DATE_PATTERNS) is None


def test_folder_candidate_year_at_end_and_uncertain() -> None:
    p = DEFAULT_FOLDER_DATE_PATTERNS
    assert folder_candidate("Europe_2010/a.jpg", p) == ("2010-01-01", "year")
    assert folder_candidate("Metamorphosis_exhibit_2005/a.jpg", p) == ("2005-01-01", "year")
    assert folder_candidate("2005?/a.jpg", p) == ("2005-01-01", "year")
    # Ranges stay ambiguous — never auto-assigned to a single year.
    assert folder_candidate("2004-2009/a.jpg", p) is None
    assert folder_candidate("2000-2003/a.jpg", p) is None
    # Non-date topical folders remain unmatched.
    assert folder_candidate("videocalls/a.jpg", p) is None
    assert folder_candidate("Japan/a.jpg", p) is None


def test_folder_candidate_embedded_yyyymmdd() -> None:
    p = DEFAULT_FOLDER_DATE_PATTERNS
    assert folder_candidate("card-telling_20060624/v.mp4", p) == ("2006-06-24", "day")
    assert folder_candidate("trip_20060624_final/a.jpg", p) == ("2006-06-24", "day")
    # Deepest date-bearing component wins.
    assert folder_candidate("2010/event_20100715/a.jpg", p) == ("2010-07-15", "day")
    # An explicit YYYY-MM-DD folder still resolves via its own pattern, not this.
    assert folder_candidate("2010-06-15/a.jpg", p) == ("2010-06-15", "day")
    # A YYYYMM folder name resolves at month precision.
    assert folder_candidate("200804_SanFrancisco/a.jpg", p) == ("2008-04-01", "month")
    # No false positives on ranges or non-date digit runs.
    assert folder_candidate("2004-2009/a.jpg", p) is None
    assert folder_candidate("batch_00012345/a.jpg", p) is None  # year 0001
    assert folder_candidate("id_20069999/a.jpg", p) is None     # month 99


def test_filename_candidate_patterns() -> None:
    p = DEFAULT_FILENAME_DATE_PATTERNS
    assert filename_candidate("x/IMG_20150418_093000.jpg", p) == (
        "2015-04-18T09:30:00", "second"
    )
    assert filename_candidate("x/PXL_20220101_120000123.jpg", p) == (
        "2022-01-01T12:00:00", "second"
    )
    assert filename_candidate("x/IMG-20150418-WA0001.jpg", p) == ("2015-04-18", "day")
    assert filename_candidate("x/scan001.jpg", p) is None
    assert filename_candidate("x/IMG_20151340_000000.jpg", p) is None  # month 13


def test_filename_candidate_embedded_date() -> None:
    p = DEFAULT_FILENAME_DATE_PATTERNS
    # A valid YYYYMMDD embedded mid-name (day precision).
    assert filename_candidate("x/kory_nyc_200704_ellora_20070408_PD(10).JPG", p) == (
        "2007-04-08", "day"
    )
    assert filename_candidate("x/20030916_ultrasound.jpg", p) == ("2003-09-16", "day")
    assert filename_candidate("x/IMG00063-20101121-1633.jpg", p) == ("2010-11-21", "day")
    # No false positives: sub-date numbers, invalid months/days, long digit runs.
    assert filename_candidate("x/20991340.jpg", p) is None          # month 13, day 40
    assert filename_candidate("x/serial_120070408.jpg", p) is None  # 9-digit run
    assert filename_candidate("x/12345678.jpg", p) is None          # year 1234


def test_filename_candidate_year_month() -> None:
    p = DEFAULT_FILENAME_DATE_PATTERNS
    assert filename_candidate("x/200804_SanFrancisco_DSCN0200.JPG", p) == (
        "2008-04-01", "month"
    )
    # A full YYYYMMDD still wins over the shorter YYYYMM form.
    assert filename_candidate("x/20080415_trip.jpg", p) == ("2008-04-15", "day")
    # Guards: invalid month, and 6 digits inside a longer run are not months.
    assert filename_candidate("x/209913_x.jpg", p) is None           # month 13
    assert filename_candidate("x/id_2008041.jpg", p) is None         # 7-digit run


def test_exif_candidate_fallback_to_createdate() -> None:
    assert exif_candidate("1998-07-12T14:33:05", {"XMP:CreateDate": "x"}) == (
        "1998-07-12T14:33:05", False
    )
    assert exif_candidate(None, {"XMP:CreateDate": "2019:11:03 10:00:00"}) == (
        "2019-11-03T10:00:00", True
    )
    assert exif_candidate(None, {}) == (None, False)


# --- Distrust heuristics (one test each) ---------------------------------------


def _flags(**kwargs: object) -> list[str]:
    defaults: dict[str, object] = {
        "cand_exif": "2010-06-15T18:00:00",
        "exif_from_createdate": False,
        "mime": "image/jpeg",
        "camera_model": None,
        "camera_era": {},
        "folder": None,
        "folder_precision": None,
        "mass_identical": False,
    }
    defaults.update(kwargs)
    return compute_exif_flags(**defaults)  # type: ignore[arg-type]


def test_flag_epoch_default() -> None:
    for date in ("1970-01-01T00:00:00", "1980-01-01T12:00:00", "2000-01-01T00:00:00"):
        assert _flags(cand_exif=date) == ["epoch_default"]
    assert _flags() == []


def test_flag_mass_identical() -> None:
    assert _flags(mass_identical=True) == ["mass_identical"]


def test_flag_predates_camera() -> None:
    assert _flags(
        cand_exif="1996-01-01T00:00:00", camera_model="PowerShot A5",
        camera_era={"PowerShot A5": 1998},
    ) == ["predates_camera"]
    assert _flags(
        cand_exif="1999-01-01T00:00:00", camera_model="PowerShot A5",
        camera_era={"PowerShot A5": 1998},
    ) == []


def test_flag_scanner_date() -> None:
    assert _flags(
        cand_exif="2019-11-03T10:00:00", folder="2001-07-01", folder_precision="month"
    ) == ["scanner_date"]
    # Year-precision folders don't trigger the scan-date signature.
    assert _flags(
        cand_exif="2019-11-03T10:00:00", folder="2001-01-01", folder_precision="year"
    ) == []


def test_flag_scanner_createdate() -> None:
    assert _flags(exif_from_createdate=True, mime="image/png") == ["scanner_createdate"]
    assert _flags(exif_from_createdate=True, mime="image/jpeg") == []


# --- Resolution rules (one test each) ------------------------------------------

EXIF = "1998-07-12T14:33:05"


def test_r1_exif_within_folder_granularity() -> None:
    r = resolve(
        Candidates(exif=EXIF, folder="1998-01-01", folder_precision="year",
                   folder_trusted=True),
        [],
    )
    assert (r.rule, r.source, r.date, r.precision, r.status) == (
        "R1", "exif", EXIF, "second", "auto"
    )


def test_r1_day_folder_tolerates_same_month_exif() -> None:
    # A day-labeled folder with a photo from a nearby day in the same month
    # uses the precise EXIF (R1), not a conflict — multi-day event folders.
    r = resolve(
        Candidates(exif="2006-06-28T10:00:00", folder="2006-06-24",
                   folder_precision="day", folder_trusted=True),
        [],
    )
    assert (r.rule, r.source, r.date) == ("R1", "exif", "2006-06-28T10:00:00")


def test_r6_day_folder_conflicts_different_month() -> None:
    r = resolve(
        Candidates(exif="2006-09-01T10:00:00", folder="2006-06-24",
                   folder_precision="day", folder_trusted=True),
        [],
    )
    assert (r.rule, r.status) == ("R6", "conflict")


def test_r2_exif_trusted_no_folder() -> None:
    r = resolve(Candidates(exif=EXIF), [])
    assert (r.rule, r.source, r.date) == ("R2", "exif", EXIF)


def test_r3_folder_when_exif_distrusted() -> None:
    r = resolve(
        Candidates(exif="2000-01-01T00:00:00", folder="2003-07-01",
                   folder_precision="month", folder_trusted=True),
        ["epoch_default"],
    )
    assert (r.rule, r.source, r.date, r.precision) == ("R3", "folder", "2003-07-01", "month")


def test_r4_takeout_candidate() -> None:
    r = resolve(Candidates(takeout="2015-04-18T09:30:00+00:00"), [])
    assert (r.rule, r.source, r.status) == ("R4", "takeout_json", "auto")


def test_r4_skips_upload_artifact() -> None:
    r = resolve(
        Candidates(takeout="2015-04-18T09:30:00+00:00", takeout_is_upload_artifact=True),
        [],
    )
    assert r.rule == "R7"


def test_r4b_untrusted_folder_after_takeout() -> None:
    takeout_first = resolve(
        Candidates(folder="2015-01-01", folder_precision="year", folder_trusted=False,
                   takeout="2015-04-18T09:30:00+00:00"),
        [],
    )
    assert takeout_first.rule == "R4"
    folder_fallback = resolve(
        Candidates(folder="2015-01-01", folder_precision="year", folder_trusted=False),
        [],
    )
    assert (folder_fallback.rule, folder_fallback.source, folder_fallback.confidence) == (
        "R4b", "folder", 0.6
    )


def test_r5_filename_last_resort() -> None:
    r = resolve(
        Candidates(filename="2015-04-18T09:30:00", filename_precision="second"), []
    )
    assert (r.rule, r.source) == ("R5", "filename")


def test_r6_exif_conflicts_with_folder() -> None:
    r = resolve(
        Candidates(exif=EXIF, folder="2003-01-01", folder_precision="year",
                   folder_trusted=True),
        [],
    )
    assert (r.rule, r.status, r.date) == ("R6", "conflict", None)


def test_r7_nothing_usable() -> None:
    r = resolve(Candidates(), [])
    assert (r.rule, r.status) == ("R7", "conflict")


def test_distrusted_exif_never_wins_over_curated_folder() -> None:
    r = resolve(
        Candidates(exif=EXIF, folder="1998-01-01", folder_precision="year",
                   folder_trusted=True),
        ["mass_identical"],
    )
    assert r.rule == "R3"


# --- Filename refinement of coarse folder dates --------------------------------


def test_r3f_filename_refines_curated_year_folder() -> None:
    r = resolve(
        Candidates(folder="2003-01-01", folder_precision="year", folder_trusted=True,
                   filename="2003-09-16", filename_precision="day"),
        ["epoch_default"],
    )
    assert (r.rule, r.source, r.date, r.precision, r.status) == (
        "R3f", "filename", "2003-09-16", "day", "auto"
    )


def test_r3c_filename_contradicts_curated_folder_year() -> None:
    r = resolve(
        Candidates(folder="2003-01-01", folder_precision="year", folder_trusted=True,
                   filename="2007-04-08", filename_precision="day"),
        ["epoch_default"],
    )
    assert (r.rule, r.status, r.date) == ("R3c", "conflict", None)


def test_r4bf_filename_refines_takeout_year_folder() -> None:
    r = resolve(
        Candidates(folder="2019-01-01", folder_precision="year", folder_trusted=False,
                   filename="2019-08-12", filename_precision="day"),
        [],
    )
    assert (r.rule, r.source, r.date, r.precision) == (
        "R4bf", "filename", "2019-08-12", "day"
    )


def test_r4bf_filename_trusted_over_takeout_year_bucket() -> None:
    # "Photos from YYYY" is Google's upload-year bucket, too weak to contest a
    # capture date: the filename wins even when the years disagree (no review).
    r = resolve(
        Candidates(folder="2019-01-01", folder_precision="year", folder_trusted=False,
                   filename="2015-08-12", filename_precision="day"),
        [],
    )
    assert (r.rule, r.source, r.date, r.status) == (
        "R4bf", "filename", "2015-08-12", "auto"
    )


def test_folder_stands_when_no_filename_date() -> None:
    # Regression: the refinement path must not disturb plain folder resolution.
    curated = resolve(
        Candidates(folder="2003-07-01", folder_precision="month", folder_trusted=True), []
    )
    assert (curated.rule, curated.source, curated.date) == ("R3", "folder", "2003-07-01")
    takeout = resolve(
        Candidates(folder="2019-01-01", folder_precision="year", folder_trusted=False), []
    )
    assert (takeout.rule, takeout.source) == ("R4b", "folder")


def test_takeout_sidecar_beats_filename_refinement() -> None:
    # A usable sidecar (R4) still wins before the folder/filename step.
    r = resolve(
        Candidates(folder="2019-01-01", folder_precision="year", folder_trusted=False,
                   takeout="2019-08-12T10:00:00", filename="2019-08-13",
                   filename_precision="day"),
        [],
    )
    assert r.rule == "R4"
