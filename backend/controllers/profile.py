from typing import Optional

from fastapi import APIRouter, Body, File, UploadFile

from core.dependencies import DbConn
from models import ProfileOut, ProfileUpdate
from services import profile_service

router = APIRouter(prefix="/api", tags=["profile"])


@router.get("/profile", response_model=ProfileOut)
def get_profile(conn: DbConn):
    return profile_service.get_profile_data(conn)


@router.put("/profile", response_model=ProfileOut)
def update_profile(conn: DbConn, body: ProfileUpdate):
    return profile_service.update_profile_data(conn, body)


@router.post("/profile/resume-upload")
async def upload_resume(conn: DbConn, file: UploadFile = File(...)):
    return await profile_service.upload_resume(conn, file)


@router.get("/inventory")
def get_inventory(conn: DbConn):
    return profile_service.get_inventory_data(conn)


@router.put("/inventory")
def update_inventory(conn: DbConn, body: dict):
    return profile_service.update_inventory(conn, body)


@router.post("/inventory/extract")
def extract_inventory(conn: DbConn, body: Optional[dict] = Body(default=None)):
    return profile_service.extract_inventory(conn, body)


@router.post("/profile/suggest-roles")
def suggest_roles(conn: DbConn):
    return profile_service.suggest_roles(conn)
