"""Unit tests for exiftool date normalization and field extraction."""

from archive_pipeline.metadata import exif_date_to_iso, extract_fields


def test_exif_date_plain() -> None:
    assert exif_date_to_iso("1998:07:12 14:33:05") == "1998-07-12T14:33:05"


def test_exif_date_with_offset_and_subseconds() -> None:
    assert exif_date_to_iso("2015:04:18 09:30:00+02:00") == "2015-04-18T09:30:00+02:00"
    assert exif_date_to_iso("2015:04:18 09:30:00.123Z") == "2015-04-18T09:30:00.123+00:00"


def test_exif_date_garbage_is_none() -> None:
    assert exif_date_to_iso(None) is None
    assert exif_date_to_iso("") is None
    assert exif_date_to_iso("0000:00:00 00:00:00") is None
    assert exif_date_to_iso("not a date") is None
    assert exif_date_to_iso("2015-04-18") is None


def test_extract_fields_basics() -> None:
    raw = {
        "SourceFile": "/x/a.jpg",
        "File:MIMEType": "image/jpeg",
        "File:FileSize": 1234,
        "EXIF:DateTimeOriginal": "1998:07:12 14:33:05",
        "EXIF:Make": "Canon",
        "EXIF:Model": "PowerShot A5",
        "Composite:GPSLatitude": 44.06,
        "Composite:GPSLongitude": -71.29,
        "ExifTool:ExifToolVersion": 13.0,
    }
    meta = extract_fields(raw)
    assert meta.mime == "image/jpeg"
    assert meta.exif_dto == "1998-07-12T14:33:05"
    assert meta.camera_make == "Canon"
    assert meta.gps_lat == 44.06 and meta.gps_lon == -71.29
    # Richness counts only embedded-metadata groups (EXIF here), not
    # File/Composite/ExifTool bookkeeping.
    assert meta.exif_tag_count == 3
    assert meta.error is None


def test_extract_fields_error_entry() -> None:
    meta = extract_fields({"SourceFile": "/x/empty.jpg", "ExifTool:Error": "File is empty"})
    assert meta.error == "File is empty"
    assert meta.mime is None
    assert meta.exif_tag_count == 0
