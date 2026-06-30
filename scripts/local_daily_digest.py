#!/usr/bin/env python3
"""Daily digest for the PRIVATE (local SQLite) Reverse-ATS instance.

Runs on GX10 cron. Unlike daily_digest_gx10.py (which pulls from the cloud Worker),
this uses the LOCAL engine end to end:
  1. Pull Remote First Jobs into the local SQLite DB.
  2. Score unscored jobs with the local years-weighted scorer (pipeline --score-only).
  3. For every NEW match at or above the score threshold (default 90), generate the
     master-template tailored résumé + grounded cover letter and email them as .docx
     attachments — one digest email, all attachments — via the Worker /digest/send relay.
  4. Record emailed jobs so each is sent only once.

Env (loaded from .digest.env by the cron):
  CF_BASE_URL, CF_INGEST_SECRET   — the send relay
  CANDIDATE_NAME / CANDIDATE_CONTACT or EMAIL_TO — recipient + résumé header
  DIGEST_MIN_SCORE (default 90), DIGEST_MAX_JOBS (default 25)
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)

DB_PATH = os.environ.setdefault(
    "REVERSE_ATS_DB_PATH", os.path.join(ROOT, "local-instance", "reverse_ats.db")
)
MIN_SCORE = int(os.environ.get("DIGEST_MIN_SCORE", "90"))
MAX_JOBS = int(os.environ.get("DIGEST_MAX_JOBS", "25"))
PACE_SECONDS = float(os.environ.get("DIGEST_PACE", "2"))

import repositories.database as db  # noqa: E402
from services.scorer import tailor_master_resume, generate_cover_letter  # noqa: E402
from services import resume_tailor as rt  # noqa: E402
from services.docgen import slug, master_resume_to_docx, cover_to_docx  # noqa: E402


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def _recipient(profile_contact: str) -> str:
    blob = f"{os.environ.get('EMAIL_TO','')} {os.environ.get('CANDIDATE_CONTACT','')} {profile_contact}"
    m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", blob)
    return (m.group(0) if m else "aries.lao@gmail.com").strip()


def _run(cmd: list, label: str) -> None:
    env = {**os.environ, "REVERSE_ATS_DB_PATH": DB_PATH}
    log(f"{label}: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        log(f"{label} exited {r.returncode}: {r.stderr[-300:]}")
    else:
        log(f"{label} ok")


def _ensure_log_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS digest_log (job_id TEXT PRIMARY KEY, sent_at TEXT)")
    conn.commit()


def _new_matches(conn):
    rows = conn.execute(
        """
        SELECT j.* FROM jobs j
        WHERE j.expired = 0
          AND j.llm_score >= ?
          AND j.id NOT IN (SELECT job_id FROM digest_log)
        ORDER BY j.llm_score DESC, j.first_seen_at DESC
        """,
        (MIN_SCORE,),
    ).fetchall()
    return [dict(r) for r in rows]


def _send(base: str, secret: str, to: str, subject: str, html: str, attachments: list) -> bool:
    payload = json.dumps({"to": to, "subject": subject, "html": html, "attachments": attachments}).encode()
    req = urllib.request.Request(f"{base}/digest/send", data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {secret}")
    req.add_header("User-Agent", "Mozilla/5.0 (reverse-ats-gx10)")
    req.add_header("Content-Type", "application/json")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                if json.loads(r.read().decode()).get("ok"):
                    return True
        except urllib.error.HTTPError as e:
            log(f"relay {e.code}: {e.read().decode()[:200]}")
        except Exception as e:  # noqa: BLE001
            log(f"relay error: {e}")
        time.sleep(3 * (attempt + 1))
    return False


def main() -> int:
    base = os.environ.get("CF_BASE_URL", "").rstrip("/")
    secret = os.environ.get("CF_INGEST_SECRET", "")
    if not base or not secret:
        log("CF_BASE_URL / CF_INGEST_SECRET missing — cannot send. Exiting.")
        return 2

    # 1. Pull RFJ into local SQLite, then 2. score unscored jobs.
    _run([sys.executable, os.path.join(HERE, "rfj_to_local_sqlite.py")], "pull-rfj")
    _run([sys.executable, os.path.join(BACKEND, "pipeline.py"), "--score-only"], "score")

    conn = db.get_connection()
    _ensure_log_table(conn)

    profile = {}
    try:
        from repositories.database import get_profile
        profile = get_profile(conn) or {}
    except Exception:  # noqa: BLE001
        pass
    master = (profile.get("resume_text") or "").strip()
    if not (master and rt.looks_like_master(master)):
        log("No master résumé on file — cannot tailor. Upload it in Admin → Profile.")
        return 3

    try:
        from repositories.database import get_llm_settings
        settings = get_llm_settings(conn)
    except Exception:  # noqa: BLE001
        settings = None

    to = _recipient(rt.split_master(master)[1])
    matches = _new_matches(conn)
    log(f"{len(matches)} new ≥{MIN_SCORE}% match(es) to send to {to}")
    if not matches:
        log("Nothing new today. Done.")
        return 0
    if len(matches) > MAX_JOBS:
        log(f"capping to {MAX_JOBS} of {len(matches)} (rest will send next run)")
        matches = matches[:MAX_JOBS]

    attachments, rows_html, sent_ids = [], [], []
    for job in matches:
        company, title = job.get("company", ""), job.get("title", "")
        try:
            res = tailor_master_resume(master, job, settings)
            if not res.get("error"):
                rbytes = master_resume_to_docx(
                    res.get("name") or os.environ.get("CANDIDATE_NAME", ""),
                    res.get("contact", ""), res.get("target_title", ""), res.get("sections", []))
                attachments.append({"filename": f"resume_{slug(company)}_{slug(title)}.docx",
                                    "content": base64.b64encode(rbytes).decode()})
            cl = generate_cover_letter(
                title=title, company=company, location=job.get("location", ""),
                department=job.get("department", ""),
                description=job.get("description_snippet", "") or job.get("description_full", ""),
                resume_text=master, settings=settings)
            if not cl.get("error") and cl.get("cover_letter"):
                cbytes = cover_to_docx(cl["cover_letter"], os.environ.get("CANDIDATE_NAME", ""),
                                       res.get("contact", ""), company)
                attachments.append({"filename": f"cover_{slug(company)}_{slug(title)}.docx",
                                    "content": base64.b64encode(cbytes).decode()})
            score = job.get("llm_score")
            loc = job.get("location") or "Remote"
            url = job.get("url") or "#"
            rows_html.append(
                f'<tr><td style="padding:10px 0;border-bottom:1px solid #eee">'
                f'<div style="font-weight:600;color:#111">{title} '
                f'<span style="color:#16a34a">· {score}%</span></div>'
                f'<div style="color:#555;font-size:13px">{company} · {loc}</div>'
                f'<a href="{url}" style="font-size:12px;color:#2563eb">View posting →</a></td></tr>')
            sent_ids.append(job["id"])
            log(f"  prepared {title} @ {company} ({score}%)")
        except Exception as e:  # noqa: BLE001
            log(f"  skip {title} @ {company}: {e}")
        time.sleep(PACE_SECONDS)

    if not attachments:
        log("No documents generated. Nothing sent.")
        return 4

    today = datetime.now(timezone.utc).strftime("%b %d")
    subject = f"Reverse-ATS — {len(sent_ids)} new ≥{MIN_SCORE}% match(es) ({today})"
    html = (
        f'<div style="font-family:system-ui,Arial,sans-serif;max-width:620px">'
        f'<h2 style="color:#111">{len(sent_ids)} new match(es) at ≥{MIN_SCORE}% fit</h2>'
        f'<p style="color:#555">Tailored résumé + cover letter attached for each (master-template tailoring).</p>'
        f'<table style="width:100%;border-collapse:collapse">{"".join(rows_html)}</table>'
        f'<p style="color:#999;font-size:12px;margin-top:16px">Your private Reverse-ATS instance · '
        f'{len(attachments)} attachment(s)</p></div>')

    if _send(base, secret, to, subject, html, attachments):
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany("INSERT OR REPLACE INTO digest_log (job_id, sent_at) VALUES (?, ?)",
                         [(jid, now) for jid in sent_ids])
        conn.commit()
        log(f"SENT {len(sent_ids)} job(s), {len(attachments)} attachment(s) to {to}")
        return 0
    log("send failed — not marking jobs as emailed (will retry next run)")
    return 5


if __name__ == "__main__":
    sys.exit(main())
