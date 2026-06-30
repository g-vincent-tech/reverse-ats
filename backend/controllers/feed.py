from typing import Optional

from fastapi import APIRouter, Query

from core.dependencies import DbConn
from services import admin_service

router = APIRouter(prefix="/api/feed", tags=["feed"])


@router.get("/industries")
def list_feed_industries(conn: DbConn):
    return admin_service.list_feed_industries(conn)


@router.get("/locations")
def list_feed_locations(
    conn: DbConn,
    filter: Optional[str] = Query(
        None,
        description="Comma-separated location tokens for hierarchical narrowing.",
    ),
):
    return admin_service.list_feed_locations(conn, filter)
