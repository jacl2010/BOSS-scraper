"""Constrained adapter around the pinned boss-scraper command-line tool."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from pathlib import Path

from scripts.job_summary import build_summary, format_summary

from app.schemas import BossStatus, CanonicalJob, JobMarketSummary, ResumeConditions


EXPERIENCE_CODES = {
    "在校生": "108", "应届生": "102", "经验不限": "101", "1年以内": "103",
    "1-3年": "104", "3-5年": "105", "5-10年": "106", "10年以上": "107",
    "10年": "107",
}
DEGREE_CODES = {
    "初中及以下": "209", "中专/中技": "208", "高中": "206", "大专": "202",
    "本科": "203", "硕士": "204", "博士": "205",
}
SALARY_CODES = {
    "3K以下": "402", "3-5K": "403", "5-10K": "404", "10-20K": "405",
    "20-50K": "406", "50K以上": "407",
}
SALARY_BUCKETS = (
    (0, 3, "402"), (3, 5, "403"), (5, 10, "404"), (10, 20, "405"),
    (20, 50, "406"), (50, None, "407"),
)
BOSS_HOMEPAGE = "https://www.zhipin.com"


def _open_boss_homepage(cdp_port: int = 9222) -> bool:
    """Open BOSS in the scraper-owned Chrome once its CDP endpoint is ready."""

    endpoint = f"http://127.0.0.1:{cdp_port}/json/new?{quote(BOSS_HOMEPAGE, safe=':/')}"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(endpoint, method="PUT"), timeout=1):
                return True
        except (OSError, URLError):
            time.sleep(0.2)
    return False


class CollectedJobs(list[CanonicalJob]):
    """List-compatible collection result with an aggregate market summary.

    Keeping this a list subclass preserves the collector contract for callers
    that only need jobs, while allowing the matching run to persist the
    accompanying job-market summary.
    """

    def __init__(self, jobs: list[CanonicalJob], summary: JobMarketSummary) -> None:
        super().__init__(jobs)
        self.summary = summary


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
    text = value or ""
    range_match = re.search(
        r"(\d+(?:\.\d+)?)\s*[kK]?\s*[-~～至]\s*(\d+(?:\.\d+)?)\s*[kK]", text
    )
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2))
    numbers = [float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*[kK]", text)]
    if len(numbers) >= 2:
        return numbers[0], numbers[1]
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return None, None


def salary_code(value: str | None) -> str | None:
    """Map a precise local salary range to a safe upstream filter bucket.

    The scraper only supports broad salary buckets.  A free-form range is
    passed to it only when it fits completely within one bucket; ranges that
    straddle buckets are left to the local, exact salary filter.
    """

    if value in SALARY_CODES:
        return SALARY_CODES[value]
    minimum, maximum = _salary_range(value)
    if minimum is None or maximum is None:
        return None
    for lower, upper, code in SALARY_BUCKETS:
        if minimum >= lower and (upper is None or maximum <= upper):
            return code
    return None


def experience_code(value: str | None) -> str | None:
    """Return a collector code only when the requested experience is singular.

    The upstream CLI accepts one experience code.  A comma-separated request
    such as ``5-10年,10年以上`` is handled as a local OR filter after scraping,
    rather than arbitrarily discarding one of the two ranges.
    """

    choices = [item.strip() for item in re.split(r"[,，]", value or "") if item.strip()]
    return EXPERIENCE_CODES.get(choices[0]) if len(choices) == 1 else None


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


def _read_payload(path: Path, label: str) -> tuple[list[dict], dict]:
    if not path.exists():
        return [], {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"BOSS 采集返回了无效的{label} JSON") from exc
    records = payload.get("jobs", []) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise RuntimeError(f"BOSS 采集返回了无效的{label} JSON")
    metadata = payload if isinstance(payload, dict) else {}
    return [record for record in records if isinstance(record, dict)], metadata


def _merge_list_and_detail_records(listings: list[dict], details: list[dict]) -> list[dict]:
    """Join list data and details by job_id, retaining a JD-bearing record only."""

    listing_by_id = {
        str(record.get("job_id") or record.get("id") or "").strip(): record
        for record in listings
        if str(record.get("job_id") or record.get("id") or "").strip()
    }
    merged: list[dict] = []
    for detail in details:
        job_id = str(detail.get("job_id") or detail.get("id") or "").strip()
        # Detail data is authoritative for JD and recruiter activity; list data
        # retains fields such as job labels and benefits for the market summary.
        record = {**listing_by_id.get(job_id, {}), **detail}
        if record:
            merged.append(record)
    return merged


def build_job_market_summary(
    listings: list[dict], details: list[dict], conditions: ResumeConditions, city: str
) -> JobMarketSummary:
    """Reuse the pinned collector's aggregation semantics and display text."""

    values = build_summary(
        listings, details, search_keyword=conditions.job_keyword, city=city
    )
    return JobMarketSummary(
        **values,
        filters={
            "experience": conditions.experience,
            "degree": conditions.degree,
            "salary": conditions.salary,
        },
        pages=conditions.pages,
        formatted_summary=format_summary(values),
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
        if not _open_boss_homepage():
            return BossStatus(state="chrome_unavailable", message="BOSS 专用 Chrome 已启动，但未能打开 BOSS 首页")
        return BossStatus(state="login_required", message="BOSS 专用 Chrome 已打开并进入 BOSS 首页，请手动登录")

    def collect(self, conditions: ResumeConditions) -> CollectedJobs:
        if not shutil.which(self.command):
            raise RuntimeError("未找到固定版本的 BOSS 采集器")
        with tempfile.TemporaryDirectory(prefix="boss-matcher-") as temporary:
            output = Path(temporary) / "jobs.json"
            detail_output = Path(temporary) / "details.json"
            # The upstream CLI resolves Chinese city names / nine-digit codes
            # itself (local city_codes.json -> live BOSS city API -> code
            # fallback).  Do not pre-resolve it here or duplicate that logic.
            args = [
                self.command, "--keyword", conditions.job_keyword, "--city", conditions.city,
                "--pages", str(conditions.pages), "--detail", "--output", str(output),
                "--detail-output", str(detail_output), "--format", "json",
            ]
            for flag, value in (
                ("--salary", salary_code(conditions.salary)),
                ("--experience", experience_code(conditions.experience)),
                ("--degree", DEGREE_CODES.get(conditions.degree)),
            ):
                if value:
                    args.extend([flag, value])
            try:
                result = subprocess.run(args, capture_output=True, text=True, timeout=6000, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("BOSS 采集超过 6000 秒，已停止") from exc
            if result.returncode != 0:
                output_text = f"{result.stdout}\n{result.stderr}"
                if "无法解析城市" in output_text:
                    raise RuntimeError("城市无法解析，请输入受支持的中文城市名或 9 位代码")
                raise RuntimeError("BOSS 采集失败，请检查登录状态或平台限制")
            listings, list_metadata = _read_payload(output, "岗位列表")
            details, _ = _read_payload(detail_output, "岗位详情")
            summary_city = str(list_metadata.get("city") or conditions.city)
        summary = build_job_market_summary(listings, details, conditions, summary_city)
        normalized: list[CanonicalJob] = []
        for record in _merge_list_and_detail_records(listings, details):
            try:
                normalized.append(normalize_job(record))
            except ValueError:
                continue
        return CollectedJobs(normalized, summary)
