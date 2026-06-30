"""
Push scraped jobs to the Cloudflare Worker /ingest endpoint.

This is what makes Phase 0 "additive only": the local SQLite pipeline
keeps working untouched, and ALSO pushes its scraped jobs to the central
D1 instance so we can validate the cloud architecture against real data
before any user-facing rewrite.

Used by:
  - pipeline.py --push-to-d1 flag (called by GitHub Actions cron)
  - Any future tool that wants to backfill historical jobs
"""

from __future__ import annotations

import json
import logging
import os
from typing import Iterable, Optional

import requests

logger = logging.getLogger("reverse-ats.d1-uploader")

# Tune these via env vars in CI; sensible defaults for ad-hoc local runs.
DEFAULT_BATCH_SIZE = 200
DEFAULT_TIMEOUT_S = 60


def push_jobs(
    jobs: Iterable[dict],
    *,
    ingest_url: Optional[str] = None,
    secret: Optional[str] = None,
    source: str = "manual",
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> dict:
    """
    POST scraped jobs to the Cloudflare Worker /ingest endpoint.

    Args:
        jobs: iterable of job dicts (matches the IngestJob TypeScript type).
        ingest_url: e.g. "https://reverse-ats-ingest.<account>.workers.dev/ingest".
                    Falls back to env CF_INGEST_URL.
        secret: bearer token shared with the Worker. Falls back to env
                CF_INGEST_SECRET.
        source: free-form label written to ingest_runs.source.
        batch_size: max jobs per HTTP request — keeps payloads under 1MB
                    on Cloudflare and avoids long timeouts.

    Returns:
        Aggregate stats: {sent, new, updated, errors}. Raises only on
        unrecoverable errors (auth failure, no URL configured) — per-batch
        failures are logged and counted in `errors`.
    """
    url = ingest_url or os.environ.get("CF_INGEST_URL")
    token = secret or os.environ.get("CF_INGEST_SECRET")
    if not url:
        raise RuntimeError(
            "CF_INGEST_URL not set. Either pass ingest_url= or export CF_INGEST_URL."
        )
    if not token:
        raise RuntimeError(
            "CF_INGEST_SECRET not set. Either pass secret= or export CF_INGEST_SECRET."
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    sent = 0
    new = 0
    updated = 0
    errors: list[str] = []

    batch: list[dict] = []
    for job in jobs:
        batch.append(_normalize_for_wire(job))
        if len(batch) >= batch_size:
            stats = _post_batch(url, headers, batch, source, timeout)
            sent += stats["sent"]
            new += stats["new"]
            updated += stats["updated"]
            errors.extend(stats["errors"])
            batch = []

    if batch:
        stats = _post_batch(url, headers, batch, source, timeout)
        sent += stats["sent"]
        new += stats["new"]
        updated += stats["updated"]
        errors.extend(stats["errors"])

    logger.info(
        "d1 ingest complete: sent=%d new=%d updated=%d errors=%d",
        sent, new, updated, len(errors),
    )
    return {"sent": sent, "new": new, "updated": updated, "errors": errors}


def push_company_stats(
    stats: list[dict],
    *,
    ingest_url: Optional[str] = None,
    secret: Optional[str] = None,
    source: str = "pipeline-stats",
    timeout: int = DEFAULT_TIMEOUT_S,
) -> dict:
    """POST per-company scrape outcomes to the Worker /ingest endpoint.

    The Worker writes each entry to `scrape_company_runs`, which feeds
    /admin/scrape-health. No jobs are upserted (sends `jobs: []`), and
    only one ingest_runs row is created — much cleaner than attaching
    stats to an arbitrary job-bearing batch.

    Backwards-compat: this is a separate call from push_jobs(). Workers
    that haven't deployed the migration just silently drop the field.
    """
    url = ingest_url or os.environ.get("CF_INGEST_URL")
    token = secret or os.environ.get("CF_INGEST_SECRET")
    if not url or not token:
        return {"ok": False, "error": "CF_INGEST_URL or CF_INGEST_SECRET not set"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"source": source, "jobs": [], "company_stats": stats}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        logger.info("d1 company_stats pushed: %d entries", len(stats))
        return {"ok": True, "sent": len(stats)}
    except requests.RequestException as exc:
        logger.warning("company_stats push failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ───── Helpers ──────────────────────────────────────────────────────────────

def _post_batch(
    url: str,
    headers: dict,
    batch: list[dict],
    source: str,
    timeout: int,
) -> dict:
    payload = {"source": source, "jobs": batch}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code == 401:
            raise RuntimeError(
                "Worker rejected ingest with 401 — check CF_INGEST_SECRET matches "
                "the value set via `wrangler secret put INGEST_SECRET`."
            )
        resp.raise_for_status()
        body = resp.json()
        return {
            "sent": len(batch),
            "new": int(body.get("new", 0)),
            "updated": int(body.get("updated", 0)),
            "errors": list(body.get("errors", [])),
        }
    except requests.RequestException as exc:
        logger.warning("ingest batch failed: %s", exc)
        return {
            "sent": 0,
            "new": 0,
            "updated": 0,
            "errors": [f"batch failed: {exc}"],
        }


def _normalize_for_wire(job: dict) -> dict:
    """Match the IngestJob TypeScript shape — only fields the Worker uses.
    Skips DB-internal columns (llm_score, dismissed, etc.) that Phase 0
    doesn't centralize."""
    return {
        "id": job["id"],
        "company": job["company"],
        "title": job["title"],
        "url": job["url"],
        "location": job.get("location"),
        "department": job.get("department"),
        "team": job.get("team"),
        "description_full": job.get("description_full"),
        "description_snippet": job.get("description_snippet"),
        "category": job.get("category"),
        "ats_type": job.get("ats_type"),
        "remote": bool(job.get("remote")),
        "first_seen_at": job.get("first_seen_at"),
        "last_seen_at": job.get("last_seen_at"),
        "posted_at": job.get("posted_at"),
        # Compensation + workplace classification (added 2026-04-29).
        # Populated by fetch_ashby (structured), fetch_lever (structured +
        # regex), fetch_greenhouse (regex). NULL when not disclosed.
        "employment_type": job.get("employment_type"),
        "workplace_type": job.get("workplace_type"),
        "salary_min": job.get("salary_min"),
        "salary_max": job.get("salary_max"),
        "salary_currency": job.get("salary_currency"),
        "comp_summary": job.get("comp_summary"),
    }
