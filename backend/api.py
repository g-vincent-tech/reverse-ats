#!/usr/bin/env python3
"""Reverse ATS — FastAPI entry point (uvicorn api:app)."""
from core.app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    from core.config import DEFAULT_HOST, DEFAULT_PORT

    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT)
