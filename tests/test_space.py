"""Tests for the INV-9 disk-space preflight."""

import shutil
from pathlib import Path

import pytest

import archive_pipeline.space as space_mod
from archive_pipeline.space import SpaceError, preflight, require_space


def test_preflight_ok_for_zero_bytes(tmp_path: Path) -> None:
    check = preflight(tmp_path, 0, 15.0)
    assert check.ok
    assert check.bytes_required == 0


def test_margin_applied(tmp_path: Path) -> None:
    check = preflight(tmp_path, 1000, 15.0)
    assert check.bytes_required == 1150


def test_require_space_raises_when_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(
        space_mod.shutil, "disk_usage", lambda _: usage._replace(free=100)
    )
    with pytest.raises(SpaceError, match="not enough free space"):
        require_space(tmp_path, 1000, 15.0)
    assert require_space(tmp_path, 80, 15.0).ok
