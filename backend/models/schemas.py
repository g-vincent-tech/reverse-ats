"""
Pydantic v2 models for Reverse ATS FastAPI application.
All datetime fields use ISO-format strings for SQLite compatibility.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PipelineStage(str, Enum):
    discovered = "discovered"
    saved = "saved"
    applied = "applied"
    phone_screen = "phone_screen"
    technical = "technical"
    final = "final"
    offer = "offer"
    rejected = "rejected"
    withdrawn = "withdrawn"


# ---------------------------------------------------------------------------
# Job models
# ---------------------------------------------------------------------------

class JobOut(BaseModel):
    id: str
    company: str
    title: str
    location: Optional[str] = None
    department: Optional[str] = None
    team: Optional[str] = None
    url: str
    description_snippet: Optional[str] = None
    description_full: Optional[str] = None
    category: Optional[str] = None
    ats_type: Optional[str] = None
    remote: bool = False
    # Ashby's `workplaceType`: "OnSite" | "Remote" | "Hybrid". Finer than the
    # legacy boolean `remote`. NULL for ATS that don't expose it.
    workplace_type: Optional[str] = None
    # Ashby's `employmentType`: "FullTime" | "PartTime" | "Intern" | "Contract" | "Temporary".
    employment_type: Optional[str] = None
    # Salary range (annual). Sourced from Ashby `summaryComponents.Salary`,
    # which already collapses geographic compensation tiers into a single
    # min/max range. NULL when the employer hasn't disclosed comp.
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: Optional[str] = None
    # Human-readable summary the employer publishes (Ashby's
    # `compensationTierSummary`, e.g. "$211.4K – $290.6K • Offers Equity").
    comp_summary: Optional[str] = None
    keyword_score: int = 0
    llm_score: Optional[int] = None
    llm_reasoning: Optional[str] = None
    first_seen_at: str
    last_seen_at: str
    expired: bool = False
    dismissed: bool = False
    # Joined from pipeline table — present only when a pipeline entry exists
    pipeline_stage: Optional[PipelineStage] = None

    model_config = {"from_attributes": True}

    @field_validator("remote", "expired", "dismissed", mode="before")
    @classmethod
    def coerce_int_to_bool(cls, v):
        """SQLite stores booleans as 0/1 integers."""
        if isinstance(v, int):
            return bool(v)
        return v


class JobListResponse(BaseModel):
    jobs: list[JobOut]
    total: int
    page: int
    per_page: int


class JobDismiss(BaseModel):
    dismissed: bool = True


# ---------------------------------------------------------------------------
# Pipeline models
# ---------------------------------------------------------------------------

class PipelineCreate(BaseModel):
    job_id: str
    stage: PipelineStage = PipelineStage.saved
    notes: Optional[str] = None


class PipelineUpdate(BaseModel):
    stage: Optional[PipelineStage] = None
    notes: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_role: Optional[str] = None
    next_step: Optional[str] = None
    next_step_date: Optional[str] = None   # ISO date string e.g. "2026-05-01"
    salary_offered: Optional[int] = None
    cover_letter: Optional[str] = None


class PipelineOut(BaseModel):
    id: int
    job_id: str
    stage: PipelineStage
    applied_at: Optional[str] = None
    notes: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_role: Optional[str] = None
    next_step: Optional[str] = None
    next_step_date: Optional[str] = None
    salary_offered: Optional[int] = None
    cover_letter: Optional[str] = None
    created_at: str
    updated_at: str
    # Populated when the query joins job data
    job: Optional[JobOut] = None

    model_config = {"from_attributes": True}


class PipelineEventOut(BaseModel):
    id: int
    pipeline_id: int
    from_stage: Optional[str] = None
    to_stage: str
    note: Optional[str] = None
    created_at: str

    model_config = {"from_attributes": True}


class PipelineListResponse(BaseModel):
    items: list[PipelineOut]
    # Convenience grouping: stage value → list of items in that stage
    by_stage: dict[str, list[PipelineOut]]


# ---------------------------------------------------------------------------
# Profile models
# ---------------------------------------------------------------------------

class ProfileUpdate(BaseModel):
    resume_text: Optional[str] = None
    target_roles: Optional[list[str]] = None
    target_locations: Optional[list[str]] = None
    remote_only: Optional[bool] = None
    min_seniority: Optional[str] = None       # e.g. "senior", "staff", "principal"
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    must_have_skills: Optional[list[str]] = None
    nice_to_have_skills: Optional[list[str]] = None
    blacklisted_companies: Optional[list[str]] = None
    blacklisted_keywords: Optional[list[str]] = None
    priority_categories: Optional[list[str]] = None


class ProfileOut(BaseModel):
    resume_text: Optional[str] = None
    target_roles: list[str] = Field(default_factory=list)
    target_locations: list[str] = Field(default_factory=list)
    remote_only: bool = True
    min_seniority: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    blacklisted_companies: list[str] = Field(default_factory=list)
    blacklisted_keywords: list[str] = Field(default_factory=list)
    priority_categories: list[str] = Field(default_factory=list)
    updated_at: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator(
        "target_roles", "target_locations", "must_have_skills",
        "nice_to_have_skills", "blacklisted_companies", "blacklisted_keywords",
        "priority_categories",
        mode="before",
    )
    @classmethod
    def parse_json_list(cls, v):
        """SQLite stores list fields as JSON strings; parse them on read."""
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                return []
        if v is None:
            return []
        return v

    @field_validator("remote_only", mode="before")
    @classmethod
    def coerce_int_to_bool(cls, v):
        if isinstance(v, int):
            return bool(v)
        return v


# ---------------------------------------------------------------------------
# Analytics models
# ---------------------------------------------------------------------------

class FunnelMetric(BaseModel):
    stage: str
    count: int


class AnalyticsOut(BaseModel):
    funnel: list[FunnelMetric]
    total_discovered: int
    total_applied: int
    # (interviews_with_response) / total_applied, 0.0 if no applications
    response_rate: float
    # company → {"discovered": N, "applied": N, "pipeline": N}
    by_company: dict[str, dict]
    # List of {"week": "2026-W15", "discovered": N, "applied": N}
    weekly_activity: list[dict]


# ---------------------------------------------------------------------------
# Scrape run models
# ---------------------------------------------------------------------------

class ScrapeRunOut(BaseModel):
    id: int
    started_at: str
    completed_at: Optional[str] = None
    total_fetched: int = 0
    new_jobs: int = 0
    updated_jobs: int = 0
    expired_jobs: int = 0
    llm_scored: int = 0
    errors: list[str] = Field(default_factory=list)

    model_config = {"from_attributes": True}

    @field_validator("errors", mode="before")
    @classmethod
    def parse_errors_json(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                return []
        if v is None:
            return []
        return v


# ---------------------------------------------------------------------------
# Company models
# ---------------------------------------------------------------------------

class CompanyCreate(BaseModel):
    name: str
    ats: str = "greenhouse"
    slug: str
    category: str = "fintech"
    enabled: bool = True
    careers_url: Optional[str] = None
    workday_url: Optional[str] = None

class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    ats: Optional[str] = None
    slug: Optional[str] = None
    category: Optional[str] = None
    enabled: Optional[bool] = None
    careers_url: Optional[str] = None
    workday_url: Optional[str] = None

class CompanyOut(BaseModel):
    id: int
    name: str
    ats: str
    slug: str
    category: str
    enabled: bool = True
    careers_url: Optional[str] = None
    workday_url: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}

    @field_validator("enabled", mode="before")
    @classmethod
    def coerce_int_to_bool(cls, v):
        if isinstance(v, int):
            return bool(v)
        return v


# ---------------------------------------------------------------------------
# LLM Settings models
# ---------------------------------------------------------------------------

class LLMSettingsUpdate(BaseModel):
    provider: Optional[str] = None      # openai, anthropic, ollama, openai_compatible, keyword_only
    api_key: Optional[str] = None
    api_url: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

class LLMSettingsOut(BaseModel):
    provider: str = "keyword_only"
    api_key: Optional[str] = None       # will be masked in response
    api_url: Optional[str] = None
    model: Optional[str] = None
    temperature: float = 0.1
    max_tokens: int = 500
    updated_at: Optional[str] = None

    model_config = {"from_attributes": True}
