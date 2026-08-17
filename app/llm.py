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
        return "***"

    def _client(self, settings: LlmSettingsInput | dict, api_key: str | None = None) -> ChatOpenAI:
        base_url = settings["base_url"] if isinstance(settings, dict) else settings.base_url
        model = settings["model"] if isinstance(settings, dict) else settings.model
        thinking_enabled = settings.get("thinking_enabled", True) if isinstance(settings, dict) else settings.thinking_enabled
        reasoning_effort = settings.get("reasoning_effort", "low") if isinstance(settings, dict) else settings.reasoning_effort
        return ChatOpenAI(
            base_url=base_url,
            model=model,
            api_key=self._api_key(api_key), temperature=0, timeout=30,
            reasoning_effort=reasoning_effort,
            extra_body={"thinking": {"type": "enabled" if thinking_enabled else "disabled"}},
        )

    def test_and_save(self, settings: LlmSettingsInput, api_key: str | None = None) -> LlmSettingsView:
        # A blank API key means the UI is keeping the existing masked key.  It
        # must use the key already held in the local environment and never
        # rewrite the .env file with an empty value.
        new_api_key = api_key.strip() if api_key is not None else None
        candidate = self._api_key(new_api_key or None)
        self.test_settings(settings, candidate)
        if new_api_key:
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

    def _invoke_structured(self, settings: dict, schema: type[BaseModel], prompt: str) -> BaseModel:
        chain = self._client(settings).with_structured_output(schema, method="json_mode")
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        instructed_prompt = (
            f"{prompt}\n\n只返回一个符合以下 JSON Schema 的 JSON 对象，不要添加 Markdown 或解释：\n"
            f"{schema_json}"
        )
        try:
            return chain.invoke(instructed_prompt)
        except Exception:
            return chain.invoke(instructed_prompt + "\n请严格遵守字段名称和字段类型，不要省略必填字段。")

    def parse_resume(self, settings: dict, text: str) -> ResumeProfile:
        prompt = _prompt("resume_parse_v1.md").replace("{{resume_text}}", text)
        return self._invoke_structured(settings, ResumeProfile, prompt)

    def score_jobs(self, settings: dict, profile: ResumeProfile, jobs: list[CanonicalJob]) -> list[ScoredJob]:
        if not jobs:
            return []
        prompt = _prompt("job_match_v1.md")
        prompt = prompt.replace("{{resume_profile}}", profile.model_dump_json())
        prompt = prompt.replace("{{jobs}}", json.dumps([job.model_dump() for job in jobs], ensure_ascii=False))
        response = self._invoke_structured(settings, _ScoredBatch, prompt)
        valid_ids = {job.id for job in jobs}
        return [item for item in response.results if item.job_id in valid_ids]
