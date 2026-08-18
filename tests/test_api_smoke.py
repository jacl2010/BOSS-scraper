import time
from pathlib import Path

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


def test_v_cloak_keeps_unmounted_vue_template_hidden():
    css = (Path(__file__).parents[1] / "app" / "web" / "app.css").read_text(encoding="utf-8")

    assert "#app[v-cloak] { display:none; }" in css


def test_select_menu_constrains_its_popover_to_the_visible_viewport():
    script = (Path(__file__).parents[1] / "app" / "web" / "app.js").read_text(encoding="utf-8")
    css = (Path(__file__).parents[1] / "app" / "web" / "app.css").read_text(encoding="utf-8")

    assert "syncPopoverPosition" in script
    assert "open-upward" in script
    assert ".select-menu.open-upward .select-popover" in css


def test_match_page_new_active_tab_filters_to_just_active_jobs():
    script = (Path(__file__).parents[1] / "app" / "web" / "app.js").read_text(encoding="utf-8")
    template = (Path(__file__).parents[1] / "app" / "web" / "index.html").read_text(encoding="utf-8")

    assert "newActiveResults()" in script
    assert "this.jobActiveStatus(item) === '刚刚活跃'" in script
    assert "{{ newActiveResults.length }}" in template


def test_match_page_splits_monitor_and_match_signal_tracks():
    script = (Path(__file__).parents[1] / "app" / "web" / "app.js").read_text(encoding="utf-8")
    template = (Path(__file__).parents[1] / "app" / "web" / "index.html").read_text(encoding="utf-8")

    assert "monitorStageLabel" in script and "matchStageLabel" in script
    assert "startMonitor" in script and "saveMonitorConditions" in script
    assert "{{ matchStatus.task }}" not in template
    assert "执行监控" in template
    assert "conditionForm" not in script


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
        self.last_conditions = None
        self.jd_text = "Python FastAPI 后端开发"
        self.jobs = None

    def status(self):
        self.status_calls += 1
        return BossStatus(state="ready", message="fixture ready")

    def setup(self):
        return BossStatus(state="login_required", message="fixture setup")

    def collect(self, conditions):
        self.collect_calls += 1
        self.last_conditions = conditions
        jobs = self.jobs or [
            CanonicalJob(
                id="job-fixture",
                title="Python 工程师",
                company_name="示例公司",
                job_url="https://www.zhipin.com/job_detail/job-fixture.html",
                jd_text=self.jd_text,
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
                pages=conditions.pages,
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

    assert client.get("/api/monitor-settings").json() is None
    _save_monitor_conditions(client)
    _run_monitor(client)

    _run_match(client, first.json()["id"])
    assert boss.collect_calls == 1
    assert llm.score_calls == 1

    first_results = client.get(f"/api/resumes/{first.json()['id']}/results").json()
    second_results = client.get(f"/api/resumes/{second.json()['id']}/results").json()
    assert [item["job_id"] for item in first_results["new_published"]] == ["job-fixture"]
    assert first_results["new_published"][0]["jd_text"] == "Python FastAPI 后端开发"
    assert first_results["new_active"] == []
    assert first_results["collection_summary"]["total_details"] == 1
    assert first_results["collection_summary"]["formatted_summary"] == "岗位市场摘要: Python @ 上海"
    assert first_results["last_completed_at"]
    assert second_results["new_published"] == second_results["new_active"] == []
    assert "never-serialized" not in repr(database.get_llm_settings())


def _wait_for_match(client: TestClient) -> dict:
    for _ in range(100):
        status = client.get("/api/matches/status").json()
        if status["status"] != "running":
            return status
        time.sleep(0.01)
    return status


CONDITIONS = {
    "job_keyword": "Python",
    "city": "上海",
    "experience": "3-5年",
    "degree": "本科",
    "salary": "20-30K",
}


def _save_monitor_conditions(client: TestClient, **overrides) -> dict:
    payload = {**CONDITIONS, **overrides}
    response = client.put("/api/monitor-settings", json=payload)
    assert response.status_code == 200
    return response.json()


def _run_monitor(client: TestClient) -> dict:
    assert client.post("/api/monitor").status_code == 202
    status = _wait_for_match(client)
    assert status["status"] == "completed", status
    return status


def _run_match(client: TestClient, resume_id: str) -> dict:
    assert client.post("/api/matches", json={"resume_id": resume_id}).status_code == 202
    status = _wait_for_match(client)
    assert status["status"] == "completed", status
    return status


def test_selected_resume_is_the_only_resume_processed(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_MATCHER_LLM_API_KEY", "test-key")
    database = Database(tmp_path / "app.db", tmp_path / "resumes")
    llm, boss = FakeLlm(database), FakeBoss()
    client = TestClient(create_app(database=database, llm=llm, boss=boss))
    client.put("/api/llm-settings", json={"base_url": "https://example.test/v1", "model": "test-model"})
    first = client.post("/api/resumes", files={"file": ("first.pdf", pdf_bytes("Python first"), "application/pdf")}).json()
    second = client.post("/api/resumes", files={"file": ("second.pdf", pdf_bytes("Python second"), "application/pdf")}).json()
    _save_monitor_conditions(client)
    _run_monitor(client)

    assert client.post("/api/matches", json={"resume_id": second["id"]}).status_code == 202
    status = _wait_for_match(client)

    assert status["status"] == "completed"
    assert status["current_resume_id"] == second["id"]
    assert boss.collect_calls == 1 and llm.score_calls == 1
    assert client.get(f"/api/resumes/{first['id']}/results").json()["new_published"] == []


def test_matches_require_a_nonempty_selected_resume_id(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_MATCHER_LLM_API_KEY", "test-key")
    database = Database(tmp_path / "app.db", tmp_path / "resumes")
    client = TestClient(create_app(database=database, llm=FakeLlm(database), boss=FakeBoss()))
    client.put("/api/llm-settings", json={"base_url": "https://example.test/v1", "model": "test-model"})
    resume = client.post("/api/resumes", files={"file": ("resume.pdf", pdf_bytes("Python resume"), "application/pdf")}).json()
    _save_monitor_conditions(client)
    _run_monitor(client)

    for payload in (None, {}, {"resume_id": ""}, {"resume_id": "   "}):
        response = client.post("/api/matches") if payload is None else client.post("/api/matches", json=payload)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


def test_monitor_pages_are_saved_and_used_for_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_MATCHER_LLM_API_KEY", "test-key")
    database = Database(tmp_path / "app.db", tmp_path / "resumes")
    llm, boss = FakeLlm(database), FakeBoss()
    client = TestClient(create_app(database=database, llm=llm, boss=boss))
    client.put("/api/llm-settings", json={"base_url": "https://example.test/v1", "model": "test-model"})
    resume = client.post("/api/resumes", files={"file": ("resume.pdf", pdf_bytes("Python resume"), "application/pdf")}).json()

    saved = _save_monitor_conditions(client, pages=7)

    assert saved["conditions"]["pages"] == 7
    _run_monitor(client)
    _run_match(client, resume["id"])
    assert boss.last_conditions.pages == 7
    assert client.get(f"/api/resumes/{resume['id']}/results").json()["collection_summary"]["pages"] == 7


def test_same_job_is_scored_for_each_selected_resume_independently(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_MATCHER_LLM_API_KEY", "test-key")
    database = Database(tmp_path / "app.db", tmp_path / "resumes")
    llm, boss = FakeLlm(database), FakeBoss()
    client = TestClient(create_app(database=database, llm=llm, boss=boss))
    client.put("/api/llm-settings", json={"base_url": "https://example.test/v1", "model": "test-model"})
    first = client.post("/api/resumes", files={"file": ("first.pdf", pdf_bytes("Python first"), "application/pdf")}).json()
    second = client.post("/api/resumes", files={"file": ("second.pdf", pdf_bytes("Python second"), "application/pdf")}).json()
    _save_monitor_conditions(client)
    _run_monitor(client)
    for resume in (first, second):
        _run_match(client, resume["id"])

    assert llm.score_calls == 2
    assert client.get(f"/api/resumes/{first['id']}/results").json()["new_published"][0]["job_id"] == "job-fixture"
    assert client.get(f"/api/resumes/{second['id']}/results").json()["new_published"][0]["job_id"] == "job-fixture"


def test_changed_collected_job_is_scored_again_and_result_exposes_raw_active_status(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_MATCHER_LLM_API_KEY", "test-key")
    database = Database(tmp_path / "app.db", tmp_path / "resumes")
    llm, boss = FakeLlm(database), FakeBoss()
    client = TestClient(create_app(database=database, llm=llm, boss=boss))
    client.put("/api/llm-settings", json={"base_url": "https://example.test/v1", "model": "test-model"})
    resume = client.post("/api/resumes", files={"file": ("resume.pdf", pdf_bytes("Python resume"), "application/pdf")}).json()
    _save_monitor_conditions(client)
    _run_monitor(client)
    _run_match(client, resume["id"])
    assert llm.score_calls == 1

    # 不执行监控时，重复匹配不重复评分
    _run_match(client, resume["id"])
    assert llm.score_calls == 1

    # 职位内容变化后重新监控、匹配，才重新评分
    boss.jd_text = "Python FastAPI 后端开发，负责分布式任务调度"
    _run_monitor(client)
    _run_match(client, resume["id"])

    response = client.get(f"/api/resumes/{resume['id']}/results").json()
    results = response["new_published"]
    assert llm.score_calls == 2
    assert results[0]["active_status_raw"] == results[0]["boss_active_status"] == "刚刚活跃"
    assert response["last_completed_at"]


def test_second_llm_batch_failure_marks_run_failed_and_keeps_previous_results(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_MATCHER_LLM_API_KEY", "test-key")
    database = Database(tmp_path / "app.db", tmp_path / "resumes")

    class BatchFailingLlm(FakeLlm):
        def __init__(self, database):
            super().__init__(database)
            self.fail_on_call = None

        def score_jobs(self, settings, profile, jobs):
            self.score_calls += 1
            if self.score_calls == self.fail_on_call:
                raise RuntimeError("second scoring batch failed")
            return [ScoredJob(job_id=job.id, score=92, reason="匹配", strengths=["Python"], gaps=[]) for job in jobs]

    llm, boss = BatchFailingLlm(database), FakeBoss()
    client = TestClient(create_app(database=database, llm=llm, boss=boss))
    client.put("/api/llm-settings", json={"base_url": "https://example.test/v1", "model": "test-model"})
    resume = client.post("/api/resumes", files={"file": ("resume.pdf", pdf_bytes("Python resume"), "application/pdf")}).json()
    _save_monitor_conditions(client)
    _run_monitor(client)
    _run_match(client, resume["id"])
    previous = client.get(f"/api/resumes/{resume['id']}/results").json()

    boss.jobs = [
        CanonicalJob(
            id=f"job-{number}", title="Python 工程师", company_name="示例公司",
            job_url=f"https://www.zhipin.com/job_detail/job-{number}.html", jd_text=f"Python FastAPI {number}",
            city="上海", experience="3-5年", degree="本科", salary_text="20-30K", salary_min_k=20, salary_max_k=30,
            active_status_raw="刚刚活跃", active_bucket="active",
        ) for number in range(11)
    ]
    _run_monitor(client)
    llm.fail_on_call = llm.score_calls + 2
    assert client.post("/api/matches", json={"resume_id": resume["id"]}).status_code == 202
    status = _wait_for_match(client)
    current = client.get(f"/api/resumes/{resume['id']}/results").json()

    assert status["status"] == "failed"
    assert current["new_published"] == previous["new_published"]
    assert current["last_completed_at"] == previous["last_completed_at"]


def test_result_keeps_active_status_snapshot_after_global_job_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_MATCHER_LLM_API_KEY", "test-key")
    database = Database(tmp_path / "app.db", tmp_path / "resumes")
    llm, boss = FakeLlm(database), FakeBoss()
    client = TestClient(create_app(database=database, llm=llm, boss=boss))
    client.put("/api/llm-settings", json={"base_url": "https://example.test/v1", "model": "test-model"})
    resume = client.post("/api/resumes", files={"file": ("resume.pdf", pdf_bytes("Python resume"), "application/pdf")}).json()
    _save_monitor_conditions(client)
    _run_monitor(client)
    _run_match(client, resume["id"])

    database.upsert_job(CanonicalJob(
        id="job-fixture", title="Python 工程师", company_name="示例公司",
        job_url="https://www.zhipin.com/job_detail/job-fixture.html", jd_text="Python FastAPI 后端开发",
        city="上海", experience="3-5年", degree="本科", salary_text="20-30K", salary_min_k=20, salary_max_k=30,
        active_status_raw="今日活跃", active_bucket="recent",
    ))
    result = client.get(f"/api/resumes/{resume['id']}/results").json()["new_published"][0]

    assert result["active_status_raw"] == "刚刚活跃"
