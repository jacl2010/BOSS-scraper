"""Deterministic filtering and the single in-memory matching runner."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from app.database import Database
from app.schemas import CanonicalJob, MatchResult, MatchStatus, ResumeConditions, ResumeProfile, ScoredJob


@dataclass
class CandidatePools:
    new_published: list[CanonicalJob]
    new_active: list[CanonicalJob]


def _unlimited(value: str | None) -> bool:
    return not value or value.strip() in {"不限", "全部", "any"}


def _same_requirement(expected: str, actual: str | None) -> bool:
    return _unlimited(expected) or (actual is not None and expected.strip().lower() in actual.strip().lower())


def _same_experience_requirement(expected: str, actual: str | None) -> bool:
    if _unlimited(expected):
        return True
    if actual is None:
        return False
    choices = [item.strip() for item in re.split(r"[,，]", expected) if item.strip()]
    aliases = {"10年": "10年以上"}
    actual_value = aliases.get(actual.strip(), actual.strip())
    return any(aliases.get(item, item) == actual_value for item in choices)


def _salary(value: str) -> tuple[float | None, float | None]:
    range_match = re.search(
        r"(\d+(?:\.\d+)?)\s*[kK]?\s*[-~～至]\s*(\d+(?:\.\d+)?)\s*[kK]", value
    )
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2))
    amounts = [float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*[kK]", value)]
    return (amounts[0], amounts[-1]) if amounts else (None, None)


def filter_jobs(jobs: list[CanonicalJob], conditions: ResumeConditions) -> list[CanonicalJob]:
    wanted_min, wanted_max = _salary(conditions.salary)
    # The collector accepts a nine-digit city code and resolves it before
    # scraping.  Detail records contain a Chinese location, so comparing the
    # original code to that string would incorrectly discard every result.
    city_is_code = re.fullmatch(r"\d{9}", conditions.city.strip()) is not None
    result: list[CanonicalJob] = []
    for job in jobs:
        haystack = f"{job.title} {job.jd_text}".lower()
        salary_matches = _unlimited(conditions.salary) or (
            wanted_min is not None and job.salary_min_k is not None and job.salary_max_k is not None
            and job.salary_max_k >= wanted_min and (wanted_max is None or job.salary_min_k <= wanted_max)
        )
        if (
            conditions.job_keyword.lower() in haystack and (city_is_code or _same_requirement(conditions.city, job.city))
            and _same_experience_requirement(conditions.experience, job.experience)
            and _same_requirement(conditions.degree, job.degree)
            and salary_matches
        ):
            result.append(job)
    return result


def select_candidate_pools(
    jobs: list[CanonicalJob], previous_buckets: dict[str, str | None], newly_seen: set[str],
    changed_ids: set[str] | None = None, previous_active_statuses: dict[str, str | None] | None = None,
) -> CandidatePools:
    changed_ids = changed_ids or set()
    previous_active_statuses = previous_active_statuses or {}
    # A later collector run may return the same job ID with a revised JD or
    # requirement. Those jobs need a new LLM assessment just like new jobs.
    published = [job for job in jobs if job.id in newly_seen or job.id in changed_ids]
    published_ids = {job.id for job in published}
    activated = [
        job for job in jobs
        if job.id not in published_ids and job.active_status_raw == "刚刚活跃"
        and previous_active_statuses.get(job.id) != "刚刚活跃"
    ]
    return CandidatePools(new_published=published, new_active=activated)


def sort_scored_results(scored: list[ScoredJob]) -> list[ScoredJob]:
    return sorted(scored, key=lambda item: (-item.score, item.job_id))


class MatchRunner:
    def __init__(self, database: Database, resumes, boss, llm) -> None:
        self.database, self.resumes, self.boss, self.llm = database, resumes, boss, llm
        self._lock = threading.Lock()
        self._status = MatchStatus()

    def status(self) -> MatchStatus:
        with self._lock:
            return self._status.model_copy(deep=True)

    def start(self, resume_id: str) -> MatchStatus:
        with self._lock:
            if self._status.status == "running":
                raise RuntimeError("已有匹配任务正在运行")
            eligible = self.resumes.eligible()
            eligible = [resume for resume in eligible if resume["id"] == resume_id]
            if not eligible:
                raise ValueError("所选简历未解析成功、未开启监控或条件不完整")
            self._status = MatchStatus(status="running", stage="checking", progress_total=len(eligible), message="正在检查 BOSS 状态")
        thread = threading.Thread(target=self._run, args=(eligible,), daemon=True)
        thread.start()
        return self.status()

    def _update(self, **values) -> None:
        with self._lock:
            self._status = self._status.model_copy(update=values)

    def _run(self, eligible: list[dict]) -> None:
        try:
            readiness = self.boss.status()
            if readiness.state != "ready":
                raise RuntimeError(readiness.message)
            settings = self.llm.current_settings()
            for number, resume in enumerate(eligible, start=1):
                self._update(stage="scraping", current_resume_id=resume["id"], progress_current=number - 1, message="正在采集岗位")
                conditions = self.resumes._conditions(resume)
                jobs = self.boss.collect(conditions)
                summary = getattr(jobs, "summary", None)
                if summary is not None:
                    self.database.save_collection_summary(resume["id"], summary)
                previous_buckets, previous_active_statuses, newly_seen, changed_ids = {}, {}, set(), set()
                previous_states = self.database.get_resume_job_states(resume["id"], [job.id for job in jobs])
                for job in jobs:
                    self.database.upsert_job(job)
                    previous = previous_states.get(job.id)
                    if previous is None:
                        newly_seen.add(job.id)
                        continue
                    previous_active_statuses[job.id] = previous["active_status_raw"]
                    if previous["matching_content_hash"] != self.database._matching_content_hash(job):
                        changed_ids.add(job.id)
                self._update(stage="filtering", message="正在筛选岗位")
                pools = select_candidate_pools(
                    filter_jobs(jobs, conditions), previous_buckets, newly_seen, changed_ids, previous_active_statuses
                )
                profile = ResumeProfile.model_validate_json(resume["profile_json"])
                self._update(stage="scoring", message="正在进行 AI 评分")
                results: list[MatchResult] = []
                for pool_name, candidates in (("new_published", pools.new_published), ("new_active", pools.new_active)):
                    scored: list[ScoredJob] = []
                    for offset in range(0, len(candidates), 10):
                        scored.extend(self.llm.score_jobs(settings, profile, candidates[offset:offset + 10]))
                    jobs_by_id = {job.id: job for job in candidates}
                    for rank, item in enumerate(sort_scored_results(scored), start=1):
                        if item.job_id in jobs_by_id:
                            results.append(MatchResult(
                                job_id=item.job_id, pool=pool_name, rank=rank, score=item.score,
                                title=jobs_by_id[item.job_id].title, company_name=jobs_by_id[item.job_id].company_name,
                                job_url=jobs_by_id[item.job_id].job_url, city=jobs_by_id[item.job_id].city,
                                experience=jobs_by_id[item.job_id].experience, degree=jobs_by_id[item.job_id].degree,
                                salary=jobs_by_id[item.job_id].salary_text, active_status=jobs_by_id[item.job_id].active_status_raw,
                                reason=item.reason, strengths=item.strengths, gaps=item.gaps,
                            ))
                self._update(stage="finalizing", message="正在保存最近结果")
                self.database.save_completed_match(resume["id"], results, jobs)
                self._update(progress_current=number)
            self._update(status="completed", stage="completed", message="匹配完成")
        except Exception as exc:
            self._update(status="failed", stage="failed", message=str(exc))
