import json
from pathlib import Path
from types import SimpleNamespace

from app.boss import BossAdapter, active_bucket, normalize_job
from app.matching import filter_jobs, select_candidate_pools, sort_scored_results
from app.schemas import ResumeConditions, ScoredJob


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "boss_jobs.json"


def test_normalizes_boss_job_and_active_status():
    source = json.loads(FIXTURE_PATH.read_text())[0]
    job = normalize_job(source)

    assert job.id == "new-job"
    assert job.active_bucket == "active"
    assert active_bucket("月内活跃") == "inactive"
    assert active_bucket("unknown phrase") == "unknown"


def test_normalizes_actual_pinned_cli_detail_shape():
    job = normalize_job(
        {
            "job_id": "cli-job",
            "title": "Python 工程师",
            "company": "示例公司",
            "job_link": "https://www.zhipin.com/job_detail/cli-job.html",
            "jd": "Python FastAPI 后端开发",
            "location": "上海·浦东新区",
            "tags_list": "3-5年 | 本科",
            "salary": "20-30K",
            "boss_active_status": "今日活跃",
        }
    )

    assert job.company_name == "示例公司"
    assert job.city == "上海·浦东新区"
    assert job.experience == "3-5年"
    assert job.degree == "本科"
    assert job.active_bucket == "recent"


def test_collect_uses_and_reads_pinned_cli_detail_output(monkeypatch):
    observed = {}

    def fake_run(args, **kwargs):
        observed["args"] = args
        detail_path = Path(args[args.index("--detail-output") + 1])
        detail_path.write_text(
            json.dumps(
                [
                    {
                        "job_id": "cli-job",
                        "title": "Python 工程师",
                        "company": "示例公司",
                        "job_link": "https://www.zhipin.com/job_detail/cli-job.html",
                        "jd": "Python FastAPI 后端开发",
                        "location": "上海",
                        "tags_list": "3-5年 | 本科",
                        "salary": "20-30K",
                        "boss_active_status": "在线",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.boss.shutil.which", lambda command: "/fake/boss-scraper")
    monkeypatch.setattr("app.boss.subprocess.run", fake_run)
    jobs = BossAdapter().collect(
        ResumeConditions(
            job_keyword="Python", city="上海", experience="3-5年", degree="本科", salary="20-30K"
        )
    )

    assert "--detail-output" in observed["args"]
    assert [job.id for job in jobs] == ["cli-job"]


def test_five_hard_filters_keep_only_matching_job():
    jobs = [normalize_job(item) for item in json.loads(FIXTURE_PATH.read_text())]
    conditions = ResumeConditions(
        job_keyword="Python", city="上海", experience="3-5年", degree="本科", salary="20-30K"
    )

    assert [job.id for job in filter_jobs(jobs, conditions)] == ["new-job"]


def test_first_discovery_and_active_transition_pools_are_exclusive():
    jobs = [normalize_job(item) for item in json.loads(FIXTURE_PATH.read_text())]
    pools = select_candidate_pools(
        jobs,
        previous_buckets={"new-job": "inactive", "filtered-city": "active"},
        newly_seen={"new-job"},
    )

    assert [job.id for job in pools.new_published] == ["new-job"]
    assert pools.new_active == []


def test_first_scan_active_job_enters_new_active_pool():
    job = normalize_job(json.loads(FIXTURE_PATH.read_text())[0])
    pools = select_candidate_pools([job], previous_buckets={}, newly_seen=set())
    assert [item.id for item in pools.new_active] == ["new-job"]


def test_inactive_to_active_transition_enters_new_active_pool():
    job = normalize_job(json.loads(FIXTURE_PATH.read_text())[0])
    pools = select_candidate_pools([job], previous_buckets={"new-job": "inactive"}, newly_seen=set())
    assert [item.id for item in pools.new_active] == ["new-job"]


def test_sorting_is_descending_stable_top_ten_and_omits_unscored():
    scored = [
        ScoredJob(job_id=f"job-{index}", score=100 - index, reason="匹配", strengths=[], gaps=[])
        for index in range(12)
    ]
    result = sort_scored_results(scored)

    assert [item.job_id for item in result] == [f"job-{index}" for index in range(10)]
