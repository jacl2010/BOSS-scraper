import time

import fitz
import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.boss import CollectedJobs
from app.main import create_app
from app.schemas import (
    BossStatus,
    CanonicalJob,
    JobMarketSummary,
    LlmSettingsInput,
    LlmSettingsView,
    ResumeProfile,
    ScoredJob,
)


def pdf_bytes(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    content = document.tobytes()
    document.close()
    return content


class FakeLlm:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.parse_calls = 0
        self.score_calls = 0

    def test_and_save(self, settings: LlmSettingsInput, api_key=None) -> LlmSettingsView:
        saved = self.database.save_llm_settings(settings)
        return LlmSettingsView(**saved, key_configured=True, api_key_masked="***")

    def current_settings(self):
        settings = self.database.get_llm_settings()
        if not settings:
            raise ValueError("请先配置 LLM")
        return settings

    def parse_resume(self, settings, text):
        self.parse_calls += 1
        return ResumeProfile(
            title=f"候选人 {self.parse_calls}",
            summary=text,
            tags=["Python"],
            skills=["FastAPI"],
            years_experience=3,
            education="本科",
            target_roles=["Python 工程师"],
            highlights=["交付本地服务"],
        )

    def score_jobs(self, settings, profile, jobs):
        self.score_calls += 1
        return [
            ScoredJob(job_id=job.id, score=92, reason="技术栈与经验匹配", strengths=["Python"], gaps=[])
            for job in jobs
        ]


class FakeBoss:
    def __init__(self) -> None:
        self.status_calls = 0
        self.collect_calls = 0

    def status(self):
        self.status_calls += 1
        return BossStatus(state="ready", message="fixture ready")

    def setup(self):
        return BossStatus(state="login_required", message="fixture setup")

    def collect(self, conditions):
        self.collect_calls += 1
        jobs = [
            CanonicalJob(
                id="job-fixture",
                title="Python 工程师",
                company_name="示例公司",
                job_url="https://www.zhipin.com/job_detail/job-fixture.html",
                jd_text="Python FastAPI 后端开发",
                city="上海",
                experience="3-5年",
                degree="本科",
                salary_text="20-30K",
                salary_min_k=20,
                salary_max_k=30,
                active_status_raw="刚刚活跃",
                active_bucket="active",
            )
        ]
        return CollectedJobs(
            jobs,
            JobMarketSummary(
                keyword=conditions.job_keyword,
                city=conditions.city,
                total_jobs=1,
                total_details=1,
                salary_ranges=[("20-30K", 1)],
                experience=[("3-5年", 1)],
                degrees=[("本科", 1)],
                districts=[],
                companies=[("示例公司", 1)],
                skill_tags=[("Python", 1)],
                jd_terms=[("FastAPI", 1)],
                formatted_summary="岗位市场摘要: Python @ 上海",
            ),
        )


@pytest.mark.parametrize(
    ("error_type", "expected_message"),
    [
        (openai.AuthenticationError, "LLM API Key 无效或已失效"),
        (openai.PermissionDeniedError, "LLM API Key 没有访问该模型的权限"),
        (openai.NotFoundError, "未找到 LLM 服务地址或模型"),
        (openai.BadRequestError, "LLM 请求参数不被服务商接受"),
        (openai.RateLimitError, "LLM 请求过于频繁或额度不足"),
        (openai.APIConnectionError, "无法连接 LLM 服务"),
        (openai.APITimeoutError, "LLM 服务响应超时"),
    ],
)
def test_llm_configuration_returns_safe_provider_error_messages(tmp_path, error_type, expected_message):
    database = Database(tmp_path / "app.db", tmp_path / "resumes")
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    if error_type in {openai.APIConnectionError, openai.APITimeoutError}:
        provider_error = error_type(request=request)
    else:
        response = httpx.Response(400, request=request)
        provider_error = error_type("Authorization: sk-sensitive-provider-message", response=response, body=None)

    class FailingLlm(FakeLlm):
        def test_and_save(self, settings, api_key=None):
            raise provider_error

    client = TestClient(create_app(database=database, llm=FailingLlm(database)))
    response = client.put(
        "/api/llm-settings",
        json={"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash", "api_key": "sk-user-secret"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == expected_message
    assert "sk-user-secret" not in response.text
    assert "sk-sensitive-provider-message" not in response.text


def test_fake_api_core_loop_is_isolated_and_idle_has_zero_external_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_MATCHER_LLM_API_KEY", "never-serialized")
    database = Database(tmp_path / "app.db", tmp_path / "resumes")
    llm = FakeLlm(database)
    boss = FakeBoss()
    client = TestClient(create_app(database=database, llm=llm, boss=boss))

    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/matches/status").json()["status"] == "idle"
    assert boss.status_calls == boss.collect_calls == llm.score_calls == 0

    settings = client.put(
        "/api/llm-settings",
        json={"base_url": "https://example.test/v1", "model": "test-model"},
    )
    assert settings.status_code == 200
    assert settings.json()["key_configured"] is True
    assert "never-serialized" not in settings.text

    first = client.post(
        "/api/resumes",
        files={"file": ("first.pdf", pdf_bytes("Python first resume"), "application/pdf")},
    )
    second = client.post(
        "/api/resumes",
        files={"file": ("second.pdf", pdf_bytes("Python second resume"), "application/pdf")},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    conditions = {
        "job_keyword": "Python",
        "city": "上海",
        "experience": "3-5年",
        "degree": "本科",
        "salary": "20-30K",
    }
    assert client.patch(
        f"/api/resumes/{first.json()['id']}",
        json={"conditions": conditions, "monitor_enabled": True},
    ).status_code == 200
    assert client.patch(
        f"/api/resumes/{second.json()['id']}",
        json={"conditions": conditions, "monitor_enabled": False},
    ).status_code == 200

    started = client.post("/api/matches")
    assert started.status_code == 202
    for _ in range(100):
        status = client.get("/api/matches/status").json()
        if status["status"] != "running":
            break
        time.sleep(0.01)
    assert status["status"] == "completed"
    assert boss.collect_calls == 1
    assert llm.score_calls == 1

    first_results = client.get(f"/api/resumes/{first.json()['id']}/results").json()
    second_results = client.get(f"/api/resumes/{second.json()['id']}/results").json()
    assert [item["job_id"] for item in first_results["new_published"]] == ["job-fixture"]
    assert first_results["new_active"] == []
    assert first_results["collection_summary"]["total_details"] == 1
    assert first_results["collection_summary"]["formatted_summary"] == "岗位市场摘要: Python @ 上海"
    assert second_results["new_published"] == second_results["new_active"] == []
    assert "never-serialized" not in repr(database.get_llm_settings())
