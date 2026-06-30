"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.config import CORS_ORIGINS, SPA_DIST
from controllers import admin, analytics, documents, feed, health, jobs, pipeline, profile
from repositories.database import init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Reverse ATS", version="1.0.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(documents.router)
    app.include_router(documents.target_router)
    app.include_router(pipeline.router)
    app.include_router(profile.router)
    app.include_router(analytics.router)
    app.include_router(admin.router)
    app.include_router(feed.router)

    _mount_spa(app)
    return app


def _mount_spa(app: FastAPI) -> None:
    """Serve built React app when app/dist exists (single-server SPA)."""
    if not SPA_DIST.is_dir():
        return

    if (SPA_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(SPA_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/") or full_path == "health":
            raise HTTPException(status_code=404, detail="not found")
        candidate = SPA_DIST / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(SPA_DIST / "index.html"))
