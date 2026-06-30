#!/usr/bin/env python3
"""
Multi-provider LLM job relevance scorer.

Supports: OpenAI, Anthropic, Ollama, any OpenAI-compatible endpoint, or
keyword-only fallback. Configure your provider in Admin → LLM Settings.

Quick start:
    1. Run the web UI and go to Admin → LLM Settings
    2. Pick your provider and paste in your API key (if required)
    3. Set your resume in Admin → Profile
    4. Rescore jobs from the Jobs tab

Supported providers
-------------------
openai            — OpenAI API (GPT-4o, GPT-4o-mini, etc.)
anthropic         — Anthropic API (Claude Sonnet, Haiku, etc.)
ollama            — Ollama running locally (no API key needed)
openai_compatible — Any OpenAI-compatible endpoint: llama.cpp server,
                    vLLM, LiteLLM, Groq, Together AI, Fireworks AI, etc.
keyword_only      — No LLM; pure keyword matching (free fallback)
"""

import json
import logging
import re
import requests
from typing import Optional

logger = logging.getLogger("reverse-ats.scorer")

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict] = {
    "openai": {
        "name": "OpenAI",
        "default_url": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-4o-mini",
        "requires_key": True,
        "format": "openai",
    },
    "anthropic": {
        "name": "Anthropic",
        "default_url": "https://api.anthropic.com/v1/messages",
        "default_model": "claude-haiku-4-20250514",
        "requires_key": True,
        "format": "anthropic",
    },
    "ollama": {
        "name": "Ollama (Local)",
        "default_url": "http://localhost:11434/v1/chat/completions",
        "default_model": "llama3.1:8b",
        "requires_key": False,
        "format": "openai",
    },
    "groq": {
        # Groq runs open-weight models (Llama, Qwen, Mixtral) on their LPU
        # hardware with a generous free tier (14,400 req/day, 30 req/min).
        # Excellent quality + speed combo for free LLM scoring.
        "name": "Groq (Free Llama / Qwen)",
        "default_url": "https://api.groq.com/openai/v1/chat/completions",
        "default_model": "llama-3.3-70b-versatile",
        "requires_key": True,
        "format": "openai",
    },
    "openai_compatible": {
        "name": "OpenAI-Compatible (llama.cpp, vLLM, LiteLLM, Groq, Together AI, etc.)",
        "default_url": "http://localhost:8080/v1/chat/completions",
        "default_model": "default",
        "requires_key": False,
        "format": "openai",
    },
    "keyword_only": {
        "name": "Keyword Only (No LLM)",
        "default_url": "",
        "default_model": "",
        "requires_key": False,
        "format": "none",
    },
}

REQUEST_TIMEOUT = 120  # seconds — LLMs on large prompts can be slow

# ---------------------------------------------------------------------------
# Default resume placeholder
# ---------------------------------------------------------------------------

DEFAULT_RESUME_SUMMARY = """
No resume configured yet.

Go to Admin → Profile and paste your resume or a summary of your background.
The scorer will use keyword matching only until a resume is provided.
"""

# ---------------------------------------------------------------------------
# Scoring prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a job-fit evaluator scoring REAL selectability through an ATS — not just skill overlap.\n\n"
    "STEP 1 — HARD REQUIREMENTS (knockouts): Identify any DISQUALIFYING gate in the JD — an ACTIVE "
    "certification or license (e.g. 'active PMP', 'Epic certified', security clearance, RN, CPA), a specific "
    "minimum number of years in a NAMED skill/tool, mandatory work authorization or required on-site "
    "location, or an explicit 'must have' / 'required' gate. For EACH, decide whether the candidate CLEARLY "
    "meets it from their profile. List every UNMET hard requirement in \"knockouts\". Note: a lapsed or "
    "'previously held' cert does NOT meet an 'active' requirement; deep hands-on experience with a tool does "
    "NOT satisfy a 'certified in <tool>' requirement.\n\n"
    "STEP 2 — SCORE by experience depth: weight the candidate's strongest (most-years) skills most heavily. "
    "BUT if \"knockouts\" is non-empty you MUST cap the score at 65 — a candidate an ATS would auto-filter "
    "cannot be a 90 no matter how strong the skills fit. Give 90-100 ONLY when the fit is excellent AND there "
    "are NO unmet hard requirements.\n\n"
    "Return ONLY valid JSON with this exact structure:\n"
    '{"score": <0-100>, "reasoning": "<2-3 sentence explanation>", '
    '"match_highlights": ["<strength1>"], "concerns": ["<concern1>"], "knockouts": ["<unmet hard requirement>"]}\n\n'
    "Scoring guide:\n"
    "- 90-100: Excellent depth match AND all hard requirements met\n"
    "- 70-89: Strong match, minor gaps, no knockouts\n"
    "- 50-69: Moderate — good overlap on weaker/newer skills, OR capped here by an unmet hard requirement\n"
    "- 30-49: Weak match — partial overlap, different domain or level\n"
    "- 0-29: Poor match — wrong field, wrong level, or missing critical skills"
)

# ---------------------------------------------------------------------------
# Keyword scoring weights (used as fallback when LLM is unavailable)
#
# These weights are intentionally generic to cover common software/AI/ML
# engineering and leadership roles. Edit them to match your own background.
# Higher numbers = stronger signal for your target roles.
# ---------------------------------------------------------------------------

WEIGHTED_SKILLS: dict[str, int] = {
    # Core identity — very high weight
    "python": 5,
    "machine learning": 5,
    "ai": 4,
    "ml": 4,
    "llm": 5,
    "large language model": 5,
    "multi-agent": 5,
    "agent orchestration": 5,
    "inference": 4,
    "infrastructure": 3,
    # Engineering depth
    "typescript": 3,
    "react": 3,
    "data engineering": 3,
    "postgresql": 2,
    "fastapi": 3,
    "docker": 2,
    "kubernetes": 2,
    "real-time": 2,
    "streaming": 2,
    "distributed": 2,
    "api": 1,
    "websocket": 2,
    # Domain fit
    "healthcare": 4,
    "hipaa": 4,
    "fintech": 3,
    "financial": 2,
    "quantitative": 2,
    # Seniority signals
    "director": 4,
    "vp ": 5,
    "vice president": 5,
    "head of": 5,
    "principal": 4,
    "staff engineer": 4,
    "architect": 4,
    "engineering manager": 5,
    "technical program manager": 4,
    "program manager": 3,
    "solutions architect": 4,
    # Role type alignment
    "mlops": 4,
    "platform engineering": 3,
    "llmops": 5,
    "ai infrastructure": 5,
    "ai platform": 4,
    "model deployment": 4,
    "feature engineering": 3,
    "xgboost": 3,
    "deep learning": 3,
}

# Normalization scale factor: 1.5 so scoring doesn't require every keyword
# to match for a job to reach a meaningful score.
_KEYWORD_TOTAL = sum(WEIGHTED_SKILLS.values())
_KEYWORD_SCALE = 1.5


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_inference_health(settings: Optional[dict] = None) -> dict:
    """
    Check whether the configured LLM provider is reachable.

    Args:
        settings: Provider settings dict (same shape as passed to score_job).
                  If None or provider is "keyword_only", returns healthy=True
                  immediately since no network call is needed.

    Returns:
        {
            "healthy": bool,
            "provider": str,
            "message": str,
        }
    """
    if not settings or settings.get("provider") == "keyword_only":
        return {
            "healthy": True,
            "provider": "keyword_only",
            "message": "Keyword scoring active (no LLM configured)",
        }

    provider = settings.get("provider", "openai_compatible")
    provider_info = PROVIDERS.get(provider, PROVIDERS["openai_compatible"])
    url = settings.get("api_url") or provider_info["default_url"]

    # OpenAI-format: probe the /models sibling endpoint
    if provider_info["format"] == "openai":
        health_url = url.replace("/chat/completions", "/models")
        headers: dict[str, str] = {}
        api_key = settings.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            resp = requests.get(health_url, headers=headers, timeout=5)
            resp.raise_for_status()
            return {
                "healthy": True,
                "provider": provider,
                "message": f"{provider_info['name']} is reachable",
            }
        except Exception as exc:
            return {
                "healthy": False,
                "provider": provider,
                "message": f"{provider_info['name']}: {exc}",
            }

    # Anthropic: can't easily probe without spending tokens, so just verify
    # the API key is present.
    if provider_info["format"] == "anthropic":
        if not settings.get("api_key"):
            return {
                "healthy": False,
                "provider": provider,
                "message": "Anthropic API key not configured",
            }
        return {
            "healthy": True,
            "provider": provider,
            "message": "Anthropic configured (key present)",
        }

    return {
        "healthy": False,
        "provider": provider,
        "message": f"Unknown provider format: {provider_info.get('format')}",
    }


# ---------------------------------------------------------------------------
# Primary scorer
# ---------------------------------------------------------------------------

def score_job(
    title: str,
    company: str,
    location: str,
    department: str,
    description: str,
    resume_text: Optional[str] = None,
    target_roles: Optional[list[str]] = None,
    must_have_skills: Optional[list[str]] = None,
    nice_to_have_skills: Optional[list[str]] = None,
    settings: Optional[dict] = None,
) -> dict:
    """
    Score a single job posting 0–100 for fit against the candidate profile.

    Tries the configured LLM provider first. Falls back to keyword scoring on
    any connection error, timeout, HTTP error, or unparseable LLM output.

    Args:
        title:               Job title.
        company:             Employer name.
        location:            Location string (may be empty).
        department:          Department / team name (may be empty).
        description:         Full or snippet job description text.
        resume_text:         Candidate resume / profile text. Falls back to
                             DEFAULT_RESUME_SUMMARY if not provided.
        target_roles:        List of role types the candidate is targeting.
        must_have_skills:    Skills required for the candidate to consider a role.
        nice_to_have_skills: Preferred-but-optional skills.
        settings:            LLM provider settings dict with keys:
                               provider    — one of the PROVIDERS keys above
                               api_key     — API key (required for openai/anthropic)
                               api_url     — override the default endpoint URL
                               model       — model name/alias
                               temperature — float, default 0.1
                               max_tokens  — int, default 500

    Returns:
        {
            "score": int (0–100),
            "reasoning": str,
            "match_highlights": list[str],
            "concerns": list[str],
        }
    """
    # No settings or explicit keyword_only → skip the network entirely
    if not settings or settings.get("provider") == "keyword_only":
        return _keyword_fallback(title, description, "no LLM configured")

    provider = settings.get("provider", "openai_compatible")
    provider_info = PROVIDERS.get(provider, PROVIDERS["openai_compatible"])

    # Guard: provider requires a key but none is set
    if provider_info["requires_key"] and not settings.get("api_key"):
        logger.warning("Provider %s requires an API key — keyword fallback", provider)
        return _keyword_fallback(title, description, f"{provider_info['name']} API key not set")

    user_prompt = _build_user_prompt(
        resume_text or DEFAULT_RESUME_SUMMARY,
        target_roles,
        must_have_skills,
        nice_to_have_skills,
        title,
        company,
        location,
        department,
        description,
    )

    try:
        if provider_info["format"] == "openai":
            return _call_openai_format(user_prompt, settings, provider_info)
        elif provider_info["format"] == "anthropic":
            return _call_anthropic_format(user_prompt, settings, provider_info)
        else:
            raise ValueError(f"Unknown provider format: {provider_info['format']}")

    except requests.ConnectionError as exc:
        logger.info("LLM provider unreachable (%s) — keyword fallback", exc)
        return _keyword_fallback(title, description, f"connection error: {exc}")
    except requests.Timeout:
        logger.warning("LLM provider timed out after %ds — keyword fallback", REQUEST_TIMEOUT)
        return _keyword_fallback(title, description, "request timeout")
    except requests.HTTPError as exc:
        logger.warning("LLM provider HTTP error %s — keyword fallback", exc)
        return _keyword_fallback(title, description, f"HTTP {exc.response.status_code}")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        logger.warning("Failed to parse LLM response (%s) — keyword fallback", exc)
        return _keyword_fallback(title, description, f"parse error: {exc}")
    except Exception as exc:
        logger.warning("Unexpected scorer error (%s) — keyword fallback", exc)
        return _keyword_fallback(title, description, str(exc))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_user_prompt(
    profile: str,
    target_roles: Optional[list[str]],
    must_have_skills: Optional[list[str]],
    nice_to_have_skills: Optional[list[str]],
    title: str,
    company: str,
    location: str,
    department: str,
    description: str,
) -> str:
    target_str = json.dumps(target_roles) if target_roles else "Not specified"
    must_str = json.dumps(must_have_skills) if must_have_skills else "Not specified"
    nice_str = json.dumps(nice_to_have_skills) if nice_to_have_skills else "Not specified"

    return (
        f"## Candidate Profile\n{profile}\n\n"
        f"## Target Roles\n{target_str}\n\n"
        f"## Must-Have Skills\n{must_str}\n\n"
        f"## Nice-to-Have Skills\n{nice_str}\n\n"
        f"## Job Posting\n"
        f"Company: {company}\n"
        f"Title: {title}\n"
        f"Location: {location}\n"
        f"Department: {department}\n\n"
        f"Description:\n{description[:3000]}\n\n"
        "Score this job for fit."
    )


def _call_openai_format(user_prompt: str, settings: dict, provider_info: dict) -> dict:
    """
    Call an OpenAI-compatible chat completions endpoint.

    Covers: OpenAI, Ollama, llama.cpp server, vLLM, LiteLLM proxy, Groq,
    Together AI, Fireworks AI, and any other OpenAI-compatible API.

    Raises requests exceptions on network/HTTP failures so score_job can
    handle them uniformly.
    """
    url = settings.get("api_url") or provider_info["default_url"]
    model = settings.get("model") or provider_info["default_model"]
    temperature = settings.get("temperature", 0.1)
    max_tokens = settings.get("max_tokens", 500)

    headers: dict[str, str] = {"Content-Type": "application/json"}
    api_key = settings.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resp = requests.post(
        url,
        headers=headers,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()

    msg = resp.json()["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    # Qwen3 and other "thinking" models may put chain-of-thought in a
    # separate `reasoning` field and leave `content` empty, or wrap
    # thinking in <think>...</think> tags inside content.
    if not content and msg.get("reasoning"):
        # Thinking model — try to extract JSON from the reasoning field
        content = msg["reasoning"].strip()
    content = _strip_thinking_tags(content)
    return _parse_llm_response(content)


def _call_anthropic_format(user_prompt: str, settings: dict, provider_info: dict) -> dict:
    """
    Call the Anthropic Messages API.

    Uses x-api-key header and the anthropic-version handshake required by the
    Anthropic API. System prompt is passed as a top-level "system" field.

    Raises requests exceptions on network/HTTP failures so score_job can
    handle them uniformly.
    """
    url = settings.get("api_url") or provider_info["default_url"]
    model = settings.get("model") or provider_info["default_model"]
    temperature = settings.get("temperature", 0.1)
    max_tokens = settings.get("max_tokens", 500)
    api_key = settings.get("api_key", "")

    resp = requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()

    content = resp.json()["content"][0]["text"].strip()
    return _parse_llm_response(content)


def _strip_thinking_tags(content: str) -> str:
    """Remove <think>...</think> blocks that reasoning models may emit."""
    import re
    # Remove <think> blocks (greedy — may span multiple lines)
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return cleaned or content  # fall back to original if nothing remains


def _parse_llm_response(content: str) -> dict:
    """
    Parse the raw text from an LLM into a validated score dict.

    Handles:
    - Markdown code fences (```json ... ```)
    - Thinking models that embed JSON after chain-of-thought text
    - Partial / truncated JSON (best-effort extraction)
    """
    import re

    # Strip markdown fences
    if "```" in content:
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", content, re.DOTALL)
        if fence_match:
            content = fence_match.group(1).strip()

    # Try direct parse first
    try:
        result = json.loads(content)
        return _validate_result(result)
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: extract the first JSON object from the text
    # (handles models that emit thinking text before the JSON)
    json_match = re.search(r"\{[^{}]*\"score\"[^{}]*\}", content, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group())
            return _validate_result(result)
        except (json.JSONDecodeError, ValueError):
            pass

    # Last resort: raise so the caller can fall back to keyword scoring
    raise ValueError(f"parse error: could not extract JSON score from LLM response")


KNOCKOUT_CAP = 65


def _validate_result(result: dict) -> dict:
    """Ensure all required keys exist and the score is clamped to [0, 100].
    Enforce the knockout cap server-side and surface unmet hard requirements."""
    score = result.get("score", 0)
    try:
        score = max(0, min(100, int(score)))
    except (ValueError, TypeError):
        score = 0

    knockouts = [str(k).strip() for k in (result.get("knockouts") or []) if str(k).strip()]
    reasoning = str(result.get("reasoning", "")).strip()
    if knockouts:
        # An ATS gate the candidate fails caps real selectability regardless of fit.
        score = min(score, KNOCKOUT_CAP)
        flag = "⚠️ KNOCKOUT — " + "; ".join(knockouts)
        if flag.lower() not in reasoning.lower():
            reasoning = f"{flag}. {reasoning}".strip()

    return {
        "score": score,
        "reasoning": reasoning,
        "match_highlights": list(result.get("match_highlights", [])),
        "concerns": list(result.get("concerns", [])),
        "knockouts": knockouts,
    }


# ---------------------------------------------------------------------------
# Cover letter generation
# ---------------------------------------------------------------------------

COVER_LETTER_SYSTEM_PROMPT = """Write a SHORT, simple, straightforward cover letter for this specific role.

Guidelines:
- 3 short paragraphs, ~180 words total. Plain, everyday language.
- NO clichés, NO flowery adjectives, NO buzzwords, NO fluff. Sound like a real, direct person.
- Get to the point: why this candidate fits THIS role, citing 2-3 concrete things from their experience (real metrics/outcomes, not generic claims).
- Do NOT use openers like "I am writing to express my interest" or "I believe I would be a great fit".
- Do NOT fabricate experience — only what's in the résumé/inventory.

Return ONLY the cover letter body — no JSON, no markdown, no greeting/signature lines."""


TAILORED_RESUME_SYSTEM_PROMPT = """You are an expert résumé writer tailoring a candidate's résumé to ONE specific job for ATS systems.

Use the candidate's inventory + the target job. Apply these rules:
1. HEADLINE — the job's EXACT title (ATS title-matching is real); closest specific match if generic.
2. SUMMARY — 2-3 sentences aimed at THIS role; end with a sentence mirroring the JD's top 2-3 must-have phrases verbatim (kept truthful). Do NOT state a hard total-years number.
3. SKILLS — reorder so the JD's required/nice-to-have skills the candidate HAS appear first, using the JD's exact terms. Spell out an acronym once next to its short form. Add a keyword ONLY if the inventory supports it.
4. EXPERIENCE — rewrite each role's bullets to foreground experience relevant to THIS job, quantified where the source gives numbers, action-verb first.
5. NEVER invent skills, employers, titles, dates, or metrics — only reframe what's there. Frame any career transition as deliberate.

Return ONLY valid JSON:
{"headline":"","summary":"","skills":["",""],"experience":[{"company":"","title":"","dates":"","bullets":["",""]}],"education":["",""],"certifications":["",""]}
JSON only — no prose, no markdown fences."""


MASTER_TAILOR_SYSTEM_PROMPT = """You tailor a candidate's MASTER résumé to ONE job by editing ONLY three fields. You must NOT rewrite their experience, projects, education, or certifications.

You are given the JD and the candidate's FULL master résumé. Produce ONLY:
1. target_title — the JD's EXACT job title (verbatim from the posting; if generic, the closest specific title that is still truthful).
2. summary — REWRITE the professional summary (3-4 sentences) so it is CATERED to THIS job: lead with the experience, domains, and skills from the master that are most relevant to the JD, and use the JD's key terms WHERE THE CANDIDATE GENUINELY HAS THEM. Every clause must be GROUNDED in the master (its summary, skills, projects, or experience) — do NOT introduce any skill, tool, employer, metric, domain, or claim that is not already in the master. CRITICAL: do NOT claim any certification, license, or "<X>-certified" credential unless that exact credential is listed in the master's CERTIFICATIONS section (e.g. never write "Epic-certified" — Epic certification is NOT in the master; "experienced in Epic implementations" is fine). If the JD asks for things the candidate lacks, simply don't mention them (lead with their real transferable strengths instead). Keep the candidate's authentic, senior voice. Do NOT state years or any "X+ years" number.
3. core_skills — the SAME skill lines, REORDERED so the groups the JD emphasizes come first and, within each line, the JD's exact terms appear first. Spell out an acronym once next to its short form (e.g. "Retrieval-Augmented Generation (RAG)"). You MAY add a keyword only if the master already demonstrates it elsewhere. NEVER invent skills. Keep each line's "**Group:**" bold label. Return core_skills as an array of strings, one per skill line.

Rules: change nothing else. Stay strictly truthful — grounded only in the master. No fabrication, no inflation.
Return ONLY valid JSON: {"target_title":"","summary":"","core_skills":["",""]}
No prose, no markdown fences."""


def tailor_master_resume(master_text: str, job: dict, settings: Optional[dict] = None) -> dict:
    """Surgically tailor a master résumé to a job. Returns {name, contact, target_title, sections} or {error}."""
    from services import resume_tailor as rt

    if not settings or settings.get("provider") == "keyword_only":
        return {"error": "Tailored résumé requires an LLM provider (Admin → LLM Settings)."}
    provider = settings.get("provider", "openai_compatible")
    provider_info = PROVIDERS.get(provider, PROVIDERS["openai_compatible"])

    name, contact, sections = rt.split_master(master_text)
    if not sections:
        return {"error": "Could not parse the master résumé structure."}
    cur_summary = rt.summary_text(sections)
    cur_skills = rt.core_skills_text(sections)
    full_master = rt.strip_cheatsheet(master_text)[:8000]

    desc = (job.get("description_full") or job.get("description_snippet") or "")[:3500]
    user_prompt = (
        f"## Target job\n{job.get('title','')} at {job.get('company','')}"
        f"{(' (' + job.get('location') + ')') if job.get('location') else ''}\n\n{desc}\n\n"
        f"## Candidate FULL master résumé (ground everything in this — do not go beyond it)\n{full_master}\n\n"
        f"## Current CORE SKILLS lines (reorder these)\n{cur_skills}\n\n"
        "Return the JSON with target_title, a JD-catered grounded summary, and reordered core_skills."
    )
    rs = {**settings, "max_tokens": 1500, "temperature": 0.2}
    try:
        if provider_info["format"] == "anthropic":
            content = _call_llm_raw_anthropic(user_prompt, MASTER_TAILOR_SYSTEM_PROMPT, rs, provider_info)
        else:
            content = _call_llm_raw(user_prompt, MASTER_TAILOR_SYSTEM_PROMPT, rs, provider_info)
    except Exception as e:
        return {"error": str(e)}
    parsed = _parse_json_object(content)
    if not isinstance(parsed, dict):
        return {"error": "Model returned unparseable tailoring. Try again."}

    target_title = (parsed.get("target_title") or job.get("title") or "").strip()
    new_summary = (parsed.get("summary") or "").strip()
    if not new_summary:  # safety: fall back to the master's summary verbatim
        new_summary = cur_summary
    skills = parsed.get("core_skills") or []
    if isinstance(skills, str):
        skills = [l for l in skills.splitlines() if l.strip()]
    skills = [str(s).strip() for s in skills if str(s).strip()]
    if not skills:  # safety: keep the master's skills verbatim if the model returned nothing
        skills = [l for l in cur_skills.splitlines() if l.strip()]

    new_sections = rt.apply_tailoring(sections, target_title, new_summary, skills)
    return {"name": name, "contact": contact, "target_title": target_title, "sections": new_sections}


def generate_tailored_resume(inventory: dict, job: dict, settings: Optional[dict] = None) -> dict:
    """Generate a job-tailored résumé as structured JSON from the inventory."""
    if not settings or settings.get("provider") == "keyword_only":
        return {"error": "Tailored résumé requires an LLM provider (Admin → LLM Settings)."}
    provider = settings.get("provider", "openai_compatible")
    provider_info = PROVIDERS.get(provider, PROVIDERS["openai_compatible"])

    # Compact inventory digest (skill groups + experience).
    lines = []
    if inventory.get("summary"):
        lines.append(f"Summary: {inventory['summary']}")
    groups = sorted(inventory.get("skills") or [], key=lambda g: (g.get("years_num") or 0), reverse=True)
    if groups:
        lines.append("Skill areas (strongest first): " + "; ".join(
            f"{g.get('name')} ({g.get('years_label') or ''}): {', '.join(g.get('keywords') or [])}" for g in groups))
    for e in inventory.get("experience") or []:
        lines.append(f"- {e.get('title','')} at {e.get('company','')} ({e.get('start','?')}–{e.get('end') or 'Present'})")
        for h in (e.get("highlights") or []):
            lines.append(f"  • {h}")
    if inventory.get("education"):
        lines.append("Education: " + "; ".join(
            f"{x.get('degree','')} {x.get('field','')} {x.get('school','')}".strip() for x in inventory["education"]))
    if inventory.get("certifications"):
        lines.append("Certifications: " + ", ".join(c.get("name", "") for c in inventory["certifications"]))
    inv_text = "\n".join(lines)

    desc = (job.get("description_full") or job.get("description_snippet") or "")[:3000]
    user_prompt = (
        f"## Candidate inventory\n{inv_text[:5000]}\n\n"
        f"## Target job\n{job.get('title','')} at {job.get('company','')}"
        f"{(' (' + job.get('location') + ')') if job.get('location') else ''}\n\n{desc}\n\n"
        "Produce the tailored résumé JSON per the instructions."
    )
    rs = {**settings, "max_tokens": 3000, "temperature": 0.3}
    try:
        if provider_info["format"] == "anthropic":
            content = _call_llm_raw_anthropic(user_prompt, TAILORED_RESUME_SYSTEM_PROMPT, rs, provider_info)
        else:
            content = _call_llm_raw(user_prompt, TAILORED_RESUME_SYSTEM_PROMPT, rs, provider_info)
    except Exception as e:
        return {"error": str(e)}
    parsed = _parse_json_object(content)
    if not isinstance(parsed, dict):
        return {"error": "Model returned unparseable résumé. Try again."}
    return parsed


def generate_cover_letter(
    title: str,
    company: str,
    location: str,
    department: str,
    description: str,
    resume_text: Optional[str] = None,
    target_roles: Optional[list[str]] = None,
    must_have_skills: Optional[list[str]] = None,
    nice_to_have_skills: Optional[list[str]] = None,
    settings: Optional[dict] = None,
) -> dict:
    """
    Generate a personalized cover letter for a specific job posting.

    Returns: {"cover_letter": str, "provider": str, "error": str|None}
    """
    if not settings or settings.get("provider") == "keyword_only":
        return {
            "cover_letter": "",
            "provider": "keyword_only",
            "error": "Cover letter generation requires an LLM provider. Configure one in Admin → LLM Settings.",
        }

    provider = settings.get("provider", "openai_compatible")
    provider_info = PROVIDERS.get(provider, PROVIDERS["openai_compatible"])

    if provider_info["requires_key"] and not settings.get("api_key"):
        return {
            "cover_letter": "",
            "provider": provider,
            "error": f"{provider_info['name']} API key not configured. Go to Admin → LLM Settings.",
        }

    profile = resume_text or "No resume provided. Please add your resume in Admin → Profile."
    target_str = json.dumps(target_roles) if target_roles else "Not specified"
    must_str = json.dumps(must_have_skills) if must_have_skills else "Not specified"
    nice_str = json.dumps(nice_to_have_skills) if nice_to_have_skills else "Not specified"

    user_prompt = (
        f"## Candidate Resume\n{profile}\n\n"
        f"## Candidate's Target Roles\n{target_str}\n\n"
        f"## Candidate's Key Skills\nMust-have: {must_str}\nNice-to-have: {nice_str}\n\n"
        f"## Job Posting\nCompany: {company}\nTitle: {title}\n"
        f"Location: {location}\nDepartment: {department}\n\n"
        f"Description:\n{description[:4000]}\n\n"
        f"Write a cover letter for this specific role."
    )

    try:
        # Use higher max_tokens for cover letters (need ~400 tokens for 300 words)
        cl_settings = {**settings, "max_tokens": 800, "temperature": 0.4}

        if provider_info["format"] == "openai":
            result = _call_llm_raw(user_prompt, COVER_LETTER_SYSTEM_PROMPT, cl_settings, provider_info)
        elif provider_info["format"] == "anthropic":
            result = _call_llm_raw_anthropic(user_prompt, COVER_LETTER_SYSTEM_PROMPT, cl_settings, provider_info)
        else:
            return {"cover_letter": "", "provider": provider, "error": "Unsupported provider format"}

        return {"cover_letter": result.strip(), "provider": provider, "error": None}

    except Exception as e:
        logger.warning("Cover letter generation failed: %s", e)
        return {"cover_letter": "", "provider": provider, "error": str(e)}


INVENTORY_SYSTEM_PROMPT = """You build a candidate's skill inventory by REASONING over their DATED work history — not by dumping a flat skill list.

Think like an analyst: read each role and its DATES, identify the candidate's real competency areas, GROUP related skills/tools into 8-14 meaningful groups, and for EACH group compute how many years they've actually practiced it by spanning the dated roles where that group was used. A skill area only present in a recent role spans only that role's months; a career-long area spans the whole career.

Output ONLY valid JSON in this exact shape:
{
  "summary": "<2-3 sentence professional summary, or null>",
  "total_years_experience": <number or null>,
  "skill_groups": [
    {
      "name": "<group name, e.g. 'Program & Portfolio Management'>",
      "keywords": ["<specific skill/tool in this group>", "..."],
      "years_label": "<human label, e.g. '22+ yrs', '~14 yrs', '<1 yr (~10 mo)'>",
      "years_num": <best numeric estimate of years, e.g. 22, 14, 0.8>,
      "basis": "<one line justifying the years from DATED roles, e.g. 'Epic implementations 6/2011 (Cleveland) → 12/2022 (Kaiser)'>"
    }
  ],
  "experience": [{"company":"<name>","title":"<title>","start":"<YYYY or YYYY-MM>","end":"<YYYY or null if current>","location":"<or null>","highlights":["<concise bullet>"]}],
  "education": [{"school":"<name>","degree":"<or null>","field":"<or null>","start":"<or null>","end":"<or null>"}],
  "certifications": [{"name":"<cert>","issuer":"<or null>","date":"<or null>"}]
}

Rules:
- GROUP, don't list flat. Each group's keywords are the specific matchable skills/tools (e.g. "Python","TypeScript","React").
- YEARS: if the source EXPLICITLY states years for a skill (e.g. "Project Management — 20 years", or a skills table with a Years column), USE that number for years_num and set basis to "Stated by candidate". Otherwise reason years_num from the DATED history span of the roles using that group — be honest: newly-learned areas are <1 yr even if impressive.
- basis: cite the real source — stated years, or specific roles/dates (e.g. "Aries Labs 8/2025–present"). Never invent.
- Order groups from most years to least.
- Do NOT invent skills, roles, dates, or metrics. Output JSON only — no prose, no markdown fences."""


def extract_inventory(resume_text: str, settings: Optional[dict] = None) -> dict:
    """Extract a structured skills/experience inventory from resume text using
    the configured LLM. Returns the inventory dict, or {"error": ...}."""
    if not settings or settings.get("provider") == "keyword_only":
        return {"error": "Inventory extraction requires an LLM provider. Configure one in Admin → LLM Settings."}
    provider = settings.get("provider", "openai_compatible")
    provider_info = PROVIDERS.get(provider, PROVIDERS["openai_compatible"])
    if provider_info["requires_key"] and not settings.get("api_key"):
        return {"error": f"{provider_info['name']} API key not configured."}
    if not resume_text or len(resume_text.strip()) < 40:
        return {"error": "Résumé is too short — upload or paste your résumé first."}

    user_prompt = f"## Source résumé\n\n{resume_text[:8000]}\n\nExtract the structured inventory per the instructions."
    inv_settings = {**settings, "max_tokens": 3500, "temperature": 0.1}
    try:
        if provider_info["format"] == "anthropic":
            content = _call_llm_raw_anthropic(user_prompt, INVENTORY_SYSTEM_PROMPT, inv_settings, provider_info)
        else:
            content = _call_llm_raw(user_prompt, INVENTORY_SYSTEM_PROMPT, inv_settings, provider_info)
    except Exception as e:
        logger.warning("Inventory extraction failed: %s", e)
        return {"error": str(e)}

    parsed = _parse_json_object(content)
    if not isinstance(parsed, dict):
        return {"error": "Model returned unparseable inventory. Try again."}
    return _normalize_inventory(parsed)


def _parse_json_object(text: str) -> Optional[dict]:
    """Parse the first JSON object out of an LLM response (tolerant of fences/prose)."""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", t)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        s, e = t.find("{"), t.rfind("}")
        if 0 <= s < e:
            try:
                return json.loads(t[s : e + 1])
            except json.JSONDecodeError:
                return None
    return None


def _normalize_inventory(raw: dict) -> dict:
    def _arr(v):
        return v if isinstance(v, list) else []

    # Skill GROUPS (preferred); fall back to a flat "skills" list if the model
    # used the old shape. Stored in the `skills` field (each entry is a group).
    src = raw.get("skill_groups")
    if not isinstance(src, list):
        src = raw.get("skills")
    skills = []
    for g in _arr(src)[:30]:
        if isinstance(g, str):
            name = g.strip()
            if name:
                skills.append({"name": name, "keywords": [], "years_label": None, "years_num": None, "basis": None, "source": "resume"})
            continue
        if not isinstance(g, dict):
            continue
        name = (g.get("name") or "").strip()
        if not name or len(name) > 140:
            continue
        kws = [str(k).strip() for k in _arr(g.get("keywords")) if str(k).strip()][:20]
        yn = g.get("years_num")
        yn = round(float(yn), 1) if isinstance(yn, (int, float)) else None
        skills.append({
            "name": name,
            "keywords": kws,
            "years_label": ((g.get("years_label") or "").strip()[:30]) or None,
            "years_num": yn,
            "basis": ((g.get("basis") or "").strip()[:300]) or None,
            "source": "resume",
        })

    experience = []
    for e in _arr(raw.get("experience"))[:60]:
        if not isinstance(e, dict):
            continue
        company, title = (e.get("company") or "").strip(), (e.get("title") or "").strip()
        if not company and not title:
            continue
        hl = [str(h).strip() for h in _arr(e.get("highlights")) if str(h).strip()][:8]
        experience.append({"company": company, "title": title, "start": e.get("start"),
                           "end": e.get("end"), "location": e.get("location"), "highlights": hl, "source": "resume"})

    education = [{"school": (e.get("school") or "").strip(), "degree": e.get("degree"),
                  "field": e.get("field"), "start": e.get("start"), "end": e.get("end")}
                 for e in _arr(raw.get("education"))[:20] if isinstance(e, dict) and (e.get("school") or "").strip()]
    certs = [{"name": (c.get("name") or "").strip(), "issuer": c.get("issuer"), "date": c.get("date")}
             for c in _arr(raw.get("certifications"))[:40] if isinstance(c, dict) and (c.get("name") or "").strip()]
    tye = raw.get("total_years_experience")
    tye = float(tye) if isinstance(tye, (int, float)) else None
    summary = raw.get("summary")
    summary = summary.strip()[:2000] if isinstance(summary, str) else None
    return {"skills": skills, "experience": experience, "education": education,
            "certifications": certs, "summary": summary, "total_years_experience": tye, "sources": ["resume"]}


def _call_llm_raw(user_prompt: str, system_prompt: str, settings: dict, provider_info: dict) -> str:
    """Call OpenAI-compatible API and return raw text content."""
    url = settings.get("api_url") or provider_info["default_url"]
    model = settings.get("model") or provider_info["default_model"]
    temperature = settings.get("temperature", 0.4)
    max_tokens = settings.get("max_tokens", 800)

    headers = {"Content-Type": "application/json"}
    api_key = settings.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resp = requests.post(url, headers=headers, json={
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    # Thinking models (Qwen3, DeepSeek-R1) put output in `reasoning` and leave
    # `content` empty, or wrap thinking in <think>...</think> tags.
    content = (msg.get("content") or "").strip()
    if not content and msg.get("reasoning"):
        content = msg["reasoning"].strip()
    return _strip_thinking_tags(content)


def _call_llm_raw_anthropic(user_prompt: str, system_prompt: str, settings: dict, provider_info: dict) -> str:
    """Call Anthropic Messages API and return raw text content."""
    url = settings.get("api_url") or provider_info["default_url"]
    model = settings.get("model") or provider_info["default_model"]
    temperature = settings.get("temperature", 0.4)
    max_tokens = settings.get("max_tokens", 800)
    api_key = settings.get("api_key", "")

    resp = requests.post(url, headers={
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }, json={
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


# ---------------------------------------------------------------------------
# Role suggester — recommends target roles from the user's resume
# ---------------------------------------------------------------------------

ROLE_SUGGESTER_SYSTEM_PROMPT = """You are an experienced career coach reviewing a candidate's resume.

Your job is to recommend job titles for them to target. Output TWO lists:

1. **current_fit** — 6 to 10 role titles the candidate could land TODAY based on
   the experience already on their resume. These should match (or be one notch
   below) their current/most-recent seniority. Include obvious peer roles and
   adjacent specializations they're directly qualified for.

2. **next_step** — 4 to 8 role titles that represent a NATURAL CAREER PROGRESSION
   from where they are now. These are stretch roles — typically one level up in
   seniority or scope, OR a logical pivot into an adjacent function their
   experience qualifies them to grow into.

For each role, include a one-sentence rationale grounded in specific evidence
from their resume — name the actual companies, technologies, or
responsibilities you saw.

Return ONLY valid JSON with this exact structure:

{
  "current_fit": [
    {"title": "<exact role title>", "reasoning": "<one sentence citing resume evidence>"},
    ...
  ],
  "next_step": [
    {"title": "<exact role title>", "reasoning": "<one sentence citing resume evidence>"},
    ...
  ]
}

Rules:
- Use industry-standard job titles (e.g. "VP of Engineering", "Senior Director
  of Data Science") — these need to match what real job boards post.
- Do NOT invent roles or repeat the same title across both lists.
- Do NOT output any text outside the JSON object.
"""


def suggest_roles(resume_text: str, settings: Optional[dict] = None) -> dict:
    """
    Ask the configured LLM to recommend target roles from the candidate's resume.

    Returns:
        {"current_fit": [{"title": str, "reasoning": str}, ...],
         "next_step":   [{"title": str, "reasoning": str}, ...],
         "provider": str,
         "error": Optional[str]}

    Raises nothing — failures return an `error` field so the API layer can
    surface the message to the user without crashing.
    """
    if not resume_text or len(resume_text.strip()) < 50:
        return {
            "current_fit": [], "next_step": [], "provider": "none",
            "error": "Resume is too short. Add your resume in Admin → Profile first.",
        }

    settings = settings or {}
    provider = settings.get("provider", "keyword_only")
    if provider == "keyword_only":
        return {
            "current_fit": [], "next_step": [], "provider": provider,
            "error": "Role suggestions need an LLM. Configure a provider in Admin → LLM Settings.",
        }

    provider_info = PROVIDERS.get(provider, PROVIDERS["openai_compatible"])

    # Truncate resume for prompt budget — keep the first ~6000 chars which
    # almost always covers summary + recent experience + skills.
    truncated = resume_text.strip()[:6000]
    user_prompt = (
        f"## Candidate Resume\n\n{truncated}\n\n"
        "Recommend roles per the system instructions."
    )

    # Use a slightly higher max_tokens than scoring — the response can be
    # ~14 roles × 2 fields, plus reasoning models need overhead.
    sugg_settings = dict(settings)
    sugg_settings["max_tokens"] = max(int(settings.get("max_tokens") or 0), 2000)
    sugg_settings["temperature"] = 0.4  # a bit of variety in suggestions

    try:
        if provider_info["format"] == "anthropic":
            raw = _call_llm_raw_anthropic(user_prompt, ROLE_SUGGESTER_SYSTEM_PROMPT, sugg_settings, provider_info)
        else:
            raw = _call_llm_raw(user_prompt, ROLE_SUGGESTER_SYSTEM_PROMPT, sugg_settings, provider_info)
    except Exception as exc:
        logger.error("suggest_roles LLM call failed: %s", exc)
        return {
            "current_fit": [], "next_step": [], "provider": provider,
            "error": f"LLM call failed: {exc}",
        }

    parsed = _extract_role_suggestions(raw)
    if parsed is None:
        return {
            "current_fit": [], "next_step": [], "provider": provider,
            "error": "Could not parse LLM response as JSON. Try again, or switch to a different model.",
        }

    return {
        "current_fit": parsed.get("current_fit", []),
        "next_step": parsed.get("next_step", []),
        "provider": provider,
        "error": None,
    }


def _extract_role_suggestions(raw: str) -> Optional[dict]:
    """Best-effort JSON extraction. Mirrors _parse_llm_response but accepts
    a different schema (current_fit / next_step arrays)."""
    import re
    if not raw:
        return None

    # Strip markdown fences
    if "```" in raw:
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", raw, re.DOTALL)
        if m:
            raw = m.group(1).strip()

    # Try direct parse
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Fallback: extract the first {...} that contains "current_fit"
        m = re.search(r"\{.*?\"current_fit\".*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            return None

    # Normalize: keep only items with a non-empty title
    def _clean(items) -> list[dict]:
        if not isinstance(items, list):
            return []
        out = []
        seen_titles = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title or title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            out.append({
                "title": title,
                "reasoning": str(item.get("reasoning") or "").strip(),
            })
        return out

    return {
        "current_fit": _clean(data.get("current_fit")),
        "next_step": _clean(data.get("next_step")),
    }


# ---------------------------------------------------------------------------
# Keyword fallback
# ---------------------------------------------------------------------------

def _keyword_fallback(title: str, description: str, error_reason: str) -> dict:
    """
    Keyword-weighted scoring used when the LLM is unavailable or not configured.

    Scores based on the presence of skill/role keywords in the job title and
    description, weighted by the WEIGHTED_SKILLS dict. Edit that dict to match
    your own background for better keyword-only accuracy.

    Scores are scaled so a strong-but-not-exhaustive keyword match still
    produces a meaningful result rather than a tiny fraction.
    """
    text = (title + " " + description).lower()

    hit_keywords: list[str] = []
    raw_score = 0
    for keyword, weight in WEIGHTED_SKILLS.items():
        if keyword in text:
            raw_score += weight
            hit_keywords.append(keyword)

    normalized = min(100, round((raw_score / _KEYWORD_TOTAL) * 100 * _KEYWORD_SCALE))

    return {
        "score": normalized,
        "reasoning": (
            f"Keyword-based score (LLM unavailable: {error_reason}). "
            f"Matched {len(hit_keywords)} weighted keyword(s) out of "
            f"{len(WEIGHTED_SKILLS)} tracked."
        ),
        "match_highlights": hit_keywords,
        "concerns": [
            "LLM scoring unavailable — keyword fallback used; "
            "accuracy is lower than LLM scoring"
        ],
    }


# ---------------------------------------------------------------------------
# Batch scorer
# ---------------------------------------------------------------------------

def score_batch(
    jobs: list[dict],
    profile: Optional[dict] = None,
    settings: Optional[dict] = None,
) -> list[dict]:
    """
    Score a batch of job dicts.

    Args:
        jobs:     List of job dicts. Each must have at least "title" and
                  "company". Optional fields: location, department,
                  description_snippet, description_full, id.
        profile:  Candidate profile dict (mirrors the DB profile row):
                    resume_text         — plain-text resume or summary
                    target_roles        — JSON string or list of role types
                    must_have_skills    — JSON string or list
                    nice_to_have_skills — JSON string or list
        settings: LLM provider settings dict (see score_job for keys).
                  Pass None or omit to use keyword-only fallback.

    Returns:
        List of score dicts, each with an added "job_id" key.
    """
    resume = profile.get("resume_text") if profile else None
    targets = _safe_json_list(profile.get("target_roles")) if profile else None
    musts = _safe_json_list(profile.get("must_have_skills")) if profile else None
    nices = _safe_json_list(profile.get("nice_to_have_skills")) if profile else None

    results = []
    for job in jobs:
        description = job.get("description_snippet") or job.get("description_full") or ""
        result = score_job(
            title=job.get("title", ""),
            company=job.get("company", ""),
            location=job.get("location", ""),
            department=job.get("department", ""),
            description=description,
            resume_text=resume,
            target_roles=targets,
            must_have_skills=musts,
            nice_to_have_skills=nices,
            settings=settings,
        )
        result["job_id"] = job.get("id", "")
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _safe_json_list(val) -> Optional[list]:
    """Parse a value that may be a JSON string, a list, or None into a list."""
    if val is None:
        return None
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print(__doc__)
    print("=" * 60)
    print("Running a keyword-only smoke test (no LLM needed)...")
    print()

    result = score_job(
        title="Staff AI Engineer",
        company="Acme Corp",
        location="Remote, US",
        department="AI Platform",
        description=(
            "We're looking for a Staff AI Engineer to build and operate our LLM inference "
            "infrastructure. You'll design multi-agent pipelines, own model deployment with "
            "llama.cpp and vLLM, and partner with product to ship AI features at scale. "
            "Python, FastAPI, Docker, and Kubernetes required."
        ),
        settings={"provider": "keyword_only"},
    )

    print(f"Score:      {result['score']}")
    print(f"Reasoning:  {result['reasoning']}")
    print(f"Highlights: {result['match_highlights']}")
    print(f"Concerns:   {result['concerns']}")
    print()
    print("To test with a live LLM, pass a settings dict to score_job().")
    print("See PROVIDERS dict in this file for valid provider options.")
    sys.exit(0 if result["score"] > 0 else 1)
