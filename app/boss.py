"""Constrained adapter around the pinned boss-scraper command-line tool."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.schemas import BossStatus, CanonicalJob, ResumeConditions


EXPERIENCE_CODES = {
    "在校生": "108", "应届生": "102", "经验不限": "101", "1年以内": "103",
    "1-3年": "104", "3-5年": "105", "5-10年": "106", "10年以上": "107",
}
DEGREE_CODES = {
    "初中及以下": "209", "中专/中技": "208", "高中": "206", "大专": "202",
    "本科": "203", "硕士": "204", "博士": "205",
}
SALARY_CODES = {
    "3K以下": "402", "3-5K": "403", "5-10K": "404", "10-20K": "405",
    "20-50K": "406", "50K以上": "407",
}


def active_bucket(value: str | None) -> str:
    text = (value or "").strip()
    if any(item in text for item in ("在线", "刚刚活跃")):
        return "active"
    if any(item in text for item in ("今日", "近几日", "近期活跃")):
        return "recent"
    if any(item in text for item in ("两周", "月内", "很久", "不活跃")):
        return "inactive"
    return "unknown"


def _salary_range(value: str | None) -> tuple[float | None, float | None]:
    numbers = [float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*[kK]", value or "")]
    if len(numbers) >= 2:
        return numbers[0], numbers[1]
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return None, None


def normalize_job(raw: dict) -> CanonicalJob:
    job_id = raw.get("id") or raw.get("job_id") or raw.get("encryptJobId")
    title = raw.get("title") or raw.get("job_name") or raw.get("jobName")
    job_url = raw.get("job_url") or raw.get("job_link") or raw.get("url") or raw.get("jobUrl")
    jd_text = raw.get("jd_text") or raw.get("jd") or raw.get("description") or raw.get("job_desc") or raw.get("jobDescription")
    if not all(isinstance(value, str) and value.strip() for value in (job_id, title, job_url, jd_text)):
        raise ValueError("岗位缺少稳定 ID、名称、链接或 JD")
    salary = raw.get("salary_text") or raw.get("salary")
    minimum, maximum = _salary_range(salary)
    tags = str(raw.get("tags_list") or raw.get("tags") or "")
    tag_items = [item.strip() for item in tags.split("|") if item.strip()]
    experience = raw.get("experience") or next(
        (item for item in tag_items if "年" in item or item in {"应届生", "在校生", "经验不限"}), None
    )
    degree = raw.get("degree") or next((item for item in tag_items if item in DEGREE_CODES), None)
    active_status = (
        raw.get("active_status_raw") or raw.get("active_status")
        or raw.get("boss_active_status") or raw.get("boss_active_time")
    )
    return CanonicalJob(
        id=str(job_id), title=title.strip(),
        company_name=raw.get("company_name") or raw.get("company") or raw.get("boss_name") or raw.get("companyName"),
        job_url=job_url, jd_text=jd_text.strip(), city=raw.get("city") or raw.get("location"), experience=experience,
        degree=degree, salary_text=salary, salary_min_k=raw.get("salary_min_k") or minimum,
        salary_max_k=raw.get("salary_max_k") or maximum,
        active_status_raw=active_status, active_bucket=active_bucket(active_status),
        published_at=raw.get("published_at"),
    )


class BossAdapter:
    def __init__(self, command: str = "boss-scraper") -> None:
        self.command = command

    def status(self) -> BossStatus:
        if not shutil.which(self.command):
            return BossStatus(state="collector_unavailable", message="未找到固定版本的 BOSS 采集器")
        try:
            result = subprocess.run([self.command, "--check"], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return BossStatus(state="chrome_unavailable", message="BOSS Chrome 检查不可用")
        text = (result.stdout + result.stderr).lower()
        if result.returncode == 0 and not any(word in text for word in ("未登录", "login", "频繁", "限制")):
            return BossStatus(state="ready", message="BOSS 采集器已就绪")
        if any(word in text for word in ("频繁", "限制", "risk", "captcha")):
            return BossStatus(state="platform_limited", message="BOSS 平台限制，请稍后人工重试")
        if any(word in text for word in ("未登录", "login")):
            return BossStatus(state="login_required", message="请在 BOSS 专用 Chrome 中手动登录")
        return BossStatus(state="chrome_unavailable", message="BOSS Chrome 未就绪")

    def setup(self) -> BossStatus:
        if not shutil.which(self.command):
            return BossStatus(state="collector_unavailable", message="未找到固定版本的 BOSS 采集器")
        try:
            subprocess.Popen([self.command, "--setup-chrome", "--no-wait-login"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return BossStatus(state="chrome_unavailable", message="无法启动 BOSS 专用 Chrome")
        return BossStatus(state="login_required", message="BOSS 专用 Chrome 已打开，请手动登录")

    def collect(self, conditions: ResumeConditions) -> list[CanonicalJob]:
        if not shutil.which(self.command):
            raise RuntimeError("未找到固定版本的 BOSS 采集器")
        with tempfile.TemporaryDirectory(prefix="boss-matcher-") as temporary:
            output = Path(temporary) / "jobs.json"
            detail_output = Path(temporary) / "details.json"
            args = [self.command, "--keyword", conditions.job_keyword, "--city", conditions.city, "--pages", "2",
                    "--max-details", "20", "--output", str(output), "--detail-output", str(detail_output),
                    "--format", "json"]
            for flag, value in (
                ("--salary", SALARY_CODES.get(conditions.salary)),
                ("--experience", EXPERIENCE_CODES.get(conditions.experience)),
                ("--degree", DEGREE_CODES.get(conditions.degree)),
            ):
                if value:
                    args.extend([flag, value])
            try:
                result = subprocess.run(args, capture_output=True, text=True, timeout=600, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("BOSS 采集超过 600 秒，已停止") from exc
            if result.returncode != 0:
                raise RuntimeError("BOSS 采集失败，请检查登录状态或平台限制")
            if not detail_output.exists():
                return []
            try:
                payload = json.loads(detail_output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError("BOSS 采集返回了无效的岗位详情 JSON") from exc
        records = payload.get("jobs", []) if isinstance(payload, dict) else payload
        normalized = []
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                normalized.append(normalize_job(record))
            except ValueError:
                continue
        return normalized
