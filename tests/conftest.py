"""Shared test fixtures: a fully ingested pipeline environment over the corpus."""

import logging
import shutil
from pathlib import Path

from archive_pipeline.catalog import open_catalog
from archive_pipeline.config import load_config
from archive_pipeline.fixtures.generator import generate_corpus
from archive_pipeline.ingest import ingest_source
from archive_pipeline.runs import record_run
from archive_pipeline.takeout import normalize_takeout
from archive_pipeline.workingtree import init_working_tree

LOG = logging.getLogger("archive_pipeline.test")


class PipelineEnv:
    """Working tree ingested from the v0 corpus plus a merged prior-Takeout
    extraction inside LOCAL (``google-import/Google Photos/...``)."""

    def __init__(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        generate_corpus(corpus, seed=0)
        self.local_root = tmp_path / "LOCAL"
        shutil.copytree(corpus / "LOCAL", self.local_root)
        shutil.copytree(
            corpus / "TAKEOUT" / "Google Photos",
            self.local_root / "google-import" / "Google Photos",
        )
        self.wt, _ = init_working_tree(tmp_path / "worktree")
        text = self.wt.config_path.read_text(encoding="utf-8")
        text = text.replace("confirmed = false", "confirmed = true")
        text = text.replace("parallelism = 0", "parallelism = 1")
        self.wt.config_path.write_text(text, encoding="utf-8")
        self.conn = open_catalog(self.wt.catalog_path)
        self.cfg = load_config(self.wt.config_path)
        with record_run(self.conn, "ingest") as run_id:
            ingest_source(self.conn, self.cfg, "LOCAL", self.local_root, run_id, LOG)
        with record_run(self.conn, "ingest") as run_id:
            ingest_source(
                self.conn, self.cfg, "TAKEOUT:t2015", corpus / "TAKEOUT", run_id, LOG
            )
        normalize_takeout(self.conn, self.wt, LOG)

    def reload_config(self) -> None:
        self.cfg = load_config(self.wt.config_path)
