"""FastAPI dependency injection."""
from __future__ import annotations

import sqlite3
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends

from repositories.database import get_connection


def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Yield a per-request SQLite connection."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


DbConn = Annotated[sqlite3.Connection, Depends(get_db)]
