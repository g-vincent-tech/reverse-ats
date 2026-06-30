#!/usr/bin/env python3
"""Load curated Remote First Jobs into the LOCAL SQLite DB (private GX10 instance).

Same source as scripts/scrape_remotefirstjobs.py, but writes straight into the
local backend's SQLite via db.upsert_job instead of POSTing to the Cloudflare
Worker — so the private GX10 instance has jobs with full descriptions, no cloud.

  REVERSE_ATS_DB_PATH=/path/to/local.db python3 scripts/rfj_to_local_sqlite.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

import repositories.database as db  # noqa: E402
from scrape_remotefirstjobs import _fetch_page, _map  # noqa: E402


def main() -> int:
    pages = [0, 1, 2, 3, 4]
    seen: set[str] = set()
    jobs: list[dict] = []
    for p in pages:
        try:
            raw = _fetch_page(p, None)
        except Exception as e:
            print(f"page {p} fetch failed: {e}")
            continue
        for j in raw:
            m = _map(j)
            if m and m["id"] not in seen:
                seen.add(m["id"])
                jobs.append(m)
        time.sleep(0.4)

    if not jobs:
        print("no jobs fetched")
        return 1

    db.init_db()
    conn = db.get_connection()
    new = upd = 0
    for m in jobs:
        try:
            _, is_new = db.upsert_job(conn, m)
            new += 1 if is_new else 0
            upd += 0 if is_new else 1
        except Exception as e:
            print(f"upsert {m.get('id','?')[:10]} failed: {e}")
    conn.commit()
    conn.close()
    print(f"loaded {len(jobs)} RFJ jobs into local SQLite: new={new} updated={upd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
