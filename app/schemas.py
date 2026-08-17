"""API and service data models for the local MVP."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LlmSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    # The client intentionally submits an empty value when the masked key has
    # not been changed.  Treat it as "keep the existing local key", rather
    # than rejecting the complete settings update at request validation time.
    api_key: str | None = None
    thinking_enabled: bool = True
    reasoning_effort: Literal["low", "high", "max"] = "low"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        candidate = value.strip().rstrip("/")
        parsed = urlparse(candidate)
        loopback_hosts = {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or (
            parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in loopback_hosts)
        ):
            raise ValueError("LLM 服务地址必须使用 HTTPS；仅 localhost 允许 HTTP")
        return candidate


class LlmSettingsView(BaseModel):
    base_url: str
    key_configured: bool
    api_key_masked: str
    model: str
    tested_at: datetime
    thinking_enabled: bool = True
    reasoning_effort: Literal["low", "high", "max"] = "low"


class ResumeProfile(BaseModel):
    title: str
    summary: str
    tags: list[str]
    skills: list[str]
    years_experience: float | None = Field(default=None, ge=0)
    education: str | None = None
    target_roles: list[str]
    highlights: list[str]


class ResumeConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_keyword: str = Field(min_length=1, max_length=50)
    # Keep city validation deliberately light.  The pinned collector resolves a
    # Chinese city name or a nine-digit code using its local table and BOSS's
    # live city API at collection time.
    city: str = Field(default="北京", min_length=1)
    experience: str
    degree: str
    salary: str
    pages: int = Field(default=2, ge=1, le=10)

    @field_validator("city", mode="before")
    @classmethod
    def default_city(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return "北京"
        return value.strip() if isinstance(value, str) else value


class ResumePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conditions: ResumeConditions | None = None
    monitor_enabled: bool | None = None
    retry_parse: bool | None = None


class ResumeView(BaseModel):
    id: str
    filename: str
    status: Literal["parsing", "ready", "parse_failed"]
    error_message: str | None = None
    profile: ResumeProfile | None = None
    conditions: ResumeConditions | None = None
    monitor_enabled: bool
    created_at: datetime


class BossStatus(BaseModel):
    state: Literal[
        "unknown", "ready", "login_required", "platform_limited", "chrome_unavailable", "collector_unavailable"
    ]
    message: str


class MatchStatus(BaseModel):
    status: Literal["idle", "running", "completed", "failed"] = "idle"
    stage: Literal["idle", "checking", "scraping", "filtering", "scoring", "finalizing", "completed", "failed"] = "idle"
    current_resume_id: str | None = None
    progress_current: int = 0
    progress_total: int = 0
    message: str = "等待手动开始匹配"


class MatchStartRequest(BaseModel):
    """A matching run always targets one explicitly selected resume."""

    model_config = ConfigDict(extra="forbid")
    resume_id: str = Field(min_length=1)

    @field_validator("resume_id")
    @classmethod
    def validate_resume_id(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("必须选择一份简历")
        return candidate


class CanonicalJob(BaseModel):
    id: str
    title: str
    company_name: str | None = None
    job_url: str
    jd_text: str
    city: str | None = None
    experience: str | None = None
    degree: str | None = None
    salary_text: str | None = None
    salary_min_k: float | None = None
    salary_max_k: float | None = None
    active_status_raw: str | None = None
    active_bucket: Literal["active", "recent", "inactive", "unknown"] = "unknown"
    published_at: str | None = None


class JobMarketSummary(BaseModel):
    """Aggregated collector result, following boss-scraper's job_summary.py."""

    keyword: str
    city: str
    total_jobs: int = Field(ge=0)
    total_details: int = Field(ge=0)
    salary_ranges: list[tuple[str, int]] = Field(default_factory=list)
    experience: list[tuple[str, int]] = Field(default_factory=list)
    degrees: list[tuple[str, int]] = Field(default_factory=list)
    districts: list[tuple[str, int]] = Field(default_factory=list)
    companies: list[tuple[str, int]] = Field(default_factory=list)
    skill_tags: list[tuple[str, int]] = Field(default_factory=list)
    jd_terms: list[tuple[str, int]] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    pages: int = Field(default=2, ge=1, le=10)
    formatted_summary: str


class ScoredJob(BaseModel):
    job_id: str
    score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1)
    strengths: list[str] = Field(max_length=5)
    gaps: list[str] = Field(max_length=5)


class MatchResult(BaseModel):
    job_id: str
    pool: Literal["new_published", "new_active"]
    rank: int = Field(ge=1)
    score: int = Field(ge=0, le=100)
    title: str
    company_name: str | None = None
    job_url: str
    city: str | None = None
    experience: str | None = None
    degree: str | None = None
    salary: str | None = None
    active_status: str | None = None
    reason: str
    strengths: list[str]
    gaps: list[str]
