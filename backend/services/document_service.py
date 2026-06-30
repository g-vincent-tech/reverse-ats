"""Document generation and email relay business logic."""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request

from fastapi import HTTPException, Response

from core.config import DIGEST_ENV_FILE, DOCX_MIME
from repositories.database import get_inventory, get_job, get_llm_settings, get_profile


def digest_env() -> dict:
    """Load CF_BASE_URL / CF_INGEST_SECRET / recipient from env or .digest.env."""
    out = {k: os.environ.get(k, "") for k in ("CF_BASE_URL", "CF_INGEST_SECRET", "CANDIDATE_CONTACT", "EMAIL_TO")}
    if DIGEST_ENV_FILE.exists():
        for line in DIGEST_ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key in out and not out[key] and value:
                out[key] = value
    return out


def relay_recipient() -> str:
    cfg = digest_env()
    match = re.search(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        f"{cfg.get('EMAIL_TO', '')} {cfg.get('CANDIDATE_CONTACT', '')}",
    )
    return (match.group(0) if match else "aries.lao@gmail.com").strip()


def relay_send(to: str, subject: str, html: str, attachments: list) -> str:
    """POST email through Worker /digest/send. Returns '' on success."""
    cfg = digest_env()
    base, secret = cfg.get("CF_BASE_URL", "").rstrip("/"), cfg.get("CF_INGEST_SECRET", "")
    if not base or not secret:
        return "Email relay not configured (CF_BASE_URL / CF_INGEST_SECRET)."

    payload = json.dumps({"to": to, "subject": subject, "html": html, "attachments": attachments}).encode()
    req = urllib.request.Request(f"{base}/digest/send", data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {secret}")
    req.add_header("User-Agent", "Mozilla/5.0 (reverse-ats-gx10)")
    req.add_header("Content-Type", "application/json")

    last = ""
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                if json.loads(resp.read().decode()).get("ok"):
                    return ""
        except urllib.error.HTTPError as e:
            last = f"relay {e.code}: {e.read().decode()[:200]}"
        except Exception as e:  # noqa: BLE001
            last = f"relay error: {e}"
    return last or "send failed"


def resume_docx_bytes(job: dict, settings: dict, profile: dict) -> tuple[bytes | None, str | None]:
    master = (profile.get("resume_text") or "").strip()
    from services import resume_tailor as rt

    if master and rt.looks_like_master(master):
        from services.scorer import tailor_master_resume
        from services.docgen import master_resume_to_docx

        res = tailor_master_resume(master, job, settings)
        if res.get("error"):
            return None, res["error"]
        return master_resume_to_docx(
            res.get("name") or os.environ.get("CANDIDATE_NAME", ""),
            res.get("contact", ""),
            res.get("target_title", ""),
            res.get("sections", []),
        ), None
    return None, "No master résumé on file — upload it in Admin → Profile first."


def tailored_resume_response(conn: sqlite3.Connection, job_id: str) -> Response:
    job = get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    settings = get_llm_settings(conn)
    profile = get_profile(conn)
    master = (profile.get("resume_text") or "").strip()
    from services import resume_tailor as rt
    from services.docgen import slug

    if master and rt.looks_like_master(master):
        from services.scorer import tailor_master_resume
        from services.docgen import master_resume_to_docx

        res = tailor_master_resume(master, job, settings)
        if res.get("error"):
            raise HTTPException(status_code=502, detail=res["error"])
        data = master_resume_to_docx(
            res.get("name") or os.environ.get("CANDIDATE_NAME", ""),
            res.get("contact", ""),
            res.get("target_title", ""),
            res.get("sections", []),
        )
    else:
        inv = get_inventory(conn)
        if not inv.get("skills"):
            raise HTTPException(
                status_code=400,
                detail="Upload your résumé (Admin → Profile) or build the Skills inventory first.",
            )
        from services.scorer import generate_tailored_resume
        from services.docgen import resume_to_docx

        result = generate_tailored_resume(inv, job, settings)
        if result.get("error"):
            raise HTTPException(status_code=502, detail=result["error"])
        data = resume_to_docx(
            result,
            os.environ.get("CANDIDATE_NAME", ""),
            os.environ.get("CANDIDATE_CONTACT", ""),
        )

    filename = f"resume_{slug(job.get('company', ''))}_{slug(job.get('title', ''))}.docx"
    return Response(
        content=data,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def cover_letter_docx_response(conn: sqlite3.Connection, job_id: str) -> Response:
    job = get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    profile = get_profile(conn)
    settings = get_llm_settings(conn)
    from services.scorer import generate_cover_letter
    from services.docgen import cover_to_docx, slug

    result = generate_cover_letter(
        title=job["title"],
        company=job["company"],
        location=job.get("location", ""),
        department=job.get("department", ""),
        description=job.get("description_snippet", "") or job.get("description_full", ""),
        resume_text=profile.get("resume_text"),
        settings=settings,
    )
    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])

    data = cover_to_docx(
        result.get("cover_letter", ""),
        os.environ.get("CANDIDATE_NAME", ""),
        os.environ.get("CANDIDATE_CONTACT", ""),
        job.get("company", ""),
    )
    filename = f"cover_{slug(job.get('company', ''))}_{slug(job.get('title', ''))}.docx"
    return Response(
        content=data,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def email_job_docs(conn: sqlite3.Connection, job_id: str) -> dict:
    cfg = digest_env()
    base, secret = cfg.get("CF_BASE_URL", "").rstrip("/"), cfg.get("CF_INGEST_SECRET", "")
    to = relay_recipient()
    if not base or not secret:
        raise HTTPException(status_code=503, detail="Email relay not configured (CF_BASE_URL / CF_INGEST_SECRET).")

    job = get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    settings = get_llm_settings(conn)
    profile = get_profile(conn)
    from services.docgen import slug, cover_to_docx

    attachments = []
    resume_bytes, resume_err = resume_docx_bytes(job, settings, profile)
    if resume_bytes:
        attachments.append({
            "filename": f"resume_{slug(job.get('company', ''))}_{slug(job.get('title', ''))}.docx",
            "content": base64.b64encode(resume_bytes).decode(),
        })

    from services.scorer import generate_cover_letter

    cover = generate_cover_letter(
        title=job["title"],
        company=job["company"],
        location=job.get("location", ""),
        department=job.get("department", ""),
        description=job.get("description_snippet", "") or job.get("description_full", ""),
        resume_text=profile.get("resume_text"),
        settings=settings,
    )
    if not cover.get("error") and cover.get("cover_letter"):
        cover_bytes = cover_to_docx(
            cover["cover_letter"],
            os.environ.get("CANDIDATE_NAME", ""),
            cfg.get("CANDIDATE_CONTACT", ""),
            job.get("company", ""),
        )
        attachments.append({
            "filename": f"cover_{slug(job.get('company', ''))}_{slug(job.get('title', ''))}.docx",
            "content": base64.b64encode(cover_bytes).decode(),
        })

    if not attachments:
        raise HTTPException(status_code=502, detail=resume_err or "Could not generate documents.")

    subject = f"{job.get('title', '')} @ {job.get('company', '')} — tailored résumé + cover letter"
    html = (
        f"<p>Tailored documents for <b>{job.get('title', '')}</b> at <b>{job.get('company', '')}</b>"
        f"{(' · ' + job.get('location')) if job.get('location') else ''}.</p>"
        f"<p>{'Résumé + cover letter' if len(attachments) == 2 else attachments[0]['filename']} attached.</p>"
        f"<p style='color:#888;font-size:12px'>Generated by your private Reverse-ATS instance.</p>"
    )

    err = relay_send(to, subject, html, attachments)
    if err:
        raise HTTPException(status_code=502, detail=f"Email send failed — {err}")
    return {"status": "sent", "to": to, "attached": [a["filename"] for a in attachments]}


def target_resume_bytes(conn: sqlite3.Connection, role: str, focus: str) -> tuple[str, bytes]:
    role = (role or "").strip()
    if not role:
        raise HTTPException(status_code=400, detail="Enter a target role (e.g. 'Healthcare Program Manager').")

    profile = get_profile(conn)
    master = (profile.get("resume_text") or "").strip()
    from services import resume_tailor as rt

    if not (master and rt.looks_like_master(master)):
        raise HTTPException(status_code=400, detail="No master résumé on file — upload it in Admin → Profile first.")

    job = {
        "title": role,
        "company": "",
        "location": "Remote",
        "description_full": (f"Target role: {role}.\n\n{(focus or '').strip()}").strip(),
    }
    from services.scorer import tailor_master_resume
    from services.docgen import master_resume_to_docx, slug

    res = tailor_master_resume(master, job, get_llm_settings(conn))
    if res.get("error"):
        raise HTTPException(status_code=502, detail=res["error"])

    data = master_resume_to_docx(
        res.get("name") or os.environ.get("CANDIDATE_NAME", ""),
        res.get("contact", ""),
        res.get("target_title", ""),
        res.get("sections", []),
    )
    return f"resume_{slug(role)}.docx", data


def target_resume_response(conn: sqlite3.Connection, role: str, focus: str) -> Response:
    filename, data = target_resume_bytes(conn, role, focus)
    return Response(
        content=data,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def email_target_resume(conn: sqlite3.Connection, role: str, focus: str) -> dict:
    filename, data = target_resume_bytes(conn, role, focus)
    to = relay_recipient()
    role = (role or "").strip()
    html = (
        f"<p>Your résumé tailored toward <b>{role}</b> is attached (.docx), "
        f"generated from your master résumé.</p>"
        f"<p style='color:#888;font-size:12px'>Private Reverse-ATS instance.</p>"
    )
    err = relay_send(
        to,
        f"Tailored résumé — {role}",
        html,
        [{"filename": filename, "content": base64.b64encode(data).decode()}],
    )
    if err:
        raise HTTPException(status_code=502, detail=f"Email send failed — {err}")
    return {"status": "sent", "to": to, "attached": [filename]}
