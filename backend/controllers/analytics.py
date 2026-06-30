from typing import Optional

from fastapi import APIRouter, Query

from core.dependencies import DbConn
from models import AnalyticsOut, ScrapeRunOut
from services import admin_service, scrape_service

router = APIRouter(tags=["analytics", "scrape"])


@router.get("/api/analytics", response_model=AnalyticsOut)
def analytics(conn: DbConn):
    return admin_service.analytics(conn)


@router.get("/api/scrape/status", response_model=Optional[ScrapeRunOut])
def scrape_status(conn: DbConn):
    return scrape_service.scrape_status(conn)


@router.post("/api/scrape/trigger")
def trigger_scrape():
    return scrape_service.trigger_scrape()


@router.get("/api/scoring/stats")
def job_score_stats(conn: DbConn):
    return scrape_service.scoring_stats(conn)


@router.post("/api/scoring/rescore")
def trigger_rescore(conn: DbConn, all: bool = Query(False)):
    return scrape_service.trigger_rescore(conn, all)
