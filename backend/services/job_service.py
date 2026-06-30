"""Job listing and mutation business logic."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from repositories.database import (
    create_pipeline_entry,
    dismiss_job,
    get_job,
    get_jobs,
    get_llm_settings,
    get_pipeline,
    get_profile,
)
from models import JobListResponse, JobOut, PipelineOut


def list_jobs(
    conn: sqlite3.Connection,
    *,
    page: int,
    per_page: int,
    remote_only: bool,
    min_score: int,
    category: Optional[str],
    dismissed: bool,
    expired: bool,
    new_since: Optional[str],
    search: Optional[str],
    sort_by: str,
    exclude_companies: Optional[str],
    locations: Optional[str],
) -> JobListResponse:
    exc_companies = (
        [c.strip() for c in exclude_companies.split(",") if c.strip()]
        if exclude_companies
        else None
    ) or None
    loc_list = (
        [loc.strip() for loc in locations.split(",") if loc.strip()]
        if locations
        else None
    ) or None
    jobs_raw, total = get_jobs(
        conn,
        page=page,
        per_page=per_page,
        remote_only=remote_only,
        min_score=min_score,
        category=category,
        dismissed=dismissed,
        expired=expired,
        new_since=new_since,
        search=search,
        sort_by=sort_by,
        exclude_companies=exc_companies,
        locations=loc_list,
    )
    jobs = [JobOut.model_validate(j) for j in jobs_raw]
    return JobListResponse(jobs=jobs, total=total, page=page, per_page=per_page)


def get_job_detail(conn: sqlite3.Connection, job_id: str) -> JobOut:
    job = get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobOut.model_validate(job)


def set_job_dismissed(
    conn: sqlite3.Connection,
    job_id: str,
    dismissed: bool,
) -> dict:
    job = get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if not dismiss_job(conn, job_id, dismissed=dismissed):
        raise HTTPException(status_code=500, detail="Failed to update dismiss status")
    return {"job_id": job_id, "dismissed": dismissed}


def save_job_to_pipeline(conn: sqlite3.Connection, job_id: str) -> PipelineOut:
    job = get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    pipeline_id = create_pipeline_entry(conn, job_id, stage="saved")
    if pipeline_id is None:
        raise HTTPException(status_code=500, detail="Failed to create pipeline entry")

    pipeline_items = get_pipeline(conn)
    entry = next((p for p in pipeline_items if p["id"] == pipeline_id), None)
    if entry is None:
        raise HTTPException(status_code=500, detail="Pipeline entry created but could not be retrieved")

    out = PipelineOut.model_validate(entry)
    if entry.get("job"):
        out.job = JobOut.model_validate(entry["job"])
    else:
        out.job = JobOut.model_validate(job)
    return out


def generate_cover_letter(conn: sqlite3.Connection, job_id: str) -> dict:
    job = get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    profile = get_profile(conn)
    settings = get_llm_settings(conn)

    from services.scorer import generate_cover_letter as llm_cover_letter

    result = llm_cover_letter(
        title=job["title"],
        company=job["company"],
        location=job.get("location", ""),
        department=job.get("department", ""),
        description=job.get("description_snippet", "") or job.get("description_full", ""),
        resume_text=profile.get("resume_text"),
        target_roles=profile.get("target_roles") if isinstance(profile.get("target_roles"), list) else None,
        must_have_skills=profile.get("must_have_skills") if isinstance(profile.get("must_have_skills"), list) else None,
        nice_to_have_skills=profile.get("nice_to_have_skills") if isinstance(profile.get("nice_to_have_skills"), list) else None,
        settings=settings,
    )

    if not result.get("error") and result.get("cover_letter"):
        now = datetime.now(timezone.utc).isoformat()
        pipeline_row = conn.execute(
            "SELECT id FROM pipeline WHERE job_id = ?", (job_id,)
        ).fetchone()
        if pipeline_row:
            conn.execute(
                "UPDATE pipeline SET cover_letter = ?, updated_at = ? WHERE id = ?",
                (result["cover_letter"], now, pipeline_row["id"]),
            )
            conn.commit()

    return result
