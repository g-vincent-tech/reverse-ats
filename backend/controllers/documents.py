from fastapi import APIRouter, Body, Query

from core.dependencies import DbConn
from services import document_service

router = APIRouter(prefix="/api/jobs", tags=["documents"])


@router.post("/{job_id}/tailored-resume")
def tailored_resume(conn: DbConn, job_id: str):
    return document_service.tailored_resume_response(conn, job_id)


@router.post("/{job_id}/cover-letter-docx")
def cover_letter_docx(conn: DbConn, job_id: str):
    return document_service.cover_letter_docx_response(conn, job_id)


@router.get("/{job_id}/tailored-resume.docx")
def tailored_resume_get(conn: DbConn, job_id: str, token: str = ""):
    resp = document_service.tailored_resume_response(conn, job_id)
    if token:
        resp.set_cookie("rats_dl", token, max_age=30, path="/")
    return resp


@router.get("/{job_id}/cover-letter.docx")
def cover_letter_get(conn: DbConn, job_id: str, token: str = ""):
    resp = document_service.cover_letter_docx_response(conn, job_id)
    if token:
        resp.set_cookie("rats_dl", token, max_age=30, path="/")
    return resp


@router.post("/{job_id}/email-docs")
def email_docs(conn: DbConn, job_id: str):
    return document_service.email_job_docs(conn, job_id)


target_router = APIRouter(prefix="/api", tags=["documents"])


@target_router.get("/target-resume.docx")
def target_resume_get(conn: DbConn, role: str = "", focus: str = "", token: str = ""):
    resp = document_service.target_resume_response(conn, role, focus)
    if token:
        resp.set_cookie("rats_dl", token, max_age=30, path="/")
    return resp


@target_router.post("/target-resume/email")
def target_resume_email(conn: DbConn, payload: dict = Body(...)):
    return document_service.email_target_resume(
        conn,
        payload.get("role", ""),
        payload.get("focus", ""),
    )
