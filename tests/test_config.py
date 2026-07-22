"""Tests for config loading: defaults, overrides, template sync, typo rejection."""

from pathlib import Path

import pytest

from archive_pipeline.config import (
    Config,
    ConfigError,
    default_config_toml,
    load_config,
    parse_config,
)


def test_defaults_match_spec() -> None:
    cfg = Config()
    assert cfg.preserve.confirmed is False
    assert cfg.space.margin_pct == 15.0
    assert cfg.space.recheck_gb == 25.0
    assert cfg.dedup.phash_threshold == 6
    assert cfg.dedup.dhash_threshold == 8
    assert cfg.dedup.mass_identical_n == 25
    assert cfg.dedup.guardrail_margin == 0.5
    assert cfg.policy.raw == "companion"
    assert cfg.policy.edited == "keep_both"
    assert len(cfg.dates.folder_patterns) >= 5


def test_template_round_trips_to_defaults(tmp_path: Path) -> None:
    """The config.toml written by `archive init` must equal the in-code defaults."""
    path = tmp_path / "config.toml"
    path.write_text(default_config_toml(), encoding="utf-8")
    assert load_config(path) == Config()


def test_overrides_applied(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[preserve]\nconfirmed = true\n[dedup]\nphash_threshold = 4\n'
        '[dates.camera_era]\n"PowerShot A5" = 1998\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.preserve.confirmed is True
    assert cfg.dedup.phash_threshold == 4
    assert cfg.dates.camera_era == {"PowerShot A5": 1998}
    assert cfg.space.margin_pct == 15.0  # untouched sections keep defaults


def test_unknown_section_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown section"):
        parse_config({"dedupe": {}})


def test_unknown_key_rejected() -> None:
    with pytest.raises(ConfigError, match=r"unknown key\(s\) in \[space\]"):
        parse_config({"space": {"margin_percent": 10}})


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="archive init"):
        load_config(tmp_path / "config.toml")


def test_invalid_toml_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[preserve\nconfirmed = ??", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)
