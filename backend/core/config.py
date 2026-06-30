"""Central configuration and filesystem paths."""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent

PIPELINE_SCRIPT = BACKEND_ROOT / "pipeline.py"
RFJ_SCRIPT = REPO_ROOT / "scripts" / "rfj_to_local_sqlite.py"
DIGEST_ENV_FILE = REPO_ROOT / ".digest.env"
SPA_DIST = REPO_ROOT / "app" / "dist"

DEFAULT_HOST = os.environ.get("REVERSE_ATS_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("REVERSE_ATS_PORT", "8091"))

CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "https://*.pages.dev",
]

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
