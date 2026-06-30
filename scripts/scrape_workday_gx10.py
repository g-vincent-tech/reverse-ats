#!/usr/bin/env python3
"""Workday-only scrape, run from a residential IP (GX10).

Why this exists
---------------
Workday tenants front their CXS API with Akamai bot protection. From
GitHub Actions runner IP ranges (Microsoft Azure datacenter blocks),
Akamai returns 200 OK with empty `jobPostings` — a soft block that
makes our standard 30-min CI scrape useless for Workday.

Run this script from a residential connection (GX10 cron) and the same
adapter pulls full results. Output is POSTed to the same `/ingest`
endpoint the GitHub Actions scraper uses, so D1 sees both sources as
one logical pipeline. The Worker upserts by job_id, so concurrent
ingests are safe.

Required env
------------
  CF_INGEST_URL     e.g. https://reverse-ats-ingest.aries-lao.workers.dev/ingest
  CF_INGEST_SECRET  same value set via `wrangler secret put INGEST_SECRET`

Optional env
------------
  REVERSE_ATS_LOG_LEVEL  defaults to INFO

Cron suggestion (deploy via safe-crontab):
  */30 * * * * cd /path/to/reverse-ats && \
    CF_INGEST_URL=… CF_INGEST_SECRET=… python3 scripts/scrape_workday_gx10.py \
    >> /mnt/crucial-x10/projects/reverse-ats/logs/workday_scrape.log 2>&1
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Reuse the existing scraper + uploader without touching them.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scraper"))
sys.path.insert(0, str(REPO_ROOT / "backend"))

from job_scraper import COMPANIES, scrape_company  # noqa: E402
from repositories.d1_uploader import push_jobs                   # noqa: E402
from repositories.database import job_id_hash                          # noqa: E402


def _setup_logging() -> logging.Logger:
    level = os.environ.get("REVERSE_ATS_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("reverse-ats.workday-gx10")


def main() -> int:
    log = _setup_logging()

    if not os.environ.get("CF_INGEST_URL") or not os.environ.get("CF_INGEST_SECRET"):
        log.error("CF_INGEST_URL and CF_INGEST_SECRET must be set; refusing to run")
        return 2

    workday_companies = [c for c in COMPANIES if c.get("ats") == "workday"]
    log.info("scraping %d Workday tenants from residential IP", len(workday_companies))

    started = datetime.now(timezone.utc)
    all_jobs: list[dict] = []
    errors: list[str] = []
    per_company_counts: dict[str, int] = {}

    for company in workday_companies:
        name = company["name"]
        t0 = time.time()
        jobs, _raw_count, error = scrape_company(company, extra_keywords=[], remote_only=False)
        elapsed = time.time() - t0

        if error:
            errors.append(f"{name}: {error}")
            log.warning("%s failed in %.1fs: %s", name, elapsed, error)
            continue

        # Match the field shape pipeline.py would have set; the Worker reads
        # id, category, and ats_type into the jobs row. Compute id with the
        # same hash function the local pipeline uses so a Workday job seen
        # by both lanes (e.g. a transient CI run) lands in one D1 row.
        for j in jobs:
            j["id"] = job_id_hash(j.get("company", ""), j.get("title", ""), j.get("url", ""))
            j["category"] = company["category"]
            j["ats_type"] = "workday"

        per_company_counts[name] = len(jobs)
        all_jobs.extend(jobs)
        log.info("%s: %d jobs (%.1fs)", name, len(jobs), elapsed)

    if not all_jobs:
        log.error("no Workday jobs scraped — refusing to ingest empty payload")
        return 1

    log.info("posting %d jobs to /ingest …", len(all_jobs))
    result = push_jobs(all_jobs, source="gx10-workday")
    log.info(
        "ingest complete: sent=%d new=%d updated=%d errors=%d",
        result.get("sent", 0),
        result.get("new", 0),
        result.get("updated", 0),
        len(result.get("errors", [])),
    )

    if result.get("errors"):
        for e in result["errors"][:5]:
            log.warning("ingest error: %s", e)

    elapsed_total = (datetime.now(timezone.utc) - started).total_seconds()
    log.info("run finished in %.1fs across %d tenants", elapsed_total, len(per_company_counts))
    return 0 if not errors else 0  # tenant-level errors are non-fatal


if __name__ == "__main__":
    raise SystemExit(main())
