"""Run log shared by every ingest script.

Every load writes one row to `ingest_runs`: which script, which file (and its
SHA-256, so you can prove exactly which version was loaded), which code version
(git commit), when, how many rows, and whether it succeeded. Data rows carry the
run_id, so any value can be traced back to the load that produced it.

The run row is written in its own transaction, so a failed load is still
recorded with status='failed' and the error message.
"""
from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

RUNS_DDL = """
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    script        VARCHAR(100) NOT NULL,
    source_file   VARCHAR(255) NOT NULL,
    file_sha256   CHAR(64)     NOT NULL,
    git_commit    VARCHAR(40)  NULL COMMENT 'code version that ran the load',
    started_at    DATETIME     NOT NULL,
    finished_at   DATETIME     NULL,
    status        VARCHAR(10)  NOT NULL COMMENT 'running | success | failed',
    rows_read     INT          NULL,
    rows_loaded   INT          NULL,
    rows_rejected INT          NULL,
    error         TEXT         NULL,
    KEY ix_runs_file (source_file, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def git_commit() -> str | None:
    """Short commit hash of the scripts folder, or None outside a git repo."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=Path(__file__).parent, capture_output=True, text=True,
                             timeout=5)
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def start_run(engine, script: str, source_path: str) -> int:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(RUNS_DDL))
        res = conn.execute(text(
            "INSERT INTO ingest_runs (script, source_file, file_sha256, git_commit, "
            "started_at, status) VALUES (:s, :f, :h, :g, :t, 'running')"),
            {"s": script, "f": Path(source_path).name, "h": file_sha256(source_path),
             "g": git_commit(), "t": _now()})
        return int(res.lastrowid)


def finish_run(engine, run_id: int, *, ok: bool, rows_read=None, rows_loaded=None,
               rows_rejected=None, error: str | None = None) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE ingest_runs SET finished_at=:t, status=:st, rows_read=:r, "
            "rows_loaded=:l, rows_rejected=:x, error=:e WHERE run_id=:id"),
            {"t": _now(), "st": "success" if ok else "failed", "r": rows_read,
             "l": rows_loaded, "x": rows_rejected, "e": (error or None) and error[:5000],
             "id": run_id})
