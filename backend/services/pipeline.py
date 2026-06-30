#!/usr/bin/env python3
"""
Reverse ATS Pipeline — Daily scrape, deduplicate, score, and store.

Wraps the existing job_scraper.py and adds:
1. SQLite persistence (via db.py)
2. Deduplication (hash-based — jobs are keyed by a stable ID derived from url/title/company)
3. LLM scoring (via scorer.py)
4. Scrape run audit logging

Usage:
    python3 pipeline.py                        # Full run (all categories, all companies)
    python3 pipeline.py --skip-score           # Scrape only, skip LLM scoring
    python3 pipeline.py --score-only           # Re-score existing unscored jobs, no scraping
    python3 pipeline.py --category fintech     # Only scrape one category
    python3 pipeline.py --no-remote-filter     # Include non-remote jobs
    python3 pipeline.py --db-path /tmp/ats.db  # Override database path
    python3 pipeline.py --inference-url http://gx10:8080/v1/chat/completions
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: add the existing scraper directory to sys.path before local imports
# ---------------------------------------------------------------------------

# Search multiple locations for job_scraper.py:
BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
_candidates = [
    REPO_ROOT / "scraper",
    REPO_ROOT.parent / "infrastructure" / "scripts",
]
for _candidate in _candidates:
    SCRAPER_DIR = str(_candidate)
    if _candidate.exists() and SCRAPER_DIR not in sys.path:
        sys.path.insert(0, SCRAPER_DIR)
        break

# Scraper exports we rely on
from job_scraper import COMPANIES, CATEGORY_LABELS, scrape_company, is_active  # noqa: E402

# Local backend imports
from repositories.database import (  # noqa: E402
    get_connection,
    init_db,
    upsert_job,
    mark_expired,
    create_scrape_run,
    complete_scrape_run,
    get_profile,
    get_llm_settings,
)
from services.scorer import check_inference_health, score_job  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("reverse-ats.pipeline")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RATE_LIMIT_SLEEP = 1.0   # seconds between company scrapes (be polite to ATS endpoints)
SCORE_SLEEP = 0.5        # seconds between LLM calls (don't overwhelm the local gateway)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_pipeline(
    categories: list[str] = None,
    remote_only: bool = True,
    skip_score: bool = False,
    score_only: bool = False,
    db_path: str = None,
    inference_url: str = None,
    push_to_d1: bool = False,
) -> dict:
    """
    Main pipeline: scrape → deduplicate → score → store.

    Args:
        categories:     Limit scraping to these category keys (e.g. ["fintech", "ai_tech"]).
                        None means all categories.
        remote_only:    When True, skip jobs without a remote location signal.
        skip_score:     Scrape and store but do not call the LLM scorer.
        score_only:     Skip scraping entirely and just score unscored jobs in the DB.
        db_path:        Override the SQLite database path (uses db.py default if None).
        inference_url:  Override the LLM gateway URL (uses scorer.py default if None).

    Returns:
        Stats dict with totals for logging/audit.
    """
    conn = get_connection(db_path)
    init_db(db_path)

    # Load LLM settings from DB; CLI --inference-url overrides the DB value
    llm_settings = get_llm_settings(conn)
    if inference_url:
        llm_settings["api_url"] = inference_url

    # Load the candidate profile (used to personalise LLM scoring)
    profile = get_profile(conn)

    if score_only:
        _score_unscored(conn, profile, llm_settings)
        conn.close()
        return {}

    # Try loading companies from DB first, fall back to hardcoded list
    from repositories.database import get_companies as db_get_companies
    db_companies = db_get_companies(conn, enabled_only=True)
    if db_companies:
        # Convert DB rows to the format scrape_company expects
        target_companies = []
        for c in db_companies:
            entry = {"name": c["name"], "ats": c["ats"], "slug": c["slug"], "category": c["category"]}
            if c.get("careers_url"):
                entry["careers_url"] = c["careers_url"]
            if c.get("workday_url"):
                entry["workday_url"] = c["workday_url"]
            if categories and c["category"] not in categories:
                continue
            target_companies.append(entry)
    else:
        target_companies = [c for c in COMPANIES if not categories or c["category"] in categories]

    # Skip Workday tenants when running in an environment that's soft-blocked
    # by Akamai (GitHub Actions IP ranges return 200 OK with empty postings).
    # The companion script `scripts/scrape_workday_gx10.py` handles the
    # Workday lane from a residential IP and POSTs to the same /ingest.
    if os.environ.get("SKIP_WORKDAY", "").lower() in ("1", "true", "yes"):
        skipped = sum(1 for c in target_companies if c.get("ats") == "workday")
        target_companies = [c for c in target_companies if c.get("ats") != "workday"]
        if skipped:
            logger.info(
                "SKIP_WORKDAY=true — skipping %d Workday tenant(s); "
                "they are scraped from GX10 (residential IP) instead",
                skipped,
            )

    # Filter quarantined slugs (audit-flagged broken ATS endpoints). This runs
    # against whichever source populated target_companies — DB or hardcoded —
    # so DB-managed companies that match a known-broken (ats, slug) pair are
    # also skipped without needing a separate DB migration.
    inactive_count = sum(1 for c in target_companies if not is_active(c))
    if inactive_count:
        target_companies = [c for c in target_companies if is_active(c)]
        logger.info(
            "Skipped %d company(ies) marked inactive (registry audit). "
            "See INACTIVE_SLUGS in scraper/job_scraper.py to reactivate.",
            inactive_count,
        )

    _print_header(len(target_companies))

    stats: dict = {
        "total_fetched": 0,
        "new_jobs": 0,
        "updated_jobs": 0,
        "expired_jobs": 0,
        "llm_scored": 0,
        "errors": [],
    }

    # Track every job_id we see this run so we can expire stale ones
    seen_ids: set[str] = set()
    # Collect jobs that were new/updated so we can score them after the scrape loop
    jobs_to_score: list[dict] = []
    # Collect EVERY upserted job (new + updated) for the optional D1 push.
    # Phase 0: this is what feeds the centralized cloud architecture without
    # changing anything about the local single-tenant flow.
    jobs_for_d1: list[dict] = []
    # Per-company scrape outcomes, pushed to D1 in a single stats-only call
    # at the end of the run. Powers /admin/scrape-health on the Worker side.
    company_stats: list[dict] = []

    # Create the audit record before we start scraping
    run_id = create_scrape_run(conn)

    # ------------------------------------------------------------------
    # Scrape loop
    # ------------------------------------------------------------------
    for i, company in enumerate(target_companies, 1):
        name = company["name"]
        ats = company.get("ats", "unknown").upper()
        label = f"[{i:02d}/{len(target_companies):02d}] {name:<22} ({ats})"
        print(f"  {label}", end=" ... ", flush=True)

        jobs, raw_count, error = scrape_company(company, extra_keywords=[], remote_only=remote_only)

        company_stats.append({
            "company": company.get("name", ""),
            "ats": company.get("ats", ""),
            "slug": company.get("slug", ""),
            "raw_count": raw_count,
            "filtered_count": len(jobs),
            "error": error,
        })

        if error:
            msg = f"{name}: {error}"
            stats["errors"].append(msg)
            print(f"ERROR: {error}")
            logger.warning("Scrape error — %s", msg)
            continue

        count_new = 0
        count_updated = 0

        for raw_job in jobs:
            # Skip placeholder / custom redirect entries the scraper emits
            if raw_job.get("_custom"):
                continue

            # Annotate with category and ATS type before storage
            raw_job["category"] = company.get("category", "")
            raw_job["ats_type"] = company.get("ats", "")
            # Map scraper's "score" field to db's "keyword_score"
            if "score" in raw_job and "keyword_score" not in raw_job:
                raw_job["keyword_score"] = raw_job["score"]

            job_id, is_new = upsert_job(conn, raw_job)
            seen_ids.add(job_id)

            if is_new:
                count_new += 1
                # Keep a copy of the job dict for scoring later
                jobs_to_score.append({**raw_job, "id": job_id})
            else:
                count_updated += 1

            # Always queue for D1 (Worker upserts so duplicates are fine)
            if push_to_d1:
                jobs_for_d1.append({**raw_job, "id": job_id})

        stats["total_fetched"] += len(jobs)
        stats["new_jobs"] += count_new
        stats["updated_jobs"] += count_updated

        print(f"{len(jobs)} jobs ({count_new} new, {count_updated} updated)")

        # Rate-limit between companies to avoid hammering ATS APIs.
        # Skip the sleep for custom scrapers — they're usually a single HTML fetch.
        if company.get("ats") != "custom":
            time.sleep(RATE_LIMIT_SLEEP)

    # ------------------------------------------------------------------
    # Mark jobs not seen this run as expired
    # ------------------------------------------------------------------
    expired_count = mark_expired(conn, seen_ids)
    stats["expired_jobs"] = expired_count
    if expired_count:
        logger.info("Marked %d jobs as expired (no longer on ATS boards)", expired_count)

    # ------------------------------------------------------------------
    # Optional: push every scraped job to the centralized D1 instance.
    # Phase 0 of the cloud rewrite — additive only, doesn't affect the
    # local SQLite app at all. Set CF_INGEST_URL + CF_INGEST_SECRET in env.
    # ------------------------------------------------------------------
    if push_to_d1 and jobs_for_d1:
        try:
            from repositories.d1_uploader import push_jobs as _push_jobs_to_d1
            d1_stats = _push_jobs_to_d1(jobs_for_d1, source="pipeline")
            stats["d1_pushed"] = d1_stats["sent"]
            stats["d1_new"] = d1_stats["new"]
            stats["d1_updated"] = d1_stats["updated"]
            if d1_stats["errors"]:
                logger.warning("D1 push had %d errors", len(d1_stats["errors"]))
        except Exception as exc:
            # D1 push failures must NEVER break the local pipeline — they're
            # purely an additive observability stream during Phase 0.
            logger.warning("D1 push failed (local pipeline unaffected): %s", exc)
            stats["errors"].append(f"d1_push: {exc}")

    # Push per-company scrape outcomes to D1 so /admin/scrape-health can
    # flag dead slugs without operator intervention. Wrapped in the same
    # never-fail-the-pipeline guard as the jobs push above.
    if push_to_d1 and company_stats:
        try:
            from repositories.d1_uploader import push_company_stats as _push_company_stats
            _push_company_stats(company_stats, source="pipeline-stats")
        except Exception as exc:
            logger.warning("D1 company_stats push failed (pipeline unaffected): %s", exc)
            stats["errors"].append(f"d1_company_stats: {exc}")

    # ------------------------------------------------------------------
    # LLM scoring for new jobs
    # ------------------------------------------------------------------
    if not skip_score and jobs_to_score:
        stats["llm_scored"] = _score_jobs(conn, jobs_to_score, profile, llm_settings)
    elif skip_score:
        logger.info("Scoring skipped (--skip-score)")

    # ------------------------------------------------------------------
    # Finalise audit record
    # ------------------------------------------------------------------
    complete_scrape_run(conn, run_id, stats)
    conn.close()

    _print_summary(stats)
    return stats


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _score_jobs(conn, jobs: list[dict], profile: dict, llm_settings: dict) -> int:
    """
    Score each job in `jobs` via the LLM, writing results back to the DB.

    Returns the number of jobs successfully scored (keyword fallback counts too).
    """
    # Report gateway status once before the loop so the operator knows what mode we're in
    health = check_inference_health(llm_settings)
    if health.get("healthy"):
        logger.info("LLM provider UP (%s) — scoring active", health.get("provider"))
    else:
        logger.warning("LLM provider issue: %s — keyword fallback may be used", health.get("message"))

    resume = profile.get("resume_text") if profile else None
    targets = _json_load(profile.get("target_roles")) if profile else None
    musts = _json_load(profile.get("must_have_skills")) if profile else None
    nices = _json_load(profile.get("nice_to_have_skills")) if profile else None

    # Years-weighted skill profile from the inventory — so scoring PRIORITIZES the
    # skills the candidate has the MOST experience in (their strongest areas).
    try:
        from repositories.database import get_inventory
        inv = get_inventory(conn)
    except Exception:
        inv = None
    if inv and inv.get("skills"):
        groups = sorted(inv["skills"], key=lambda g: (g.get("years_num") or 0), reverse=True)
        lines, inv_keywords = [], []
        for g in groups:
            yl = g.get("years_label") or (f"{g.get('years_num')} yrs" if g.get("years_num") else "")
            kws = ", ".join(g.get("keywords") or [])
            lines.append(f"- {g.get('name')} ({yl}): {kws}")
            inv_keywords.extend(g.get("keywords") or [])
        depth = ("CANDIDATE SKILL DEPTH — strongest first, by years of experience. "
                 "Weight the top (most-years) areas most when judging fit:\n" + "\n".join(lines))
        resume = (depth + "\n\n" + (resume or "")).strip()
        if inv_keywords:
            musts = inv_keywords[:40]  # the strongest-skill keywords drive the match

    scored = 0
    total = len(jobs)

    for i, job in enumerate(jobs, 1):
        title_display = job.get("title", "")[:50]
        company_display = job.get("company", "")
        print(f"  [SCORE {i:03d}/{total:03d}] {company_display}: {title_display}", end=" → ", flush=True)

        description = (
            job.get("description_snippet") or job.get("description_full") or ""
        )

        try:
            result = score_job(
                title=job.get("title", ""),
                company=job.get("company", ""),
                location=job.get("location", ""),
                department=job.get("department", ""),
                description=description,
                resume_text=resume,
                target_roles=targets,
                must_have_skills=musts,
                nice_to_have_skills=nices,
                settings=llm_settings,
            )
        except Exception as exc:
            # score_job has its own internal exception handling, but just in case
            logger.error("Unexpected scoring error for job %s: %s", job.get("id"), exc)
            print("ERROR (skipped)")
            continue

        # Persist the score and reasoning to the DB
        try:
            conn.execute(
                "UPDATE jobs SET llm_score = ?, llm_reasoning = ? WHERE id = ?",
                (
                    result["score"],
                    result.get("reasoning", ""),
                    job["id"],
                ),
            )
            conn.commit()
        except Exception as exc:
            logger.error("Failed to write score for job %s: %s", job.get("id"), exc)
            print("DB_ERROR (skipped)")
            continue

        fallback_flag = " [kw]" if "keyword fallback" in result.get("reasoning", "").lower() else ""
        print(f"{result['score']:3d}{fallback_flag}")
        scored += 1

        time.sleep(SCORE_SLEEP)

    return scored


def _score_unscored(conn, profile: dict, llm_settings: dict) -> int:
    """
    Find all active jobs without an LLM score and score them.

    Useful for re-scoring after downtime or after updating the candidate profile.
    Returns the number of jobs scored.
    """
    rows = conn.execute(
        "SELECT * FROM jobs WHERE llm_score IS NULL AND dismissed = 0 AND expired = 0"
    ).fetchall()

    if not rows:
        print("  No unscored active jobs found.")
        return 0

    jobs = [dict(r) for r in rows]
    print(f"  Found {len(jobs)} unscored active jobs")
    return _score_jobs(conn, jobs, profile, llm_settings)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _json_load(val) -> list | None:
    """Safely parse a value that may be a JSON string, list, or None into a list."""
    if val is None:
        return None
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _print_header(company_count: int) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'=' * 64}")
    print(f"  Reverse ATS Pipeline")
    print(f"  {now}")
    print(f"  Targeting {company_count} companies")
    print(f"{'=' * 64}\n")


def _print_summary(stats: dict) -> None:
    print(f"\n{'=' * 64}")
    print(f"  Pipeline Complete")
    # Local New/Updated is relative to whatever SQLite the runner has on disk.
    # On ephemeral CI runners that starts empty, so `New` matches `Fetched`
    # every run — for the real net-add count, see the D1 line below.
    print(
        f"  Fetched: {stats['total_fetched']} | "
        f"Local New: {stats['new_jobs']} | "
        f"Local Updated: {stats['updated_jobs']}"
    )
    if "d1_pushed" in stats:
        print(
            f"  D1: pushed {stats['d1_pushed']} | "
            f"net-new {stats.get('d1_new', 0)} | "
            f"updated {stats.get('d1_updated', 0)}"
        )
    print(
        f"  Expired: {stats.get('expired_jobs', 0)} | "
        f"LLM Scored: {stats.get('llm_scored', 0)}"
    )
    error_count = len(stats.get("errors", []))
    print(f"  Errors: {error_count}")
    if error_count:
        for err in stats["errors"]:
            print(f"    • {err}")
    print(f"{'=' * 64}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reverse ATS Pipeline — scrape, deduplicate, score, store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 pipeline.py                       # Full daily run\n"
            "  python3 pipeline.py --category fintech    # Fintech companies only\n"
            "  python3 pipeline.py --skip-score          # Scrape without LLM scoring\n"
            "  python3 pipeline.py --score-only          # Score unscored jobs, no scraping\n"
        ),
    )
    parser.add_argument(
        "--category",
        choices=list(CATEGORY_LABELS.keys()),
        metavar="CATEGORY",
        help=f"Only scrape one category. Choices: {', '.join(CATEGORY_LABELS.keys())}",
    )
    parser.add_argument(
        "--skip-score",
        action="store_true",
        help="Scrape and store jobs but skip LLM scoring.",
    )
    parser.add_argument(
        "--score-only",
        action="store_true",
        help="Score unscored active jobs without running the scraper.",
    )
    parser.add_argument(
        "--no-remote-filter",
        action="store_true",
        help="Include jobs that don't appear to be remote.",
    )
    parser.add_argument(
        "--db-path",
        help="Override the SQLite database file path.",
    )
    parser.add_argument(
        "--inference-url",
        help=(
            "Override the LLM inference gateway URL "
            "(default: http://localhost:8080/v1/chat/completions). "
            "Use this to point at GX10 from another machine."
        ),
    )
    parser.add_argument(
        "--push-to-d1",
        action="store_true",
        help=(
            "After scraping, push all upserted jobs to the centralized "
            "Cloudflare D1 instance via the ingest Worker. Requires env "
            "vars CF_INGEST_URL + CF_INGEST_SECRET. Used by GitHub Actions "
            "cron — local users typically leave this off."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.skip_score and args.score_only:
        print("Error: --skip-score and --score-only are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    run_pipeline(
        categories=[args.category] if args.category else None,
        remote_only=not args.no_remote_filter,
        skip_score=args.skip_score,
        score_only=args.score_only,
        db_path=args.db_path,
        inference_url=args.inference_url,
        push_to_d1=args.push_to_d1,
    )


if __name__ == "__main__":
    main()
