"""Two small structured LangChain calls; keys never leave process environment."""

from __future__ import annotations

import json
import os
from pathlib import Path

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.database import Database
from app.schemas import CanonicalJob, LlmSettingsInput, LlmSettingsView, ResumeProfile, ScoredJob


class _Probe(BaseModel):
    ok: bool


class _ScoredBatch(BaseModel):
    results: list[ScoredJob]


def _prompt(name: str) -> str:
    return (Path(__file__).parent / "prompts" / name).read_text(encoding="utf-8")


class LlmService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _api_key() -> str:
        key = os.environ.get("BOSS_MATCHER_LLM_API_KEY", "").strip()
        if not key:
            raise ValueError("未检测到 BOSS_MATCHER_LLM_API_KEY 环境变量")
        return key

    def _client(self, settings: LlmSettingsInput | dict) -> ChatOpenAI:
        return ChatOpenAI(
            base_url=settings["base_url"] if isinstance(settings, dict) else settings.base_url,
            model=settings["model"] if isinstance(settings, dict) else settings.model,
            api_key=self._api_key(), temperature=0, timeout=30,
        )

    def test_and_save(self, settings: LlmSettingsInput) -> LlmSettingsView:
        self.test_settings(settings)
        saved = self.database.save_llm_settings(settings)
        return LlmSettingsView(**saved, key_configured=True)

    def test_settings(self, settings: LlmSettingsInput) -> bool:
        result = self._client(settings).with_structured_output(_Probe).invoke(
            "Return the JSON object {\"ok\": true}. Do not include sensitive information."
        )
        if not result.ok:
            raise ValueError("LLM 配置测试未通过")
        return True

    def current_settings(self) -> dict:
        settings = self.database.get_llm_settings()
        if not settings:
            raise ValueError("请先测试并保存 LLM 配置")
        return settings

    def parse_resume(self, settings: dict, text: str) -> ResumeProfile:
        chain = self._client(settings).with_structured_output(ResumeProfile)
        prompt = _prompt("resume_parse_v1.md").replace("{{resume_text}}", text)
        try:
            return chain.invoke(prompt)
        except Exception:
            return chain.invoke(prompt + "\n请只返回符合结构的 JSON，不要省略字段。")

    def score_jobs(self, settings: dict, profile: ResumeProfile, jobs: list[CanonicalJob]) -> list[ScoredJob]:
        if not jobs:
            return []
        chain = self._client(settings).with_structured_output(_ScoredBatch)
        prompt = _prompt("job_match_v1.md")
        prompt = prompt.replace("{{resume_profile}}", profile.model_dump_json())
        prompt = prompt.replace("{{jobs}}", json.dumps([job.model_dump() for job in jobs], ensure_ascii=False))
        try:
            response = chain.invoke(prompt)
        except Exception:
            response = chain.invoke(prompt + "\n请严格输出指定 JSON 结构。")
        valid_ids = {job.id for job in jobs}
        return [item for item in response.results if item.job_id in valid_ids]
