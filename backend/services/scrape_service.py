"""Scrape and scoring orchestration."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from typing import Optional

from fastapi import HTTPException

from core.config import PIPELINE_SCRIPT, RFJ_SCRIPT
from repositories.database import backup_db, get_latest_scrape_run
from models import ScrapeRunOut


def scrape_status(conn: sqlite3.Connection) -> Optional[ScrapeRunOut]:
    run = get_latest_scrape_run(conn)
    if run is None:
        return None
    return ScrapeRunOut.model_validate(run)


def trigger_scrape() -> dict:
    if not RFJ_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"RFJ loader not found at {RFJ_SCRIPT}")

    subprocess.Popen(
        [sys.executable, str(RFJ_SCRIPT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"status": "started", "source": "remotefirstjobs"}


def scoring_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN llm_score IS NOT NULL THEN 1 ELSE 0 END) AS scored,
          SUM(CASE WHEN llm_score IS NULL     THEN 1 ELSE 0 END) AS unscored
        FROM jobs
        WHERE dismissed = 0 AND expired = 0
        """
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "scored": row["scored"] or 0,
        "unscored": row["unscored"] or 0,
    }


def trigger_rescore(conn: sqlite3.Connection, rescore_all: bool) -> dict:
    if not PIPELINE_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"pipeline.py not found at {PIPELINE_SCRIPT}")

    cleared = 0
    backup_info: Optional[dict] = None
    if rescore_all:
        backup_info = backup_db(reason="rescore-all")
        cur = conn.execute(
            "UPDATE jobs SET llm_score = NULL, llm_reasoning = NULL WHERE dismissed = 0 AND expired = 0"
        )
        cleared = cur.rowcount
        conn.commit()

    subprocess.Popen(
        [sys.executable, str(PIPELINE_SCRIPT), "--score-only"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {
        "status": "started",
        "mode": "all" if rescore_all else "unscored_only",
        "cleared": cleared,
        "backup": backup_info,
    }
