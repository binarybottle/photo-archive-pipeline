"""Configuration loading for the archive pipeline (spec section 12).

All tunables live in the working tree's ``config.toml``. Unknown sections or keys
are rejected loudly so typos cannot silently fall back to defaults.

Usage:
    >>> from pathlib import Path
    >>> from archive_pipeline.config import Config, load_config
    >>> cfg = Config()
    >>> cfg.preserve.confirmed
    False
    >>> cfg.dedup.phash_threshold
    6
    >>> cfg = load_config(Path("config.toml"))  # doctest: +SKIP
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Folder-name date patterns tried against each path component, deepest match wins
#: (spec Stage 3). Extendable via ``dates.folder_patterns`` in config.toml.
DEFAULT_FOLDER_DATE_PATTERNS: tuple[str, ...] = (
    r"^(?P<year>(19|20)\d{2})$",
    r"^(?P<year>(19|20)\d{2})-(?P<month>0[1-9]|1[0-2])$",
    r"^(?P<year>(19|20)\d{2})-(?P<month>0[1-9]|1[0-2])-(?P<day>0[1-9]|[12]\d|3[01])$",
    r"^(?P<year>(19|20)\d{2})[_ -](?P<event>\D.*)$",
    r"^(?P<year>(19|20)\d{2})(?P<month>0[1-9]|1[0-2])(?P<day>0[1-9]|[12]\d|3[01])[_ -].*$",
)


class ConfigError(Exception):
    """Raised for a missing config file or unknown/invalid keys."""


@dataclass(frozen=True)
class PreserveConfig:
    """Stage 0 gate: the user asserts a verbatim backup of all sources exists."""

    confirmed: bool = False


@dataclass(frozen=True)
class SpaceConfig:
    """Disk-space preflight (INV-9)."""

    margin_pct: float = 15.0
    recheck_gb: float = 25.0


@dataclass(frozen=True)
class DedupConfig:
    """Near-duplicate thresholds and guardrails (spec Stage 5)."""

    phash_threshold: int = 6
    dhash_threshold: int = 8
    review_band: int = 4
    guardrail_margin: float = 0.5
    mass_identical_n: int = 25


@dataclass(frozen=True)
class PolicyConfig:
    """User-decided dispositions (spec sections 12 and 13)."""

    raw: str = "companion"  # companion | prefer_raw
    edited: str = "keep_both"  # keep_both | quarantine_edits
    undated_placement: str = "undated"


@dataclass(frozen=True)
class DatesConfig:
    """Date-candidate extraction settings (spec Stage 3)."""

    folder_patterns: tuple[str, ...] = DEFAULT_FOLDER_DATE_PATTERNS
    #: Camera model -> first plausible year; EXIF dates before it are distrusted.
    camera_era: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class KeywordsConfig:
    """Keyword mapping settings (spec Stage 6)."""

    hierarchy_separator: str = "/"


@dataclass(frozen=True)
class RuntimeConfig:
    """Execution settings. ``parallelism`` of 0 means use all CPUs."""

    parallelism: int = 0


@dataclass(frozen=True)
class ProvenanceConfig:
    """Stage 2b overrides: LOCAL path prefixes whose classification the user fixes."""

    curated_overrides: tuple[str, ...] = ()
    takeout_derived_overrides: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    """Complete pipeline configuration with spec-stated defaults."""

    preserve: PreserveConfig = field(default_factory=PreserveConfig)
    space: SpaceConfig = field(default_factory=SpaceConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    dates: DatesConfig = field(default_factory=DatesConfig)
    keywords: KeywordsConfig = field(default_factory=KeywordsConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    provenance: ProvenanceConfig = field(default_factory=ProvenanceConfig)


def _build_section(cls: type[Any], name: str, raw: dict[str, Any]) -> Any:
    """Build one config dataclass from a TOML table, rejecting unknown keys.

    Usage:
        >>> _build_section(SpaceConfig, "space", {"margin_pct": 20})
        SpaceConfig(margin_pct=20, recheck_gb=25.0)
    """
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"unknown key(s) in [{name}]: {', '.join(sorted(unknown))}")
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        kwargs[key] = tuple(value) if isinstance(value, list) else value
    return cls(**kwargs)


def parse_config(data: dict[str, Any]) -> Config:
    """Build a :class:`Config` from parsed TOML data, rejecting unknown sections.

    Usage:
        >>> parse_config({"space": {"margin_pct": 20}}).space.margin_pct
        20
    """
    sections = {f.name: f for f in dataclasses.fields(Config)}
    unknown = set(data) - set(sections)
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")
    kwargs: dict[str, Any] = {}
    for name, fld in sections.items():
        if name in data:
            kwargs[name] = _build_section(fld.default_factory, name, data[name])  # type: ignore[arg-type]
    return Config(**kwargs)


def load_config(path: Path) -> Config:
    """Load ``config.toml``; a missing file is an error (run ``archive init`` first).

    Usage:
        >>> load_config(Path("/nonexistent/config.toml"))  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ConfigError: ...
    """
    if not path.is_file():
        raise ConfigError(f"config file not found: {path} (run `archive init` first)")
    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    return parse_config(data)


def default_config_toml() -> str:
    """Return the commented ``config.toml`` template written by ``archive init``.

    The template's values equal the :class:`Config` defaults; a test enforces this.

    Usage:
        >>> "preserve" in default_config_toml()
        True
    """
    patterns = "\n".join(f"    '{p}'," for p in DEFAULT_FOLDER_DATE_PATTERNS)
    return f"""\
# Photo archive pipeline configuration (spec section 12).
# Every tunable lives here; unknown keys are rejected to catch typos.

[preserve]
# Stage 0 gate. Set to true ONLY after you have made a verbatim backup of LOCAL
# and all Takeout archives on a separate physical disk. `archive ingest` refuses
# to run while this is false.
confirmed = false

[space]
# Disk-space preflight (INV-9).
margin_pct = 15.0      # refuse to start unless free space exceeds estimate by this %
recheck_gb = 25.0      # re-check free space every N gigabytes written

[dedup]
phash_threshold = 6    # T1: pHash Hamming distance for auto-clustering
dhash_threshold = 8    # T2: dHash confirmation distance
review_band = 4        # distances in (T1, T1+review_band] queue for review
guardrail_margin = 0.5 # top-two winner scores closer than this queue for review
mass_identical_n = 25  # >N identical timestamps in one folder distrusts EXIF

[policy]
raw = "companion"            # companion | prefer_raw
edited = "keep_both"         # keep_both | quarantine_edits
undated_placement = "undated"  # archive/<this>/ for unresolvable dates

[dates]
# Regexes tried against each path component; deepest match wins (spec Stage 3).
folder_patterns = [
{patterns}
]

# Camera model -> first plausible year; earlier EXIF dates are distrusted.
[dates.camera_era]

[keywords]
hierarchy_separator = "/"

[runtime]
parallelism = 0        # 0 = use all CPUs

[provenance]
# Stage 2b overrides: LOCAL path prefixes forced to a classification.
curated_overrides = []
takeout_derived_overrides = []
"""
