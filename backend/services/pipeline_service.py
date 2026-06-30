"""Pipeline CRUD and export business logic."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from repositories.database import (
    create_pipeline_entry,
    get_job,
    get_pipeline,
    get_pipeline_events,
    update_pipeline_entry,
)
from models import JobOut, PipelineCreate, PipelineEventOut, PipelineListResponse, PipelineOut, PipelineStage, PipelineUpdate


def _pipeline_out_from_entry(entry: dict, job: dict | None = None) -> PipelineOut:
    out = PipelineOut.model_validate(entry)
    if entry.get("job"):
        out.job = JobOut.model_validate(entry["job"])
    elif job:
        out.job = JobOut.model_validate(job)
    return out


def list_pipeline(conn: sqlite3.Connection) -> PipelineListResponse:
    raw_items = get_pipeline(conn)
    items = []
    for row in raw_items:
        entry = PipelineOut.model_validate(row)
        if row.get("company") and row.get("title"):
            entry.job = JobOut(
                id=row.get("job_id", ""),
                company=row["company"],
                title=row["title"],
                location=row.get("location"),
                url=row.get("url", ""),
                remote=bool(row.get("remote", False)),
                keyword_score=row.get("keyword_score", 0),
                llm_score=row.get("llm_score"),
                category=row.get("category"),
                ats_type=row.get("ats_type"),
                first_seen_at=row.get("first_seen_at", ""),
                last_seen_at=row.get("last_seen_at", ""),
                description_snippet=row.get("description_snippet"),
            )
        items.append(entry)

    by_stage: dict[str, list[PipelineOut]] = {}
    for item in items:
        stage_key = item.stage.value if isinstance(item.stage, PipelineStage) else str(item.stage)
        by_stage.setdefault(stage_key, []).append(item)

    return PipelineListResponse(items=items, by_stage=by_stage)


def export_pipeline(conn: sqlite3.Connection, fmt: str) -> StreamingResponse:
    rows = get_pipeline(conn)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    if fmt == "json":
        payload = json.dumps(rows, indent=2, default=str)
        return StreamingResponse(
            io.BytesIO(payload.encode("utf-8")),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="reverse-ats-pipeline-{ts}.json"'},
        )

    cols = [
        "stage", "company", "title", "url", "location",
        "applied_at", "notes",
        "contact_name", "contact_email", "contact_role",
        "next_step", "next_step_date", "salary_offered",
        "created_at", "updated_at", "source_deleted", "job_id",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in cols})

    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="reverse-ats-pipeline-{ts}.csv"'},
    )


def create_pipeline(conn: sqlite3.Connection, body: PipelineCreate) -> PipelineOut:
    job = get_job(conn, body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{body.job_id}' not found")

    pipeline_id = create_pipeline_entry(
        conn,
        job_id=body.job_id,
        stage=body.stage.value,
        notes=body.notes,
    )
    if pipeline_id is None:
        raise HTTPException(status_code=500, detail="Failed to create pipeline entry")

    pipeline_items = get_pipeline(conn)
    entry = next((p for p in pipeline_items if p["id"] == pipeline_id), None)
    if entry is None:
        raise HTTPException(status_code=500, detail="Pipeline entry created but could not be retrieved")
    return _pipeline_out_from_entry(entry, job)


def update_pipeline(conn: sqlite3.Connection, pipeline_id: int, body: PipelineUpdate) -> PipelineOut:
    updates = body.model_dump(exclude_none=True)
    if "stage" in updates and isinstance(updates["stage"], PipelineStage):
        updates["stage"] = updates["stage"].value

    if not updates:
        raise HTTPException(status_code=422, detail="No update fields provided")

    if not update_pipeline_entry(conn, pipeline_id, updates):
        raise HTTPException(status_code=404, detail=f"Pipeline entry {pipeline_id} not found")

    pipeline_items = get_pipeline(conn)
    entry = next((p for p in pipeline_items if p["id"] == pipeline_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Pipeline entry {pipeline_id} not found after update")
    return _pipeline_out_from_entry(entry)


def pipeline_events(conn: sqlite3.Connection, pipeline_id: int) -> list[PipelineEventOut]:
    events_raw = get_pipeline_events(conn, pipeline_id)
    if events_raw is None:
        raise HTTPException(status_code=404, detail=f"Pipeline entry {pipeline_id} not found")
    return [PipelineEventOut.model_validate(e) for e in events_raw]


def delete_pipeline(conn: sqlite3.Connection, pipeline_id: int) -> dict:
    rows = get_pipeline(conn)
    entry = next((p for p in rows if p["id"] == pipeline_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Pipeline entry {pipeline_id} not found")

    conn.execute("DELETE FROM pipeline_events WHERE pipeline_id = ?", (pipeline_id,))
    conn.execute("DELETE FROM pipeline WHERE id = ?", (pipeline_id,))
    conn.commit()
    return {"deleted": True, "id": pipeline_id}
