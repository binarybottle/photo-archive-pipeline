"""Tests for structured JSONL logging."""

import json
import logging
from pathlib import Path

from archive_pipeline.logs import LOG_FILENAME, configure_logging


def _records(logs_dir: Path) -> list[dict[str, object]]:
    text = (logs_dir / LOG_FILENAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_jsonl_records_have_structure_and_extras(tmp_path: Path) -> None:
    log = configure_logging(tmp_path, stage="ingest", console=False)
    log.info("hashed batch", extra={"count": 128, "source": "LOCAL"})
    records = _records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "hashed batch"
    assert record["stage"] == "ingest"
    assert record["level"] == "INFO"
    assert record["count"] == 128
    assert record["source"] == "LOCAL"
    assert str(record["ts"]).endswith("+00:00")


def test_reconfigure_does_not_duplicate_handlers(tmp_path: Path) -> None:
    for _ in range(3):
        log = configure_logging(tmp_path, stage="init", console=False)
    log.info("once")
    assert len(_records(tmp_path)) == 1


def test_stage_updates_on_reconfigure(tmp_path: Path) -> None:
    configure_logging(tmp_path, stage="ingest", console=False).info("a")
    configure_logging(tmp_path, stage="dedup", console=False).info("b")
    stages = [r["stage"] for r in _records(tmp_path)]
    assert stages == ["ingest", "dedup"]


def test_child_loggers_inherit_configuration(tmp_path: Path) -> None:
    configure_logging(tmp_path, stage="ingest", console=False)
    logging.getLogger("archive_pipeline.hashing").info("child event")
    records = _records(tmp_path)
    assert records[-1]["logger"] == "archive_pipeline.hashing"
    assert records[-1]["stage"] == "ingest"
