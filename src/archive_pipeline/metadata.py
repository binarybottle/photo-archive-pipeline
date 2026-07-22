"""Metadata reading via exiftool in stay-open batch mode (spec section 6).

exiftool is the only tool trusted to touch metadata. This module only *reads*;
writing arrives with materialize (M7). Field extraction turns the raw group-
prefixed dump into the catalog's `instance` columns, including signature-based
MIME detection and the populated-tag richness metric.

Usage:
    >>> from pathlib import Path
    >>> from archive_pipeline.metadata import MetadataReader, extract_fields
    >>> with MetadataReader() as reader:  # doctest: +SKIP
    ...     raw = reader.read_batch([Path("a.jpg")])[0]
    ...     extract_fields(raw).mime
    'image/jpeg'
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from exiftool import ExifToolHelper

_BATCH_SIZE = 200

#: exiftool date form: "YYYY:MM:DD HH:MM:SS" with optional subseconds/offset.
_EXIF_DATE_RE = re.compile(
    r"^(?P<y>\d{4}):(?P<mo>\d{2}):(?P<d>\d{2})[ T]"
    r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})(?P<sub>\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)

#: Groups that do not count toward metadata richness (filesystem facts,
#: exiftool bookkeeping, and values derived rather than embedded).
_NON_RICHNESS_PREFIXES = ("File:", "ExifTool:", "Composite:")


def exif_date_to_iso(value: str | None) -> str | None:
    """Normalize an exiftool date string to ISO-8601, or None if unparseable.

    Usage:
        >>> exif_date_to_iso("1998:07:12 14:33:05")
        '1998-07-12T14:33:05'
        >>> exif_date_to_iso("2015:04:18 09:30:00+02:00")
        '2015-04-18T09:30:00+02:00'
        >>> exif_date_to_iso("0000:00:00 00:00:00") is None
        True
    """
    if not value:
        return None
    match = _EXIF_DATE_RE.match(value.strip())
    if match is None:
        return None
    y, mo, d = match["y"], match["mo"], match["d"]
    if y == "0000" or mo == "00" or d == "00":
        return None
    iso = f"{y}-{mo}-{d}T{match['h']}:{match['mi']}:{match['s']}"
    if match["sub"]:
        iso += match["sub"]
    if match["tz"]:
        iso += "+00:00" if match["tz"] == "Z" else match["tz"]
    return iso


@dataclass(frozen=True)
class ExtractedMeta:
    """The `instance` columns derived from one exiftool dump."""

    mime: str | None
    exif_dto: str | None  # DateTimeOriginal, ISO-normalized
    gps_lat: float | None
    gps_lon: float | None
    camera_make: str | None
    camera_model: str | None
    exif_tag_count: int
    width: int | None
    height: int | None
    exif_json: str
    error: str | None  # exiftool's per-file error (e.g. "File is empty")


def extract_fields(raw: dict[str, Any]) -> ExtractedMeta:
    """Extract catalog fields from one raw exiftool dump (``-G -n`` mode).

    Usage:
        >>> meta = extract_fields({"File:MIMEType": "image/jpeg",
        ...                        "EXIF:DateTimeOriginal": "1998:07:12 14:33:05"})
        >>> meta.exif_dto
        '1998-07-12T14:33:05'
    """

    def _num(key: str) -> float | None:
        value = raw.get(key)
        return float(value) if isinstance(value, int | float) else None

    def _int(*keys: str) -> int | None:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, int):
                return value
        return None

    def _str(*keys: str) -> str | None:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    tag_count = sum(
        1
        for key in raw
        if key != "SourceFile" and not key.startswith(_NON_RICHNESS_PREFIXES)
    )
    return ExtractedMeta(
        mime=_str("File:MIMEType"),
        exif_dto=exif_date_to_iso(_str("EXIF:DateTimeOriginal")),
        gps_lat=_num("Composite:GPSLatitude"),
        gps_lon=_num("Composite:GPSLongitude"),
        camera_make=_str("EXIF:Make"),
        camera_model=_str("EXIF:Model"),
        exif_tag_count=tag_count,
        width=_int("File:ImageWidth", "EXIF:ExifImageWidth", "PNG:ImageWidth"),
        height=_int("File:ImageHeight", "EXIF:ExifImageHeight", "PNG:ImageHeight"),
        exif_json=json.dumps(raw, sort_keys=True, default=str),
        error=_str("ExifTool:Error"),
    )


class MetadataReader:
    """Context manager holding one stay-open exiftool process for batch reads.

    ``check_execute`` is off so per-file problems (empty/unknown files) surface
    as ``ExifTool:Error`` entries instead of aborting the whole batch.

    Usage:
        >>> with MetadataReader() as reader:  # doctest: +SKIP
        ...     dumps = reader.read_batch([Path("a.jpg"), Path("b.png")])
    """

    def __init__(self) -> None:
        self._helper = ExifToolHelper(common_args=["-G", "-n"], check_execute=False)

    def __enter__(self) -> Self:
        self._helper.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._helper.__exit__(exc_type, exc, tb)

    def read_batch(self, paths: list[Path]) -> list[dict[str, Any]]:
        """Read raw metadata dumps for ``paths``, preserving input order.

        Files exiftool cannot open at all still yield an entry (with an
        ``ExifTool:Error`` key), so callers can rely on positional pairing.
        """
        results: list[dict[str, Any]] = []
        for start in range(0, len(paths), _BATCH_SIZE):
            chunk = [str(p) for p in paths[start : start + _BATCH_SIZE]]
            dumps = list(self._helper.get_metadata(chunk))
            by_source = {d.get("SourceFile"): d for d in dumps}
            for path_str in chunk:
                results.append(by_source.get(path_str, {"ExifTool:Error": "no output"}))
        return results
