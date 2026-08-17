"""PDF validation, extraction and resume lifecycle."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import fitz

from app.database import Database, utc_now
from app.schemas import ResumeConditions, ResumePatch, ResumeProfile, ResumeView


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_RESUMES = 10
MAX_TEXT_CHARS = 24_000


class ResumeError(ValueError):
    pass


class ResumeService:
    def __init__(self, database: Database, llm) -> None:
        self.database = database
        self.llm = llm

    @staticmethod
    def _extract_text(content: bytes) -> str:
        try:
            document = fitz.open(stream=content, filetype="pdf")
            if document.needs_pass:
                raise ResumeError("加密 PDF 不受支持")
            text = "".join(page.get_text() for page in document).strip()
            document.close()
        except ResumeError:
            raise
        except Exception as exc:
            raise ResumeError("无法读取 PDF 文件") from exc
        if not text:
            raise ResumeError("PDF 没有可提取文本；MVP 不支持 OCR")
        if len(text) > MAX_TEXT_CHARS:
            raise ResumeError("简历正文超过 24,000 字符的 MVP 上限")
        return text

    def upload(self, filename: str, content: bytes) -> ResumeView:
        if not filename.lower().endswith(".pdf") or not content.startswith(b"%PDF"):
            raise ResumeError("仅支持有效的 PDF 文件")
        if not content or len(content) > MAX_FILE_BYTES:
            raise ResumeError("单份 PDF 不能超过 10 MB")
        digest = hashlib.sha256(content).hexdigest()
        if self.database.find_resume_by_sha(digest):
            raise ResumeError("该 PDF 已重复上传")
        if self.database.count_resumes() >= MAX_RESUMES:
            raise ResumeError("最多保存 10 份简历")
        text = self._extract_text(content)
        if len(text) > MAX_TEXT_CHARS:
            raise ResumeError("简历正文超过 24,000 字符的 MVP 上限")
        resume_id = str(uuid.uuid4())
        file_path = self.database.resumes_dir / f"{resume_id}.pdf"
        file_path.write_bytes(content)
        try:
            file_path.chmod(0o600)
        except OSError:
            pass
        now = utc_now()
        self.database.insert_resume(
            {
                "id": resume_id, "filename": Path(filename).name, "file_path": str(file_path), "sha256": digest,
                "file_size": len(content), "extracted_text": text, "profile_json": None, "status": "parsing",
                "error_message": None, "monitor_enabled": 0, "conditions_json": "{}", "created_at": now, "updated_at": now,
            }
        )
        return self._parse(resume_id)

    def _parse(self, resume_id: str) -> ResumeView:
        row = self._require_row(resume_id)
        try:
            profile = self.llm.parse_resume(self.llm.current_settings(), row["extracted_text"])
            self.database.update_resume(
                resume_id, status="ready", profile_json=profile.model_dump_json(), error_message=None
            )
        except Exception:
            self.database.update_resume(
                resume_id, status="parse_failed", profile_json=None,
                error_message="简历解析失败，请检查 LLM 配置后手动重试",
            )
        return self.get(resume_id)

    def retry_parse(self, resume_id: str) -> ResumeView:
        self._require_row(resume_id)
        self.database.update_resume(resume_id, status="parsing", error_message=None)
        return self._parse(resume_id)

    def get(self, resume_id: str) -> ResumeView:
        return self._to_view(self._require_row(resume_id))

    def list(self) -> list[ResumeView]:
        return [self._to_view(row) for row in self.database.list_resume_rows()]

    def update(self, resume_id: str, patch: ResumePatch) -> ResumeView:
        row = self._require_row(resume_id)
        if patch.retry_parse:
            return self.retry_parse(resume_id)
        conditions = patch.conditions
        values: dict = {}
        if conditions is not None:
            values["conditions_json"] = conditions.model_dump_json()
        current_conditions = conditions or self._conditions(row)
        if patch.monitor_enabled is not None:
            if patch.monitor_enabled and (row["status"] != "ready" or current_conditions is None):
                raise ResumeError("解析成功且条件完整后才能开启监控")
            values["monitor_enabled"] = int(patch.monitor_enabled)
        if not values:
            raise ResumeError("请提供可更新字段")
        self.database.update_resume(resume_id, **values)
        return self.get(resume_id)

    def delete(self, resume_id: str) -> None:
        row = self.database.delete_resume(resume_id)
        if row is None:
            raise KeyError(resume_id)
        try:
            Path(row["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass

    def eligible(self) -> list[dict]:
        return [
            row for row in self.database.list_resume_rows()
            if row["status"] == "ready" and bool(row["monitor_enabled"]) and self._conditions(row) is not None
        ]

    @staticmethod
    def _conditions(row: dict) -> ResumeConditions | None:
        value = json.loads(row["conditions_json"] or "{}")
        return ResumeConditions(**value) if value else None

    @staticmethod
    def _to_view(row: dict) -> ResumeView:
        profile = ResumeProfile.model_validate_json(row["profile_json"]) if row["profile_json"] else None
        return ResumeView(
            id=row["id"], filename=row["filename"], status=row["status"], error_message=row["error_message"],
            profile=profile, conditions=ResumeService._conditions(row), monitor_enabled=bool(row["monitor_enabled"]),
            created_at=row["created_at"],
        )

    def _require_row(self, resume_id: str) -> dict:
        row = self.database.get_resume_row(resume_id)
        if row is None:
            raise KeyError(resume_id)
        return row
