"""LLM calls; keys never leave process environment."""

from __future__ import annotations

import json
import os
from pathlib import Path

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.database import Database
from app.schemas import CanonicalJob, LlmSettingsInput, LlmSettingsView, ResumeProfile, ScoredJob


class _ScoredBatch(BaseModel):
    results: list[ScoredJob]


def _prompt(name: str) -> str:
    return (Path(__file__).parent / "prompts" / name).read_text(encoding="utf-8")


class LlmService:
    ENV_NAME = "BOSS_MATCHER_LLM_API_KEY"

    def __init__(self, database: Database, env_path: Path | str | None = None) -> None:
        self.database = database
        self.env_path = Path(env_path) if env_path else Path(__file__).resolve().parent.parent / ".env"
        self._load_env_file()

    def _load_env_file(self) -> None:
        if not self.env_path.exists() or os.environ.get(self.ENV_NAME, "").strip():
            return
        for raw_line in self.env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() != self.ENV_NAME:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if value:
                os.environ.setdefault(self.ENV_NAME, value)
            return

    @classmethod
    def _api_key(cls, candidate: str | None = None) -> str:
        key = (candidate if candidate is not None else os.environ.get(cls.ENV_NAME, "")).strip()
        if not key:
            raise ValueError(f"未检测到 {cls.ENV_NAME} 环境变量")
        if "\n" in key or "\r" in key:
            raise ValueError("API Key 格式无效，不能包含换行")
        return key

    @staticmethod
    def mask_key(key: str | None) -> str:
        if not key:
            return ""
        return f"{key[:3]}****{key[-4:]}" if len(key) > 7 else "****"

    def _client(self, settings: LlmSettingsInput | dict, api_key: str | None = None) -> ChatOpenAI:
        return ChatOpenAI(
            base_url=settings["base_url"] if isinstance(settings, dict) else settings.base_url,
            model=settings["model"] if isinstance(settings, dict) else settings.model,
            api_key=self._api_key(api_key), temperature=0, timeout=30,
        )

    def test_and_save(self, settings: LlmSettingsInput, api_key: str | None = None) -> LlmSettingsView:
        candidate = self._api_key(api_key)
        self.test_settings(settings, candidate)
        if api_key is not None:
            self._write_env_key(candidate)
            os.environ[self.ENV_NAME] = candidate
        saved = self.database.save_llm_settings(settings)
        return LlmSettingsView(**saved, key_configured=True, api_key_masked=self.mask_key(candidate))

    def test_settings(self, settings: LlmSettingsInput, api_key: str | None = None) -> bool:
        self._client(settings, api_key).invoke("Reply with exactly: OK")
        return True

    def _write_env_key(self, key: str) -> None:
        lines = self.env_path.read_text(encoding="utf-8").splitlines() if self.env_path.exists() else []
        replacement = f"{self.ENV_NAME}={key}"
        replaced = False
        output: list[str] = []
        for line in lines:
            if line.strip().startswith(f"{self.ENV_NAME}="):
                if not replaced:
                    output.append(replacement)
                    replaced = True
                continue
            output.append(line)
        if not replaced:
            output.append(replacement)
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        self.env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
        try:
            self.env_path.chmod(0o600)
        except OSError:
            pass

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
