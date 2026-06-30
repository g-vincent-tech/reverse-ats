"""Master-résumé template tailoring.

Treats the user's polished résumé (markdown) as the source of truth and only
surgically edits the agreed-upon fields per job:
  1. TARGET TITLE  → the JD's exact title
  2. SUMMARY       → append one true sentence mirroring the JD's must-haves
  3. CORE SKILLS   → reorder groups/keywords to the JD (no fabrication)
Everything else (projects, experience, education, certs) is kept verbatim, and
the "HOW TO TAILOR" cheat-sheet block is stripped.
"""

from __future__ import annotations

import re

# Markers that begin the tail "cheat-sheet" the user keeps in their master file.
_CHEATSHEET_MARKERS = (
    "<!-- ====",
    "<!--====",
    "▼▼▼ TAILORING",
    "TAILORING CHEAT-SHEET",
    "HOW TO TAILOR THIS PER JOB",
    "## ▼ HOW TO TAILOR",
)

_TITLE_PLACEHOLDER = re.compile(r"\[?\s*TARGET TITLE.*?\]?$", re.IGNORECASE)


def strip_cheatsheet(text: str) -> str:
    cut = len(text)
    for marker in _CHEATSHEET_MARKERS:
        p = text.find(marker)
        if p != -1:
            cut = min(cut, p)
    return text[:cut].rstrip()


def looks_like_master(text: str) -> bool:
    """Heuristic: a structured master résumé has markdown section headers."""
    if not text:
        return False
    t = text.upper()
    return ("## SUMMARY" in t or "\nSUMMARY" in t) and ("EXPERIENCE" in t) and ("## " in text or "**" in text)


def _is_contact_line(ln: str) -> bool:
    s = ln.strip().strip("*")
    has_sep = "·" in s or "|" in s
    has_contact = ("@" in s) or ("linkedin" in s.lower()) or bool(re.search(r"\(?\d{3}\)?[\s.-]?\d{3}", s))
    return has_sep and has_contact


def _is_title_placeholder(ln: str) -> bool:
    return "TARGET TITLE" in ln.upper()


def split_master(text: str):
    """Return (name, contact, title_is_placeholder, sections).

    sections = ordered list of (heading, [body_lines]); heading is the text
    after '## '. The preamble before the first '## ' yields name + contact.
    """
    text = strip_cheatsheet(text)
    name, contact = "", ""
    sections: list[tuple[str, list[str]]] = []
    cur_head: str | None = None
    cur_body: list[str] = []
    header_lines: list[str] = []
    in_header = True

    for ln in text.splitlines():
        if ln.lstrip().startswith("## "):
            in_header = False
            if cur_head is not None:
                sections.append((cur_head, cur_body))
            cur_head = ln.lstrip()[3:].strip()
            cur_body = []
            continue
        (header_lines if in_header else cur_body).append(ln)
    if cur_head is not None:
        sections.append((cur_head, cur_body))

    for ln in header_lines:
        s = ln.strip()
        if s.startswith("# ") and not name:
            name = s[2:].strip()
        elif _is_contact_line(s) and not contact:
            contact = s.strip().strip("*").strip()
    return name, contact, sections


def find_section(sections, *needles):
    up = [n.upper() for n in needles]
    for i, (head, body) in enumerate(sections):
        h = head.upper()
        if any(n in h for n in up):
            return i
    return -1


def core_skills_text(sections) -> str:
    i = find_section(sections, "CORE SKILL", "SKILLS")
    if i == -1:
        return ""
    return "\n".join(l for l in sections[i][1] if l.strip() and l.strip() != "---")


def summary_text(sections) -> str:
    i = find_section(sections, "SUMMARY", "PROFILE")
    if i == -1:
        return ""
    return "\n".join(l for l in sections[i][1] if l.strip() and l.strip() != "---").strip()


# Phrases the user does not want in generated résumés, regardless of the master:
#  - "F500"/"Fortune 500" qualifier
#  - estimated cost-savings claims (not actual figures) e.g. "...that eliminated an
#    estimated $18K+/month in equivalent cloud AI cost"
_SCRUB_PATTERNS = (
    re.compile(r"\b(?:F500|Fortune\s*500)\b\s*", re.IGNORECASE),
    re.compile(r"\s*(?:[—–-]\s*)?(?:that |which )?(?:eliminated|saved|cut|avoided|reduced)[^.\n]*?estimated\s+\$[^.\n]*", re.IGNORECASE),
    re.compile(r"\s*(?:[—–-]\s*)?(?:an?\s+)?estimated\s+\$[\d.,]+\s*[KMB]?\+?\s*/?\s*month[^.\n]*", re.IGNORECASE),
)


def scrub_phrases(text: str) -> str:
    """Remove unwanted phrases while preserving line formatting (for verbatim sections)."""
    s = text or ""
    for p in _SCRUB_PATTERNS:
        s = p.sub("", s)
    return s


def scrub(text: str) -> str:
    """Phrase removal + whitespace normalization (for the rewritten summary)."""
    return re.sub(r"\s{2,}", " ", scrub_phrases(text)).strip()


def apply_tailoring(sections, target_title: str, new_summary: str, core_skills_lines):
    """Return a new sections list with SUMMARY (full grounded rewrite) + CORE SKILLS replaced."""
    out = []
    for head, body in sections:
        h = head.upper()
        if "SUMMARY" in h or "PROFILE" in h:
            base = " ".join(l.strip() for l in body if l.strip() and l.strip() != "---").strip()
            summ = (new_summary or "").strip() or base
            out.append((head, [scrub(summ)]))
        elif ("CORE SKILL" in h or h.strip() == "SKILLS") and core_skills_lines:
            out.append((head, [scrub_phrases(l) for l in core_skills_lines]))
        else:
            # Verbatim sections (projects, experience, education, certs) — but still
            # strip the user's unwanted phrases (F500, estimated cost savings).
            out.append((head, [scrub_phrases(l) for l in body]))
    return out
