from fastapi import APIRouter, Query

from core.dependencies import DbConn
from models import PipelineCreate, PipelineEventOut, PipelineListResponse, PipelineOut, PipelineUpdate
from services import pipeline_service

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.get("", response_model=PipelineListResponse)
def list_pipeline(conn: DbConn):
    return pipeline_service.list_pipeline(conn)


@router.get("/export")
def export_pipeline(conn: DbConn, format: str = Query("csv", pattern="^(csv|json)$")):
    return pipeline_service.export_pipeline(conn, format)


@router.post("", response_model=PipelineOut)
def create_pipeline(conn: DbConn, body: PipelineCreate):
    return pipeline_service.create_pipeline(conn, body)


@router.put("/{pipeline_id}", response_model=PipelineOut)
def update_pipeline(conn: DbConn, pipeline_id: int, body: PipelineUpdate):
    return pipeline_service.update_pipeline(conn, pipeline_id, body)


@router.get("/{pipeline_id}/events", response_model=list[PipelineEventOut])
def pipeline_events(conn: DbConn, pipeline_id: int):
    return pipeline_service.pipeline_events(conn, pipeline_id)


@router.delete("/{pipeline_id}")
def delete_pipeline_entry(conn: DbConn, pipeline_id: int):
    return pipeline_service.delete_pipeline(conn, pipeline_id)
