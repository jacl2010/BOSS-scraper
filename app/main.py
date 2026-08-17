"""Loopback-only FastAPI entry point for the local MVP."""

from __future__ import annotations

import json
import os
from pathlib import Path

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.boss import BossAdapter
from app.database import Database
from app.llm import LlmService
from app.matching import MatchRunner
from app.resumes import ResumeError, ResumeService
from app.schemas import LlmSettingsInput, LlmSettingsView, MatchStartRequest, ResumePatch


def _error(status: int, message: str, code: str = "invalid_request") -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _llm_configuration_message(error: Exception) -> str:
    messages = (
        (AuthenticationError, "LLM API Key 无效或已失效"),
        (PermissionDeniedError, "LLM API Key 没有访问该模型的权限"),
        (NotFoundError, "未找到 LLM 服务地址或模型"),
        (BadRequestError, "LLM 请求参数不被服务商接受"),
        (RateLimitError, "LLM 请求过于频繁或额度不足"),
        (APITimeoutError, "LLM 服务响应超时"),
        (APIConnectionError, "无法连接 LLM 服务"),
    )
    for error_type, message in messages:
        if isinstance(error, error_type):
            return message
    return "LLM 配置测试失败，请检查地址、模型和环境变量"


def create_app(database: Database | None = None, llm=None, boss=None) -> FastAPI:
    app = FastAPI(title="BOSS Resume Matcher", docs_url=None, redoc_url=None)
    database = database or Database()
    llm = llm or LlmService(database)
    resumes = ResumeService(database, llm)
    boss = boss or BossAdapter()
    runner = MatchRunner(database, resumes, boss, llm)
    app.state.database, app.state.llm, app.state.resumes, app.state.boss, app.state.runner = database, llm, resumes, boss, runner

    @app.exception_handler(HTTPException)
    async def api_error_handler(request, exc: HTTPException):
        if isinstance(exc.detail, dict) and {"code", "message"} <= set(exc.detail):
            return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": "invalid_request", "message": "请求无效"}})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, exc: RequestValidationError):
        messages = [str(error.get("msg", "")) for error in exc.errors()]
        message = "；".join(item.removeprefix("Value error, ") for item in messages if item) or "请求字段无效"
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": message}})

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/llm-settings")
    def get_llm_settings():
        settings = database.get_llm_settings()
        if not settings:
            return None
        return LlmSettingsView(
            **settings,
            key_configured=bool(os.environ.get("BOSS_MATCHER_LLM_API_KEY", "").strip()),
            api_key_masked=LlmService.mask_key(os.environ.get("BOSS_MATCHER_LLM_API_KEY")),
        )

    @app.put("/api/llm-settings")
    def put_llm_settings(payload: LlmSettingsInput):
        try:
            return llm.test_and_save(payload, payload.api_key)
        except ValueError as exc:
            message = str(exc)
            if "BOSS_MATCHER_LLM_API_KEY" not in message:
                message = _llm_configuration_message(exc)
            raise _error(422, message, "llm_configuration_failed") from exc
        except Exception as exc:
            raise _error(422, _llm_configuration_message(exc), "llm_configuration_failed") from exc

    @app.get("/api/resumes")
    def list_resumes():
        return resumes.list()

    @app.post("/api/resumes", status_code=201)
    async def upload_resume(file: UploadFile = File(...)):
        try:
            return resumes.upload(file.filename or "resume.pdf", await file.read())
        except ResumeError as exc:
            raise _error(409 if "重复" in str(exc) else 422, str(exc), "invalid_resume") from exc

    @app.patch("/api/resumes/{resume_id}")
    def patch_resume(resume_id: str, payload: ResumePatch):
        try:
            return resumes.update(resume_id, payload)
        except KeyError as exc:
            raise _error(404, "简历不存在", "resume_not_found") from exc
        except ResumeError as exc:
            raise _error(422, str(exc), "invalid_resume") from exc

    @app.delete("/api/resumes/{resume_id}", status_code=204)
    def delete_resume(resume_id: str):
        try:
            resumes.delete(resume_id)
        except KeyError as exc:
            raise _error(404, "简历不存在", "resume_not_found") from exc

    @app.get("/api/boss/status")
    def boss_status():
        return boss.status()

    @app.post("/api/boss/setup")
    def boss_setup():
        return boss.setup()

    @app.post("/api/matches", status_code=202)
    def start_matches(payload: MatchStartRequest):
        try:
            return runner.start(payload.resume_id)
        except RuntimeError as exc:
            raise _error(409, str(exc), "match_running") from exc
        except ValueError as exc:
            raise _error(422, str(exc), "no_eligible_resume") from exc

    @app.get("/api/matches/status")
    def match_status():
        return runner.status()

    @app.get("/api/resumes/{resume_id}/results")
    def resume_results(resume_id: str):
        if database.get_resume_row(resume_id) is None:
            raise _error(404, "简历不存在", "resume_not_found")
        rows = database.get_results(resume_id)
        collection_summary = database.get_collection_summary(resume_id)
        last_completed_at = database.get_match_completion(resume_id)
        pools = {"new_published": [], "new_active": []}
        for row in rows:
            pools[row["pool"]].append({
                "job_id": row["job_id"], "pool": row["pool"], "rank": row["rank"], "score": row["score"],
                "title": row["title"], "company_name": row["company_name"], "job_url": row["job_url"],
                "jd_text": row["jd_text"],
                "city": row["city"], "experience": row["experience"], "degree": row["degree"], "salary": row["salary_text"],
                "active_status": row["active_status_raw"],
                "active_status_raw": row["active_status_raw"],
                "boss_active_status": row["active_status_raw"],
                "reason": row["reason"],
                "strengths": json.loads(row["strengths_json"]), "gaps": json.loads(row["gaps_json"]),
            })
        return {
            "resume_id": resume_id,
            "matched_at": rows[0]["matched_at"] if rows else None,
            "last_completed_at": last_completed_at,
            "collection_summary": collection_summary,
            **pools,
        }

    @app.get("/api/resumes/{resume_id}/collection-summary")
    def resume_collection_summary(resume_id: str):
        if database.get_resume_row(resume_id) is None:
            raise _error(404, "简历不存在", "resume_not_found")
        return {"resume_id": resume_id, "collection_summary": database.get_collection_summary(resume_id)}

    web_dir = Path(__file__).parent / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


app = create_app()
