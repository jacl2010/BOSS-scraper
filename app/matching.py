"""Deterministic filtering and the single in-memory matching runner."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from app.database import Database
from app.schemas import CanonicalJob, MatchResult, MatchStatus, ResumeProfile, ScoredJob


@dataclass
class CandidatePools:
    new_published: list[CanonicalJob]
    new_active: list[CanonicalJob]


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

    def _claim(self, **initial) -> None:
        with self._lock:
            if self._status.status == "running":
                raise RuntimeError("已有任务正在运行")
            self._status = MatchStatus(status="running", progress_current=0, progress_total=1, **initial)

    def start_monitor(self) -> MatchStatus:
        self._claim(task="monitor", stage="checking", message="正在检查 BOSS 状态")
        thread = threading.Thread(target=self._run_monitor, daemon=True)
        thread.start()
        return self.status()

    def start_match(self, resume_id: str) -> MatchStatus:
        with self._lock:
            if self._status.status == "running":
                raise RuntimeError("已有任务正在运行")
            eligible = [resume for resume in self.resumes.eligible() if resume["id"] == resume_id]
            if not eligible:
                raise ValueError("所选简历未解析成功")
            if not self.database.list_jobs():
                raise ValueError("暂无采集到的岗位，请先执行监控")
            self._status = MatchStatus(
                status="running", task="match", stage="scoring", current_resume_id=resume_id,
                progress_current=0, progress_total=1, message="正在进行 AI 评分",
            )
        thread = threading.Thread(target=self._run_match, args=(eligible[0],), daemon=True)
        thread.start()
        return self.status()

    def _update(self, **values) -> None:
        with self._lock:
            self._status = self._status.model_copy(update=values)

    def _run_monitor(self) -> None:
        try:
            readiness = self.boss.status()
            if readiness.state != "ready":
                raise RuntimeError(readiness.message)
            settings = self.database.get_monitor_settings()
            if not settings:
                raise ValueError("请先设置监控条件")
            conditions = settings["conditions"]
            self._update(stage="scraping", message="正在采集岗位")
            jobs = self.boss.collect(conditions)
            summary = getattr(jobs, "summary", None)
            if summary is not None:
                self.database.save_collection_summary(summary)
            for job in jobs:
                self.database.upsert_job(job)
            self._update(progress_current=1)
            self._update(
                status="completed", stage="completed",
                message=f"监控完成：共采集 {len(jobs)} 个岗位，请选择简历开始匹配",
            )
        except Exception as exc:
            self._update(status="failed", stage="failed", message=str(exc))

    def _run_match(self, resume: dict) -> None:
        try:
            settings = self.llm.current_settings()
            jobs = self.database.list_jobs()
            previous_buckets, previous_active_statuses, newly_seen, changed_ids = {}, {}, set(), set()
            previous_states = self.database.get_resume_job_states(resume["id"], [job.id for job in jobs])
            for job in jobs:
                previous = previous_states.get(job.id)
                if previous is None:
                    newly_seen.add(job.id)
                    continue
                previous_active_statuses[job.id] = previous["active_status_raw"]
                if previous["matching_content_hash"] != self.database._matching_content_hash(job):
                    changed_ids.add(job.id)
            # 监控条件只用于采集参数，匹配不做确定性过滤，相关性完全交给 LLM 判断。
            pools = select_candidate_pools(
                jobs, previous_buckets, newly_seen, changed_ids, previous_active_statuses
            )
            profile = ResumeProfile.model_validate_json(resume["profile_json"])
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
            self._update(stage="finalizing", message="正在保存匹配结果")
            self.database.save_completed_match(resume["id"], results, jobs)
            self._update(progress_current=1)
            if results:
                self._update(status="completed", stage="completed", message=f"匹配完成：本轮新评分 {len(results)} 个岗位")
            else:
                self._update(status="completed", stage="completed", message="匹配完成：没有新发现或变化的岗位，无需重新评分")
        except Exception as exc:
            self._update(status="failed", stage="failed", message=str(exc))
