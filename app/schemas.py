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
    api_key: str | None = Field(default=None, min_length=1)

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
    city: str = Field(min_length=1)
    experience: str
    degree: str
    salary: str


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


class ScoredJob(BaseModel):
    job_id: str
    score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1)
    strengths: list[str] = Field(max_length=5)
    gaps: list[str] = Field(max_length=5)


class MatchResult(BaseModel):
    job_id: str
    pool: Literal["new_published", "new_active"]
    rank: int = Field(ge=1, le=10)
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
