"""Content hashing: streamed SHA-256 and perceptual image hashes.

Perceptual hashes are computed on an EXIF-orientation-normalized, downscaled
decode (spec Stage 1). HEIC decodes via pillow-heif. Undecodable images yield
``corrupt=True`` with NULL hashes; they still obey the conservation law because
their SHA-256 is always computed. Functions here are process-pool worker safe.

Usage:
    >>> from pathlib import Path
    >>> from archive_pipeline.hashing import sha256_file, image_facts
    >>> sha256_file(Path("photo.jpg"))  # doctest: +SKIP
    'a1b2...'
    >>> image_facts(Path("photo.jpg")).phash  # doctest: +SKIP
    'c4d5...'
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import imagehash
import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

_CHUNK_BYTES = 1 << 20
_DECODE_MAX_EDGE = 512


def sha256_file(path: Path) -> str:
    """Streamed SHA-256 of a file's bytes.

    Usage:
        >>> import tempfile
        >>> p = Path(tempfile.mkdtemp()) / "x"
        >>> _ = p.write_bytes(b"")
        >>> sha256_file(p)[:12]
        'e3b0c44298fc'
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ImageFacts:
    """Perceptual hashes and dimensions, or ``corrupt`` when undecodable."""

    phash: str | None
    dhash: str | None
    width: int | None
    height: int | None
    corrupt: bool


def image_facts(path: Path) -> ImageFacts:
    """Decode an image and compute pHash/dHash on a normalized downscaled copy.

    Dimensions are the orientation-corrected (display) dimensions.

    Usage:
        >>> facts = image_facts(Path("photo.heic"))  # doctest: +SKIP
        >>> facts.corrupt  # doctest: +SKIP
        False
    """
    try:
        with Image.open(path) as img:
            oriented = ImageOps.exif_transpose(img)
            width, height = oriented.size
            oriented.thumbnail((_DECODE_MAX_EDGE, _DECODE_MAX_EDGE))
            normalized = oriented.convert("RGB") if oriented.mode != "RGB" else oriented
            phash = str(imagehash.phash(normalized))
            dhash = str(imagehash.dhash(normalized))
    except Exception:
        return ImageFacts(phash=None, dhash=None, width=None, height=None, corrupt=True)
    return ImageFacts(phash=phash, dhash=dhash, width=width, height=height, corrupt=False)


@dataclass(frozen=True)
class FileFacts:
    """Per-file worker output: content hash plus image facts when applicable."""

    sha256: str
    image: ImageFacts | None


def process_file(path_str: str, is_image: bool) -> FileFacts:
    """Process-pool worker: hash one file, decoding it only if it is an image.

    Takes a string (not a Path) so arguments pickle cheaply.

    Usage:
        >>> facts = process_file("/photos/a/b.jpg", True)  # doctest: +SKIP
        >>> facts.sha256  # doctest: +SKIP
        'a1b2...'
    """
    path = Path(path_str)
    sha = sha256_file(path)
    image = image_facts(path) if is_image else None
    return FileFacts(sha256=sha, image=image)
