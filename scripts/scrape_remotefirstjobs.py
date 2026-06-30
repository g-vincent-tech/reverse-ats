#!/usr/bin/env python3
"""Ingest curated remote jobs from the Remote First Jobs public API.

Why this source
---------------
The user wants jobs sourced from the companies on
https://remotefirstjobs.com/top-remote-companies — pulled from that curated
site rather than scraped straight off Greenhouse / Ashby / Workday. Remote First
Jobs exposes a free, no-key JSON API (`/api/search-jobs`) that returns those
companies' postings WITH full descriptions, pre-classified seniority + category,
salary, locations, and an apply link to the company. That's everything our
matcher needs, already curated to remote-first roles.

Each job is mapped to the standard ingest shape and POSTed to the same Worker
/ingest endpoint as every other lane (deduped by job_id), tagged
ats_type="remotefirstjobs". Attribution: postings keep their Remote First Jobs
URL (their terms ask that we credit + link back).

Required env
------------
  CF_INGEST_URL     e.g. https://reverse-ats-ingest.aries-lao.workers.dev/ingest
  CF_INGEST_SECRET  same value as the Worker INGEST_SECRET

Optional env
------------
  RFJ_PAGES              pages to pull, 0-4 (default "0,1,2,3,4" → up to 500 jobs)
  RFJ_CATEGORY           restrict to one category slug (e.g. software-development)
  REVERSE_ATS_LOG_LEVEL  default INFO

Cron (deploy via safe-crontab — see CLAUDE.md): hourly is plenty (24h API delay).
  17 * * * * cd /mnt/crucial-x10/projects/reverse-ats && CF_INGEST_URL=… \
    CF_INGEST_SECRET=… $HOME/bin/sentinel-track reverse-ats-rfj \
    .venv/bin/python scripts/scrape_remotefirstjobs.py \
    >> /mnt/crucial-x10/projects/reverse-ats/logs/remotefirstjobs.log 2>&1
"""

from __future__ import annotations

import html
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
import json

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scraper"))
sys.path.insert(0, str(REPO_ROOT / "backend"))

from repositories.d1_uploader import push_jobs  # noqa: E402
from repositories.database import job_id_hash         # noqa: E402

API = "https://remotefirstjobs.com/api/search-jobs"


def _setup_logging() -> logging.Logger:
    level = os.environ.get("REVERSE_ATS_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("reverse-ats.remotefirstjobs")


log = _setup_logging()

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n[ \t\n]*")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    # <br>/<li>/<p> → newlines so structure survives, then drop remaining tags.
    s = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h[1-6])\s*/?>", "\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    return _WS.sub("\n", s).strip()


def _fetch_page(page: int, category: str | None) -> list[dict]:
    url = f"{API}?page={page}" + (f"&category={category}" if category else "")
    req = urlrequest.Request(url, headers={"User-Agent": "Mozilla/5.0 (reverse-ats-gx10)"})
    with urlrequest.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return data.get("jobs", []) if isinstance(data, dict) else []


def _map(job: dict) -> dict | None:
    company = (job.get("company_name") or "").strip()
    title = (job.get("title") or "").strip()
    url = (job.get("url") or "").strip()
    if not company or not title or not url:
        return None
    locations = job.get("locations") or []
    location = ", ".join(locations[:3]) if isinstance(locations, list) else None
    smin = job.get("salary_min") or None
    smax = job.get("salary_max") or None
    return {
        "id": job_id_hash(company, title, url),
        "company": company,
        "title": title,
        "url": url,
        "location": location or "Remote",
        "description_full": _strip_html(job.get("description") or ""),
        "category": job.get("category") or None,
        "ats_type": "remotefirstjobs",
        "remote": True,
        "workplace_type": "Remote",
        "posted_at": job.get("published_at") or None,
        "salary_min": int(smin) if smin else None,
        "salary_max": int(smax) if smax else None,
        "salary_currency": "USD" if (smin or smax) else None,
    }


def main() -> int:
    pages = [int(p) for p in os.environ.get("RFJ_PAGES", "0,1,2,3,4").split(",") if p.strip().isdigit()]
    category = os.environ.get("RFJ_CATEGORY") or None
    t0 = time.time()

    seen: set[str] = set()
    jobs: list[dict] = []
    for p in pages:
        try:
            raw = _fetch_page(p, category)
        except (HTTPError, URLError) as e:
            log.warning("page %d fetch failed: %s", p, e)
            continue
        for j in raw:
            m = _map(j)
            if m and m["id"] not in seen:
                seen.add(m["id"])
                jobs.append(m)
        log.info("page %d: %d jobs (running total %d)", p, len(raw), len(jobs))
        time.sleep(0.5)

    if not jobs:
        log.warning("no jobs fetched — aborting")
        return 1

    with_desc = sum(1 for j in jobs if j["description_full"])
    log.info("mapped %d jobs (%d with descriptions); pushing…", len(jobs), with_desc)
    try:
        stats = push_jobs(jobs, source="remotefirstjobs")
    except RuntimeError as e:
        log.error("push failed: %s", e)
        return 1
    log.info(
        "done: sent=%s new=%s updated=%s errors=%s (%.1fs)",
        stats.get("sent"), stats.get("new"), stats.get("updated"), len(stats.get("errors", [])), time.time() - t0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
