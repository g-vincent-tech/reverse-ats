"""Admin, feed metadata, and analytics business logic."""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import HTTPException

from repositories.database import (
    backup_db,
    create_company,
    delete_company,
    get_analytics,
    get_company,
    get_companies,
    get_llm_settings,
    list_backups,
    update_company,
    update_llm_settings,
)
from models import AnalyticsOut, CompanyCreate, CompanyOut, CompanyUpdate, LLMSettingsOut, LLMSettingsUpdate

_LEGACY_CATEGORY_LABELS = {
    "fintech": "Fintech",
    "big_tech": "Big Tech",
    "ai_tech": "AI & Tech",
    "healthtech": "HealthTech",
    "quant": "Quant / Trading",
}


def mask_api_key(settings: dict) -> dict:
    if settings.get("api_key"):
        key = settings["api_key"]
        settings["api_key"] = key[:8] + "*" * (len(key) - 8) if len(key) > 8 else "***"
    return settings


def analytics(conn: sqlite3.Connection) -> AnalyticsOut:
    return AnalyticsOut.model_validate(get_analytics(conn))


def list_db_backups() -> list:
    return list_backups()


def create_db_backup(reason: str) -> dict:
    info = backup_db(reason=reason)
    if info is None:
        raise HTTPException(status_code=500, detail="DB file does not exist")
    return info


def list_companies(
    conn: sqlite3.Connection,
    category: Optional[str],
    enabled_only: bool,
) -> list[CompanyOut]:
    companies = get_companies(conn, category=category, enabled_only=enabled_only)
    return [CompanyOut.model_validate(c) for c in companies]


def get_company_detail(conn: sqlite3.Connection, company_id: int) -> CompanyOut:
    company = get_company(conn, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail=f"Company {company_id} not found")
    return CompanyOut.model_validate(company)


def create_company_record(conn: sqlite3.Connection, body: CompanyCreate) -> CompanyOut:
    new_id = create_company(conn, body.model_dump())
    company = get_company(conn, new_id)
    return CompanyOut.model_validate(company)


def update_company_record(conn: sqlite3.Connection, company_id: int, body: CompanyUpdate) -> CompanyOut:
    company = get_company(conn, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail=f"Company {company_id} not found")
    updates = body.model_dump(exclude_none=True)
    updated = update_company(conn, company_id, updates)
    return CompanyOut.model_validate(updated)


def delete_company_record(conn: sqlite3.Connection, company_id: int) -> dict:
    if not delete_company(conn, company_id):
        raise HTTPException(status_code=404, detail=f"Company {company_id} not found")
    return {"deleted": True, "id": company_id}


def list_feed_industries(conn: sqlite3.Connection) -> list[dict]:
    from services.industry_packs import PACKS

    rows = conn.execute(
        """
        SELECT category, COUNT(*) AS n
        FROM jobs
        WHERE dismissed = 0 AND expired = 0 AND category IS NOT NULL AND category != ''
        GROUP BY category
        """
    ).fetchall()
    counts: dict[str, int] = {row["category"]: row["n"] for row in rows}

    industries: list[dict] = []
    seen: set[str] = set()

    for pack_id, pack in PACKS.items():
        industries.append({"id": pack_id, "label": pack["name"], "count": counts.get(pack_id, 0)})
        seen.add(pack_id)

    for cat_id, count in counts.items():
        if cat_id in seen:
            continue
        label = _LEGACY_CATEGORY_LABELS.get(cat_id) or cat_id.replace("_", " ").title()
        industries.append({"id": cat_id, "label": label, "count": count})

    industries.sort(key=lambda item: (0 if item["count"] > 0 else 1, -item["count"], item["label"]))
    return industries


def list_feed_locations(conn: sqlite3.Connection, filter_tokens: Optional[str]) -> dict:
    from services.location_parser import aggregate

    needles = (
        [token.strip() for token in filter_tokens.split(",") if token.strip()]
        if filter_tokens
        else None
    )
    rows = conn.execute(
        "SELECT location FROM jobs "
        "WHERE dismissed = 0 AND expired = 0 "
        "AND location IS NOT NULL AND location != ''"
    ).fetchall()
    return aggregate((row["location"] for row in rows), filter_tokens=needles)


def list_industry_packs() -> list:
    from services.industry_packs import get_available_packs

    return get_available_packs()


def install_industry_pack(conn: sqlite3.Connection, pack_id: str) -> dict:
    from services.industry_packs import get_pack_companies

    companies = get_pack_companies(pack_id)
    if not companies:
        raise HTTPException(status_code=404, detail=f"Pack '{pack_id}' not found")

    installed = 0
    skipped = 0
    for company in companies:
        try:
            create_company(conn, company)
            installed += 1
        except Exception:
            skipped += 1
    return {"pack_id": pack_id, "installed": installed, "skipped": skipped, "total": len(companies)}


def get_llm_settings_masked(conn: sqlite3.Connection) -> LLMSettingsOut:
    settings = mask_api_key(get_llm_settings(conn))
    return LLMSettingsOut.model_validate(settings)


def update_llm_settings_masked(conn: sqlite3.Connection, body: LLMSettingsUpdate) -> LLMSettingsOut:
    updates = body.model_dump(exclude_none=True)
    updated = mask_api_key(update_llm_settings(conn, updates))
    return LLMSettingsOut.model_validate(updated)


def test_llm_settings(conn: sqlite3.Connection) -> dict:
    settings = get_llm_settings(conn)
    from services.scorer import check_inference_health, score_job

    health = check_inference_health(settings)
    result = score_job(
        title="Senior AI Engineer",
        company="Test Company",
        location="Remote, US",
        department="Engineering",
        description="Looking for a senior engineer to build AI/ML infrastructure.",
        settings=settings,
    )
    return {
        "health": health,
        "test_score": result,
        "provider": settings.get("provider", "keyword_only"),
    }
