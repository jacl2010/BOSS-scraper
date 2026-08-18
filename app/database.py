"""Small SQLite persistence layer; no ORM or task history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from app.schemas import CanonicalJob, JobMarketSummary, LlmSettingsInput, MatchResult, ResumeConditions


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, db_path: Path | str = "data/app.db", resumes_dir: Path | str = "data/resumes") -> None:
        self.db_path = Path(db_path)
        self.resumes_dir = Path(resumes_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.resumes_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.db_path.parent, self.resumes_dir):
            try:
                path.chmod(0o700)
            except OSError:
                pass
        self.initialize()
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS llm_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1), base_url TEXT NOT NULL,
                    model TEXT NOT NULL, tested_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    thinking_enabled INTEGER NOT NULL DEFAULT 1,
                    reasoning_effort TEXT NOT NULL DEFAULT 'low'
                );
                CREATE TABLE IF NOT EXISTS resumes (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, file_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE, file_size INTEGER NOT NULL, extracted_text TEXT NOT NULL,
                    profile_json TEXT, status TEXT NOT NULL, error_message TEXT,
                    monitor_enabled INTEGER NOT NULL DEFAULT 0, conditions_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, company_name TEXT, job_url TEXT NOT NULL,
                    jd_text TEXT NOT NULL, city TEXT, experience TEXT, degree TEXT, salary_text TEXT,
                    salary_min_k REAL, salary_max_k REAL, active_status_raw TEXT, active_bucket TEXT NOT NULL,
                    published_at TEXT, matching_content_hash TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS match_results (
                    resume_id TEXT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    pool TEXT NOT NULL, score INTEGER NOT NULL, reason TEXT NOT NULL,
                    strengths_json TEXT NOT NULL, gaps_json TEXT NOT NULL, rank INTEGER NOT NULL,
                    active_status_raw TEXT, matched_at TEXT NOT NULL, PRIMARY KEY (resume_id, job_id)
                );
                CREATE TABLE IF NOT EXISTS resume_job_states (
                    resume_id TEXT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    matching_content_hash TEXT NOT NULL, active_status_raw TEXT,
                    PRIMARY KEY (resume_id, job_id)
                );
                CREATE TABLE IF NOT EXISTS resume_match_states (
                    resume_id TEXT PRIMARY KEY REFERENCES resumes(id) ON DELETE CASCADE,
                    last_completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_summaries (
                    resume_id TEXT PRIMARY KEY REFERENCES resumes(id) ON DELETE CASCADE,
                    summary_json TEXT NOT NULL, collected_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    conditions_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_summaries (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    summary_json TEXT NOT NULL, collected_at TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(llm_settings)")}
            if "thinking_enabled" not in columns:
                conn.execute("ALTER TABLE llm_settings ADD COLUMN thinking_enabled INTEGER NOT NULL DEFAULT 1")
            if "reasoning_effort" not in columns:
                conn.execute("ALTER TABLE llm_settings ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT 'low'")
            job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "matching_content_hash" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN matching_content_hash TEXT NOT NULL DEFAULT ''")
            result_columns = {row["name"] for row in conn.execute("PRAGMA table_info(match_results)")}
            if "active_status_raw" not in result_columns:
                conn.execute("ALTER TABLE match_results ADD COLUMN active_status_raw TEXT")
            conn.execute(
                """UPDATE match_results SET active_status_raw=(
                SELECT active_status_raw FROM jobs WHERE jobs.id=match_results.job_id
                ) WHERE active_status_raw IS NULL"""
            )

    def get_llm_settings(self) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT base_url, model, tested_at, thinking_enabled, reasoning_effort FROM llm_settings WHERE id = 1"
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["thinking_enabled"] = bool(result["thinking_enabled"])
        return result

    def save_llm_settings(self, settings: LlmSettingsInput) -> dict:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO llm_settings(id, base_url, model, tested_at, updated_at, thinking_enabled, reasoning_effort)
                VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET base_url=excluded.base_url, model=excluded.model,
                tested_at=excluded.tested_at, updated_at=excluded.updated_at,
                thinking_enabled=excluded.thinking_enabled, reasoning_effort=excluded.reasoning_effort""",
                (settings.base_url, settings.model, now, now, settings.thinking_enabled, settings.reasoning_effort),
            )
        return {
            "base_url": settings.base_url,
            "model": settings.model,
            "tested_at": now,
            "thinking_enabled": settings.thinking_enabled,
            "reasoning_effort": settings.reasoning_effort,
        }

    def get_monitor_settings(self) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT conditions_json, updated_at FROM monitor_settings WHERE id = 1").fetchone()
        if not row:
            return None
        conditions = ResumeConditions.model_validate_json(row["conditions_json"])
        return {"conditions": conditions, "updated_at": row["updated_at"]}

    def save_monitor_settings(self, conditions: ResumeConditions) -> dict:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO monitor_settings(id, conditions_json, updated_at) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET conditions_json=excluded.conditions_json,
                updated_at=excluded.updated_at""",
                (conditions.model_dump_json(), now),
            )
        return {"conditions": conditions, "updated_at": now}

    def count_resumes(self) -> int:
        with self.transaction() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0])

    def find_resume_by_sha(self, digest: str) -> bool:
        with self.transaction() as conn:
            return conn.execute("SELECT 1 FROM resumes WHERE sha256 = ?", (digest,)).fetchone() is not None

    def insert_resume(self, values: dict) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.transaction() as conn:
            conn.execute(f"INSERT INTO resumes ({columns}) VALUES ({placeholders})", tuple(values.values()))

    def update_resume(self, resume_id: str, **values: object) -> bool:
        if not values:
            return self.get_resume_row(resume_id) is not None
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{column} = ?" for column in values)
        with self.transaction() as conn:
            result = conn.execute(f"UPDATE resumes SET {assignments} WHERE id = ?", (*values.values(), resume_id))
        return result.rowcount == 1

    def get_resume_row(self, resume_id: str) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
        return dict(row) if row else None

    def list_resume_rows(self) -> list[dict]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM resumes ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def delete_resume(self, resume_id: str) -> dict | None:
        row = self.get_resume_row(resume_id)
        if not row:
            return None
        with self.transaction() as conn:
            conn.execute("DELETE FROM resumes WHERE id = ?", (resume_id,))
        return row

    def upsert_job(self, job: CanonicalJob) -> tuple[bool, str | None, bool, str | None]:
        """Persist a job and report discovery, prior activity, and match-content changes."""

        now = utc_now()
        content_hash = self._matching_content_hash(job)
        with self.transaction() as conn:
            previous = conn.execute(
                "SELECT active_bucket, active_status_raw, matching_content_hash FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if previous is None:
                conn.execute(
                    """INSERT INTO jobs(
                    id, title, company_name, job_url, jd_text, city, experience, degree, salary_text,
                    salary_min_k, salary_max_k, active_status_raw, active_bucket, published_at,
                    matching_content_hash, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*self._job_values(job), content_hash, now, now),
                )
                return True, None, False, None
            content_changed = previous["matching_content_hash"] != content_hash
            conn.execute(
                """UPDATE jobs SET title=?, company_name=?, job_url=?, jd_text=?, city=?, experience=?, degree=?,
                salary_text=?, salary_min_k=?, salary_max_k=?, active_status_raw=?, active_bucket=?, published_at=?,
                matching_content_hash=?, last_seen_at=? WHERE id=?""",
                (*self._job_values(job)[1:], content_hash, now, job.id),
            )
            return False, str(previous["active_bucket"]), content_changed, previous["active_status_raw"]

    @staticmethod
    def _job_values(job: CanonicalJob) -> tuple:
        return (
            job.id, job.title, job.company_name, job.job_url, job.jd_text, job.city, job.experience, job.degree,
            job.salary_text, job.salary_min_k, job.salary_max_k, job.active_status_raw, job.active_bucket, job.published_at,
        )

    @staticmethod
    def _matching_content_hash(job: CanonicalJob) -> str:
        """Hash only the fields that can change the LLM's assessment."""

        content = {
            "title": job.title,
            "company_name": job.company_name,
            "job_url": job.job_url,
            "jd_text": job.jd_text,
            "city": job.city,
            "experience": job.experience,
            "degree": job.degree,
            "salary_text": job.salary_text,
            "salary_min_k": job.salary_min_k,
            "salary_max_k": job.salary_max_k,
        }
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def list_jobs(self) -> list[CanonicalJob]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY first_seen_at DESC").fetchall()
        return [
            CanonicalJob(
                id=row["id"], title=row["title"], company_name=row["company_name"], job_url=row["job_url"],
                jd_text=row["jd_text"], city=row["city"], experience=row["experience"], degree=row["degree"],
                salary_text=row["salary_text"], salary_min_k=row["salary_min_k"], salary_max_k=row["salary_max_k"],
                active_status_raw=row["active_status_raw"], active_bucket=row["active_bucket"],
                published_at=row["published_at"],
            )
            for row in rows
        ]

    def get_resume_job_states(self, resume_id: str, job_ids: list[str]) -> dict[str, dict]:
        if not job_ids:
            return {}
        placeholders = ", ".join("?" for _ in job_ids)
        with self.transaction() as conn:
            rows = conn.execute(
                f"""SELECT job_id, matching_content_hash, active_status_raw FROM resume_job_states
                WHERE resume_id=? AND job_id IN ({placeholders})""",
                (resume_id, *job_ids),
            ).fetchall()
        return {row["job_id"]: dict(row) for row in rows}

    def save_completed_match(self, resume_id: str, results: list[MatchResult], observed_jobs: list[CanonicalJob]) -> str:
        """Atomically upsert this round's scores, per-resume job baselines, and completion time."""

        matched_at = utc_now()
        with self.transaction() as conn:
            conn.executemany(
                """INSERT INTO match_results(
                resume_id, job_id, pool, score, reason, strengths_json, gaps_json, rank, active_status_raw, matched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resume_id, job_id) DO UPDATE SET pool=excluded.pool, score=excluded.score,
                reason=excluded.reason, strengths_json=excluded.strengths_json, gaps_json=excluded.gaps_json,
                rank=excluded.rank, active_status_raw=excluded.active_status_raw, matched_at=excluded.matched_at""",
                [
                    (resume_id, item.job_id, item.pool, item.score, item.reason, json.dumps(item.strengths, ensure_ascii=False),
                     json.dumps(item.gaps, ensure_ascii=False), item.rank, item.active_status, matched_at)
                    for item in results
                ],
            )
            conn.executemany(
                """INSERT INTO resume_job_states(resume_id, job_id, matching_content_hash, active_status_raw)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(resume_id, job_id) DO UPDATE SET
                matching_content_hash=excluded.matching_content_hash,
                active_status_raw=excluded.active_status_raw""",
                [
                    (resume_id, job.id, self._matching_content_hash(job), job.active_status_raw)
                    for job in observed_jobs
                ],
            )
            conn.execute(
                """INSERT INTO resume_match_states(resume_id, last_completed_at) VALUES (?, ?)
                ON CONFLICT(resume_id) DO UPDATE SET last_completed_at=excluded.last_completed_at""",
                (resume_id, matched_at),
            )
        return matched_at

    def get_results(self, resume_id: str) -> list[dict]:
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT r.*, j.title, j.company_name, j.job_url, j.jd_text, j.city, j.experience, j.degree, j.salary_text
                FROM match_results r JOIN jobs j ON j.id=r.job_id
                WHERE r.resume_id=? ORDER BY r.pool, r.score DESC, r.job_id""", (resume_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_match_completion(self, resume_id: str) -> str | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT last_completed_at FROM resume_match_states WHERE resume_id=?", (resume_id,)
            ).fetchone()
        return str(row["last_completed_at"]) if row else None

    def save_collection_summary(self, summary: JobMarketSummary) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO monitor_summaries(id, summary_json, collected_at) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET summary_json=excluded.summary_json,
                collected_at=excluded.collected_at""",
                (summary.model_dump_json(), utc_now()),
            )

    def get_collection_summary(self) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT summary_json, collected_at FROM monitor_summaries WHERE id = 1").fetchone()
        if row is None:
            return None
        summary = JobMarketSummary.model_validate_json(row["summary_json"])
        return {"collected_at": row["collected_at"], **summary.model_dump(mode="json")}
