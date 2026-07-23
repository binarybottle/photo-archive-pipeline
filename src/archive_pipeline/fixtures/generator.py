"""Fixture generator v0: a deterministic synthetic corpus (spec section 9).

Builds a small LOCAL tree and a fake Google Takeout tree exercising the first
slice of the edge-case registry (spec section 11): dated/topical folders, exact
duplicates, scanner batches with shared dates, corrupt and zero-byte files,
non-media files, Takeout sidecar pathologies (truncation, ``(n)`` numbering,
``-edited`` pairs), and album duplication.

Determinism: pixel content derives from a seeded RNG and all dates are fixed, so
two runs with the same seed (and the same Pillow/exiftool versions) produce
byte-identical trees. EXIF is crafted via exiftool when available (spec: exiftool
is the only tool trusted to write metadata); without exiftool the corpus is still
generated, minus embedded EXIF. Video fixtures arrive with M2 (they need ffmpeg).

Usage:
    >>> from pathlib import Path
    >>> from archive_pipeline.fixtures.generator import generate_corpus
    >>> manifest = generate_corpus(Path("corpus"), seed=42)  # doctest: +SKIP
    >>> sorted(manifest.files)[:1]  # doctest: +SKIP
    ['LOCAL/1998/beach_001.jpg']
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

MANIFEST_NAME = "MANIFEST.json"

#: (relpath of media, exiftool tag -> value); applied only when exiftool exists.
_EXIF_PLAN: tuple[tuple[str, dict[str, str]], ...] = (
    # Trusted camera EXIF inside a year-precision folder (date rule R1 material).
    ("LOCAL/1998/beach_001.jpg",
     {"DateTimeOriginal": "1998:07:12 14:33:05", "Make": "Canon", "Model": "PowerShot A5"}),
    ("LOCAL/1998/beach_002.jpg",
     {"DateTimeOriginal": "1998:07:12 14:41:22", "Make": "Canon", "Model": "PowerShot A5"}),
    # Camera-default date (distrust heuristic) conflicting with a month folder.
    ("LOCAL/2003-07/park_001.jpg",
     {"DateTimeOriginal": "2000:01:01 00:00:00", "Make": "NoName", "Model": "DC-100"}),
    # Day-precision event folder with coherent EXIF.
    ("LOCAL/2010-06-15_wedding/wedding_001.jpg",
     {"DateTimeOriginal": "2010:06:15 18:02:10", "Make": "Google", "Model": "Pixel"}),
    ("LOCAL/2010-06-15_wedding/wedding_002.jpg",
     {"DateTimeOriginal": "2010:06:15 18:05:59", "Make": "Google", "Model": "Pixel"}),
    # Scanner batch: PNGs sharing one CreateDate, no DateTimeOriginal.
    ("LOCAL/scans/scan001.png", {"XMP:CreateDate": "2019:11:03 10:00:00"}),
    ("LOCAL/scans/scan002.png", {"XMP:CreateDate": "2019:11:03 10:00:00"}),
    ("LOCAL/scans/scan003.png", {"XMP:CreateDate": "2019:11:03 10:00:00"}),
    ("TAKEOUT/Google Photos/Photos from 2015/IMG_2015_001.jpg",
     {"DateTimeOriginal": "2015:04:18 09:30:00", "Make": "Google", "Model": "Nexus 5"}),
    # Google-edited version: re-encoded by Google, so maker notes gone and a
    # Google Software tag stamped (google_recompressed heuristic material).
    ("TAKEOUT/Google Photos/Photos from 2015/IMG_2015_003-edited.jpg",
     {"Software": "Google Photos 1.2"}),
)

#: JPEGs generated with distinct random pixel content (beyond the EXIF plan).
_PLAIN_JPEGS: tuple[str, ...] = (
    "LOCAL/1998/beach_003.jpg",  # no EXIF: folder date is the only candidate
    "TAKEOUT/Google Photos/Photos from 2015/"
    "a_very_long_filename_that_google_truncates_in_sidecars_2015.jpg",
    "TAKEOUT/Google Photos/Photos from 2015/IMG_2015_002.jpg",
    "TAKEOUT/Google Photos/Photos from 2015/IMG_2015_002(1).jpg",
    "TAKEOUT/Google Photos/Photos from 2015/IMG_2015_003.jpg",
)

#: (source relpath, destination relpath) byte-identical copies -> exact-dup clusters.
_EXACT_COPIES: tuple[tuple[str, str], ...] = (
    # Topical folder duplication in LOCAL (spec objective 5).
    ("LOCAL/1998/beach_001.jpg", "LOCAL/topical/vacations/beach_001.jpg"),
    # Same photo appears in a Takeout album folder (edge case 11).
    ("TAKEOUT/Google Photos/Photos from 2015/IMG_2015_001.jpg",
     "TAKEOUT/Google Photos/Vacation 2015/IMG_2015_001.jpg"),
)

#: Google-truncated sidecar name for the long filename (base cut, then ".json").
_LONG_NAME = "a_very_long_filename_that_google_truncates_in_sidecars_2015.jpg"
_TRUNCATED_SIDECAR = _LONG_NAME[:46] + ".json"


@dataclass(frozen=True)
class CorpusManifest:
    """What was generated: seed, per-file SHA-256, and whether EXIF was embedded."""

    seed: int
    exif_written: bool
    files: dict[str, str]

    @property
    def count(self) -> int:
        return len(self.files)


def _make_image(rng: random.Random, width: int = 96, height: int = 72) -> Image.Image:
    """Create a small deterministic test image from the given RNG state.

    Usage:
        >>> img = _make_image(random.Random(1))
        >>> img.size
        (96, 72)
    """
    base = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    img = Image.new("RGB", (width, height), base)
    for _ in range(40):
        x0 = rng.randrange(width)
        y0 = rng.randrange(height)
        x1 = min(width, x0 + rng.randrange(1, 24))
        y1 = min(height, y0 + rng.randrange(1, 24))
        color = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        img.paste(color, (x0, y0, x1, y1))
    return img


def _sidecar_json(
    title: str,
    taken_epoch: int,
    *,
    description: str = "",
    lat: float = 0.0,
    lon: float = 0.0,
) -> str:
    """Render a Takeout-style sidecar JSON body with fixed, deterministic content."""
    payload = {
        "title": title,
        "description": description,
        "photoTakenTime": {"timestamp": str(taken_epoch)},
        "creationTime": {"timestamp": str(taken_epoch + 86400 * 30)},
        "geoData": {"latitude": lat, "longitude": lon, "altitude": 0.0},
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _write_sidecars(dest: Path) -> None:
    """Write Takeout JSON sidecars, including the pathological names."""
    photos = dest / "TAKEOUT/Google Photos/Photos from 2015"
    taken = 1429349400  # 2015-04-18T09:30:00Z
    sidecars: dict[str, str] = {
        # Modern exact-match style.
        "IMG_2015_001.jpg.supplemental-metadata.json": _sidecar_json(
            "IMG_2015_001.jpg", taken, description="Lake hike", lat=44.06, lon=-71.29
        ),
        # Truncated base name (edge case 1).
        _TRUNCATED_SIDECAR: _sidecar_json(_LONG_NAME, taken + 3600),
        # Numbered-duplicate inconsistency: (1) after the extension (edge case 1).
        "IMG_2015_002.jpg.json": _sidecar_json("IMG_2015_002.jpg", taken + 7200),
        "IMG_2015_002.jpg(1).json": _sidecar_json("IMG_2015_002.jpg", taken + 7300),
        # Shared sidecar for the -edited pair (edge case 2).
        "IMG_2015_003.jpg.json": _sidecar_json(
            "IMG_2015_003.jpg", taken + 9000, description="Sunset, edited later"
        ),
    }
    for name, body in sidecars.items():
        (photos / name).write_text(body, encoding="utf-8")
    album = dest / "TAKEOUT/Google Photos/Vacation 2015"
    album_meta = {"title": "Vacation 2015", "description": "", "access": "protected"}
    (album / "metadata.json").write_text(
        json.dumps(album_meta, indent=2, sort_keys=True), encoding="utf-8"
    )


def _write_exif(dest: Path, exiftool: str) -> None:
    """Embed the planned EXIF via exiftool (the only tool allowed to write metadata)."""
    for rel_path, tags in _EXIF_PLAN:
        args = [exiftool, "-overwrite_original", "-q", "-q"]
        args += [f"-{tag}={value}" for tag, value in tags.items()]
        args.append(str(dest / rel_path))
        subprocess.run(args, check=True, capture_output=True)


def _sha256(path: Path) -> str:
    """Streamed SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 16):
            digest.update(chunk)
    return digest.hexdigest()


def generate_corpus(dest: Path, seed: int = 0, *, write_exif: bool | None = None) -> CorpusManifest:
    """Generate the v0 fixture corpus under ``dest``; return its manifest.

    ``dest`` must not already contain a corpus (existing files are never
    overwritten silently). ``write_exif=None`` auto-detects exiftool. A
    ``MANIFEST.json`` with per-file SHA-256 hashes is written into ``dest``.

    Usage:
        >>> manifest = generate_corpus(Path("corpus"), seed=42)  # doctest: +SKIP
        >>> manifest.count > 20  # doctest: +SKIP
        True
    """
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"fixture destination is not empty: {dest}")
    rng = random.Random(seed)

    for rel_path, _tags in _EXIF_PLAN:
        path = dest / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        img = _make_image(rng)
        img.save(path, format="PNG" if path.suffix == ".png" else "JPEG", quality=88)
    for rel_path in _PLAIN_JPEGS:
        path = dest / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _make_image(rng).save(path, format="JPEG", quality=88)

    exiftool = shutil.which("exiftool") if write_exif in (None, True) else None
    if write_exif is True and exiftool is None:
        raise RuntimeError("write_exif=True but exiftool was not found on PATH")
    if exiftool is not None:
        _write_exif(dest, exiftool)

    for src_rel, dst_rel in _EXACT_COPIES:
        dst = dest / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dest / src_rel, dst)

    misc = dest / "LOCAL/misc"
    misc.mkdir(parents=True, exist_ok=True)
    (misc / "notes.txt").write_text("Shopping list, not a photo.\n", encoding="utf-8")
    (misc / "empty.jpg").write_bytes(b"")  # zero-byte file (edge case 9)
    intact = (dest / "LOCAL/1998/beach_002.jpg").read_bytes()
    (misc / "broken.jpg").write_bytes(intact[: max(1, len(intact) * 2 // 5)])  # truncated

    _write_sidecars(dest)

    files = {
        str(p.relative_to(dest)): _sha256(p)
        for p in sorted(dest.rglob("*"))
        if p.is_file() and p.name != MANIFEST_NAME
    }
    manifest = CorpusManifest(seed=seed, exif_written=exiftool is not None, files=files)
    (dest / MANIFEST_NAME).write_text(
        json.dumps(
            {"seed": seed, "exif_written": manifest.exif_written, "files": files},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return manifest
