"""CLI tests: init, preserve gate, stage stubs, logging output."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archive_pipeline.catalog import LATEST_SCHEMA_VERSION, open_catalog, schema_version
from archive_pipeline.cli import EXIT_NOT_IMPLEMENTED, app
from archive_pipeline.fixtures.generator import generate_corpus

runner = CliRunner()


@pytest.fixture
def wt_root(tmp_path: Path) -> Path:
    return tmp_path / "worktree"


def _init(wt_root: Path) -> None:
    result = runner.invoke(app, ["--working-tree", str(wt_root), "init"])
    assert result.exit_code == 0, result.output


def test_init_creates_layout_config_and_catalog(wt_root: Path) -> None:
    _init(wt_root)
    for sub in ("archive", "quarantine", "review", "reports", "logs"):
        assert (wt_root / sub).is_dir()
    assert (wt_root / "config.toml").is_file()
    conn = open_catalog(wt_root / "catalog.db")
    assert schema_version(conn) == LATEST_SCHEMA_VERSION
    run = conn.execute("SELECT stage, status FROM run").fetchone()
    assert (run["stage"], run["status"]) == ("init", "ok")


def test_init_is_idempotent_and_keeps_config(wt_root: Path) -> None:
    _init(wt_root)
    config = wt_root / "config.toml"
    config.write_text(config.read_text() + "\n# user edit\n", encoding="utf-8")
    result = runner.invoke(app, ["--working-tree", str(wt_root), "init"])
    assert result.exit_code == 0
    assert "kept unchanged" in result.output
    assert "# user edit" in config.read_text()


def test_init_writes_jsonl_log(wt_root: Path) -> None:
    _init(wt_root)
    log_file = wt_root / "logs" / "archive.jsonl"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    assert record["stage"] == "init"
    assert record["event"] == "working tree initialized"
    assert record["schema_version"] == LATEST_SCHEMA_VERSION


def test_ingest_refuses_without_preserve_gate(wt_root: Path) -> None:
    _init(wt_root)
    result = runner.invoke(
        app,
        ["--working-tree", str(wt_root), "ingest", "--source", "LOCAL", "--root", str(wt_root)],
    )
    assert result.exit_code == 1
    assert "preserve.confirmed" in result.output


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")
def test_ingest_end_to_end(wt_root: Path, tmp_path: Path) -> None:
    _init(wt_root)
    config = wt_root / "config.toml"
    text = config.read_text(encoding="utf-8")
    text = text.replace("confirmed = false", "confirmed = true")
    text = text.replace("parallelism = 0", "parallelism = 1")
    config.write_text(text, encoding="utf-8")
    corpus = tmp_path / "corpus"
    generate_corpus(corpus, seed=0)
    result = runner.invoke(
        app,
        [
            "--working-tree", str(wt_root),
            "ingest", "--source", "LOCAL", "--root", str(corpus / "LOCAL"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Ingested LOCAL: 13 files on disk" in result.output
    conn = open_catalog(wt_root / "catalog.db")
    assert conn.execute("SELECT COUNT(*) FROM instance").fetchone()[0] == 13


def test_export_id_rejected_for_local(wt_root: Path) -> None:
    _init(wt_root)
    config = wt_root / "config.toml"
    config.write_text(
        config.read_text().replace("confirmed = false", "confirmed = true"), encoding="utf-8"
    )
    result = runner.invoke(
        app,
        [
            "--working-tree", str(wt_root),
            "ingest", "--source", "LOCAL", "--root", str(wt_root), "--export-id", "x",
        ],
    )
    assert result.exit_code == 1
    assert "--export-id" in result.output


def test_ingest_without_init_explains(wt_root: Path) -> None:
    result = runner.invoke(
        app,
        ["--working-tree", str(wt_root), "ingest", "--source", "LOCAL", "--root", str(wt_root)],
    )
    assert result.exit_code == 1
    assert "archive init" in result.output


@pytest.mark.parametrize(
    ("args", "milestone"),
    [
        (["review", "serve"], "M5"),
        (["dedup"], "M6"),
        (["materialize"], "M7"),
        (["materialize", "--execute"], "M7"),
        (["verify"], "M8"),
        (["report"], "M8"),
        (["maintain", "verify-checksums"], "M8"),
        (["maintain", "import", "--root", "."], "M8"),
    ],
)
def test_future_stages_are_named_stubs(wt_root: Path, args: list[str], milestone: str) -> None:
    result = runner.invoke(app, ["--working-tree", str(wt_root), *args])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert milestone in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "archive-pipeline" in result.output
