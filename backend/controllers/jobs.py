from typing import Optional

from fastapi import APIRouter, Query

from core.dependencies import DbConn
from models import JobDismiss, JobListResponse, JobOut, PipelineOut
from services import job_service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=JobListResponse)
def list_jobs(
    conn: DbConn,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    remote_only: bool = Query(True),
    min_score: int = Query(0, ge=0, le=100),
    category: Optional[str] = Query(None),
    dismissed: bool = Query(False),
    expired: bool = Query(False),
    new_since: Optional[str] = Query(None, description="ISO date, e.g. 2026-04-15"),
    search: Optional[str] = Query(None, description="Search title/company"),
    sort_by: str = Query("score", description="Sort: score, newest, oldest, company, title"),
    exclude_companies: Optional[str] = Query(None, description="Comma-separated company names to exclude"),
    locations: Optional[str] = Query(None, description="Comma-separated location tokens"),
):
    return job_service.list_jobs(
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
        exclude_companies=exclude_companies,
        locations=locations,
    )


@router.get("/{job_id}", response_model=JobOut)
def get_job_detail(conn: DbConn, job_id: str):
    return job_service.get_job_detail(conn, job_id)


@router.post("/{job_id}/dismiss")
def dismiss_job_endpoint(conn: DbConn, job_id: str, body: Optional[JobDismiss] = None):
    dismissed = body.dismissed if body is not None else True
    return job_service.set_job_dismissed(conn, job_id, dismissed)


@router.post("/{job_id}/save", response_model=PipelineOut)
def save_job(conn: DbConn, job_id: str):
    return job_service.save_job_to_pipeline(conn, job_id)


@router.post("/{job_id}/cover-letter")
def generate_cover_letter(conn: DbConn, job_id: str):
    return job_service.generate_cover_letter(conn, job_id)
