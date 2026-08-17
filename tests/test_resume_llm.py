import sqlite3

import fitz
import pytest
from pydantic import ValidationError

from app.database import Database
from app.llm import LlmService
from app.resumes import ResumeError, ResumeService
from app.schemas import LlmSettingsInput, ResumeProfile


class FakeLlm:
    def current_settings(self):
        return {"base_url": "https://example.test/v1", "model": "test"}

    def test_settings(self, settings):
        return True

    def parse_resume(self, settings, text):
        return ResumeProfile(
            title="Python 工程师",
            summary="后端开发经验",
            tags=["Python"],
            skills=["FastAPI"],
            years_experience=3,
            education="本科",
            target_roles=["Python 工程师"],
            highlights=["交付本地服务"],
        )


@pytest.fixture
def database(tmp_path):
    return Database(tmp_path / "app.db", tmp_path / "resumes")


@pytest.fixture
def service(database):
    return ResumeService(database, FakeLlm())


def pdf_bytes(text="Python resume"):
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


def test_llm_configuration_saves_only_non_secret_fields(database, monkeypatch):
    monkeypatch.setenv("BOSS_MATCHER_LLM_API_KEY", "private-key")
    llm = LlmService(database)
    monkeypatch.setattr(llm, "test_settings", lambda settings: True)
    settings = llm.test_and_save(LlmSettingsInput(base_url="https://example.test/v1", model="test"))

    assert settings.key_configured is True
    assert "private-key" not in settings.model_dump_json()
    row = sqlite3.connect(database.db_path).execute("SELECT * FROM llm_settings").fetchone()
    assert "private-key" not in repr(row)


def test_llm_requires_environment_key(database, monkeypatch):
    monkeypatch.delenv("BOSS_MATCHER_LLM_API_KEY", raising=False)

    with pytest.raises(ValueError, match="BOSS_MATCHER_LLM_API_KEY"):
        LlmService(database).test_and_save(
            LlmSettingsInput(base_url="https://example.test/v1", model="test")
        )


def test_llm_rejects_remote_plain_http_but_allows_loopback():
    with pytest.raises(ValidationError):
        LlmSettingsInput(base_url="http://example.com/v1", model="test")

    assert LlmSettingsInput(base_url="http://127.0.0.1:11434/v1", model="test").base_url.startswith(
        "http://127.0.0.1"
    )


def test_upload_extracts_and_parses_pdf(service):
    resume = service.upload("resume.pdf", pdf_bytes("Python engineer"))

    assert resume.status == "ready"
    assert resume.profile.title == "Python 工程师"
    assert not hasattr(resume, "extracted_text")


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("resume.txt", b"not pdf", "PDF"),
        ("resume.pdf", b"not a PDF", "PDF"),
    ],
)
def test_upload_rejects_invalid_pdf(service, filename, content, message):
    with pytest.raises(ResumeError, match=message):
        service.upload(filename, content)


def test_upload_rejects_duplicate_and_capacity(service):
    content = pdf_bytes()
    service.upload("one.pdf", content)
    with pytest.raises(ResumeError, match="重复"):
        service.upload("two.pdf", content)

    for number in range(2, 11):
        service.upload(f"{number}.pdf", pdf_bytes(str(number)))
    with pytest.raises(ResumeError, match="最多保存 10"):
        service.upload("overflow.pdf", pdf_bytes("overflow"))


def test_upload_rejects_file_larger_than_ten_megabytes(service):
    with pytest.raises(ResumeError, match="10 MB"):
        service.upload("large.pdf", b"%PDF" + b"x" * (10 * 1024 * 1024))


def test_upload_rejects_empty_and_overlong_text(service, monkeypatch):
    with pytest.raises(ResumeError, match="文本"):
        service.upload("empty.pdf", pdf_bytes(" "))

    monkeypatch.setattr(service, "_extract_text", lambda _: "x" * 24001)
    with pytest.raises(ResumeError, match="24,000"):
        service.upload("long.pdf", pdf_bytes("short"))


def test_parse_failure_is_retryable(database):
    class FailingLlm(FakeLlm):
        def parse_resume(self, settings, text):
            raise ValueError("provider failure")

    service = ResumeService(database, FailingLlm())
    resume = service.upload("failed.pdf", pdf_bytes())
    assert resume.status == "parse_failed"

    service.llm = FakeLlm()
    retried = service.retry_parse(resume.id)
    assert retried.status == "ready"
