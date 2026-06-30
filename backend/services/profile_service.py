"""Profile, inventory, and resume upload business logic."""
from __future__ import annotations

import io
import re
import sqlite3
from typing import Optional

from fastapi import HTTPException, UploadFile

from repositories.database import get_inventory, get_llm_settings, get_profile, save_inventory, update_profile
from models import ProfileOut, ProfileUpdate


def get_profile_data(conn: sqlite3.Connection) -> ProfileOut:
    return ProfileOut.model_validate(get_profile(conn))


def update_profile_data(conn: sqlite3.Connection, body: ProfileUpdate) -> ProfileOut:
    updates = body.model_dump(exclude_none=True)
    updated = update_profile(conn, updates)
    return ProfileOut.model_validate(updated)


async def upload_resume(conn: sqlite3.Connection, file: UploadFile) -> dict:
    name = (file.filename or "").lower()
    data = await file.read()
    text = ""
    try:
        if name.endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif name.endswith(".docx"):
            import docx

            doc = docx.Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
        elif name.endswith(".txt") or name.endswith(".md"):
            text = data.decode("utf-8", errors="ignore")
        else:
            raise HTTPException(status_code=400, detail="Unsupported file. Upload a PDF, DOCX, or TXT résumé.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read that file: {e}") from e

    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(text) < 30:
        raise HTTPException(status_code=400, detail="Could not extract enough text from that file.")

    update_profile(conn, {"resume_text": text})
    return {"resume_text": text, "chars": len(text)}


def get_inventory_data(conn: sqlite3.Connection) -> dict:
    return get_inventory(conn)


def update_inventory(conn: sqlite3.Connection, body: dict) -> dict:
    current = get_inventory(conn)
    for key in (
        "skills", "experience", "education", "certifications",
        "summary", "total_years_experience", "sources",
    ):
        if key in body:
            current[key] = body[key]
    return save_inventory(conn, current)


def merge_inventory(existing: dict, new: dict) -> dict:
    """Union a freshly-extracted inventory into the existing one."""
    def norm(s):
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    by_skill: dict = {}
    for skill in (existing.get("skills") or []) + (new.get("skills") or []):
        key = norm(skill.get("name"))
        if not key:
            continue
        if key not in by_skill:
            by_skill[key] = dict(skill)
        else:
            ex = by_skill[key]
            ex["keywords"] = sorted(set((ex.get("keywords") or []) + (skill.get("keywords") or [])))
            ex["years_num"] = max(ex.get("years_num") or 0, skill.get("years_num") or 0) or None
            ex["years_label"] = ex.get("years_label") or skill.get("years_label")
            ex["basis"] = ex.get("basis") or skill.get("basis")
            srcs = set(filter(None, [ex.get("source"), skill.get("source")]))
            ex["source"] = "+".join(sorted(srcs)) if srcs else "resume"

    def dedup(items, keyfn):
        seen, out = set(), []
        for item in items:
            key = keyfn(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    return {
        "skills": list(by_skill.values()),
        "experience": dedup(
            (existing.get("experience") or []) + (new.get("experience") or []),
            lambda e: norm(e.get("company")) + "|" + norm(e.get("title")),
        ),
        "education": dedup(
            (existing.get("education") or []) + (new.get("education") or []),
            lambda e: norm(e.get("school")) + "|" + norm(e.get("degree")),
        ),
        "certifications": dedup(
            (existing.get("certifications") or []) + (new.get("certifications") or []),
            lambda c: norm(c.get("name")),
        ),
        "summary": new.get("summary") or existing.get("summary"),
        "total_years_experience": new.get("total_years_experience") or existing.get("total_years_experience"),
        "sources": sorted(set((existing.get("sources") or []) + (new.get("sources") or []))),
    }


def extract_inventory(conn: sqlite3.Connection, body: Optional[dict]) -> dict:
    text = ((body or {}).get("text") or "").strip()
    source = (body or {}).get("source") or ("linkedin" if text else "resume")
    if not text:
        text = (get_profile(conn).get("resume_text") or "").strip()
    if len(text) < 40:
        raise HTTPException(
            status_code=400,
            detail="Paste your experience/skills, or add a résumé in Profile first.",
        )

    settings = get_llm_settings(conn)
    from services.scorer import extract_inventory as llm_extract

    result = llm_extract(text, settings)
    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])

    result["sources"] = [source]
    merged = merge_inventory(get_inventory(conn), result)
    return save_inventory(conn, merged)


def suggest_roles(conn: sqlite3.Connection) -> dict:
    profile = get_profile(conn)
    settings = get_llm_settings(conn)
    from services.scorer import suggest_roles as llm_suggest

    return llm_suggest(
        resume_text=profile.get("resume_text", "") or "",
        settings=settings,
    )
