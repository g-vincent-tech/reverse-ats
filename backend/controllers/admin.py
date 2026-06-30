from typing import Optional

from fastapi import APIRouter, Query

from core.dependencies import DbConn
from models import CompanyCreate, CompanyOut, CompanyUpdate, LLMSettingsOut, LLMSettingsUpdate
from services import admin_service

router = APIRouter(tags=["admin"])


@router.get("/api/admin/backups")
def list_db_backups():
    return admin_service.list_db_backups()


@router.post("/api/admin/backups")
def create_db_backup(reason: str = Query("manual")):
    return admin_service.create_db_backup(reason)


@router.get("/api/admin/companies", response_model=list[CompanyOut])
def list_companies(
    conn: DbConn,
    category: Optional[str] = Query(None),
    enabled_only: bool = Query(False),
):
    return admin_service.list_companies(conn, category, enabled_only)


@router.get("/api/admin/companies/{company_id}", response_model=CompanyOut)
def get_company_detail(conn: DbConn, company_id: int):
    return admin_service.get_company_detail(conn, company_id)


@router.post("/api/admin/companies", response_model=CompanyOut)
def create_company(conn: DbConn, body: CompanyCreate):
    return admin_service.create_company_record(conn, body)


@router.put("/api/admin/companies/{company_id}", response_model=CompanyOut)
def update_company(conn: DbConn, company_id: int, body: CompanyUpdate):
    return admin_service.update_company_record(conn, company_id, body)


@router.delete("/api/admin/companies/{company_id}")
def delete_company(conn: DbConn, company_id: int):
    return admin_service.delete_company_record(conn, company_id)


@router.get("/api/admin/industry-packs")
def list_industry_packs():
    return admin_service.list_industry_packs()


@router.post("/api/admin/industry-packs/{pack_id}/install")
def install_industry_pack(conn: DbConn, pack_id: str):
    return admin_service.install_industry_pack(conn, pack_id)


@router.get("/api/admin/llm-settings", response_model=LLMSettingsOut)
def get_llm_settings(conn: DbConn):
    return admin_service.get_llm_settings_masked(conn)


@router.put("/api/admin/llm-settings", response_model=LLMSettingsOut)
def update_llm_settings(conn: DbConn, body: LLMSettingsUpdate):
    return admin_service.update_llm_settings_masked(conn, body)


@router.post("/api/admin/llm-settings/test")
def test_llm_settings(conn: DbConn):
    return admin_service.test_llm_settings(conn)
