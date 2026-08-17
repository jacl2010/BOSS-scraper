import json
from pathlib import Path
from types import SimpleNamespace

from app.boss import BossAdapter, active_bucket, experience_code, normalize_job, salary_code
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
        observed["timeout"] = kwargs["timeout"]
        list_path = Path(args[args.index("--output") + 1])
        list_path.write_text(
            json.dumps(
                {
                    "keyword": "Python",
                    "city": "上海",
                    "jobs": [
                        {
                            "job_id": "cli-job",
                            "title": "Python 工程师",
                            "salary": "20-30K",
                            "location": "上海",
                            "tags": "3-5年 | 本科",
                            "boss_name": "示例公司",
                            "skills": "Python | FastAPI",
                            "job_link": "https://www.zhipin.com/job_detail/cli-job.html",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
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
            job_keyword="Python", city="上海", experience="3-5年", degree="本科", salary="20-30K", pages=7
        )
    )

    assert "--detail-output" in observed["args"]
    assert observed["args"][observed["args"].index("--pages") + 1] == "7"
    assert "--detail" in observed["args"]
    assert "--max-details" not in observed["args"]
    assert observed["timeout"] == 6000
    assert [
        observed["args"][observed["args"].index(flag) + 1]
        for flag in ("--salary", "--experience", "--degree")
    ] == ["406", "105", "203"]
    assert [job.id for job in jobs] == ["cli-job"]
    assert jobs.summary.total_jobs == jobs.summary.total_details == 1
    assert jobs.summary.pages == 7
    assert jobs.summary.filters == {"experience": "3-5年", "degree": "本科", "salary": "20-30K"}
    assert "岗位市场摘要: Python @ 上海" in jobs.summary.formatted_summary


def test_setup_opens_boss_homepage_in_the_dedicated_chrome(monkeypatch):
    launched, opened_urls = [], []

    monkeypatch.setattr("app.boss.shutil.which", lambda command: "/fake/boss-scraper")
    monkeypatch.setattr("app.boss.subprocess.Popen", lambda args, **kwargs: launched.append(args))
    monkeypatch.setattr("app.boss._open_boss_homepage", lambda: opened_urls.append("https://www.zhipin.com") or True, raising=False)

    status = BossAdapter().setup()

    assert status.state == "login_required"
    assert launched == [["boss-scraper", "--setup-chrome", "--no-wait-login"]]
    assert opened_urls == ["https://www.zhipin.com"]


def test_conditions_default_blank_city_to_beijing():
    conditions = ResumeConditions(
        job_keyword="Python", city="", experience="不限", degree="不限", salary="不限"
    )

    assert conditions.city == "北京"


def test_freeform_salary_only_uses_a_containing_upstream_bucket():
    assert salary_code("20-30K") == "406"
    assert salary_code("20-50K") == "406"
    assert salary_code("5-20K") is None


def test_experience_alias_and_multiple_ranges_are_handled_without_loss():
    assert experience_code("10年") == "107"
    assert experience_code("5-10年,10年以上") is None

    jobs = [
        normalize_job({
            "job_id": "five-to-ten", "title": "Python 工程师", "job_link": "https://example.test/1",
            "jd": "Python", "location": "上海", "tags_list": "5-10年 | 本科", "salary": "20-30K",
        }),
        normalize_job({
            "job_id": "over-ten", "title": "Python 工程师", "job_link": "https://example.test/2",
            "jd": "Python", "location": "上海", "tags_list": "10年以上 | 本科", "salary": "20-30K",
        }),
    ]
    conditions = ResumeConditions(
        job_keyword="Python", city="上海", experience="5-10年,10年以上", degree="本科", salary="20-30K"
    )

    assert [job.id for job in filter_jobs(jobs, conditions)] == ["five-to-ten", "over-ten"]


def test_five_hard_filters_keep_only_matching_job():
    jobs = [normalize_job(item) for item in json.loads(FIXTURE_PATH.read_text())]
    conditions = ResumeConditions(
        job_keyword="Python", city="上海", experience="3-5年", degree="本科", salary="20-30K"
    )

    assert [job.id for job in filter_jobs(jobs, conditions)] == ["new-job"]


def test_city_code_is_not_compared_to_chinese_detail_location():
    job = normalize_job(json.loads(FIXTURE_PATH.read_text())[0])
    conditions = ResumeConditions(
        job_keyword="Python", city="101020100", experience="3-5年", degree="本科", salary="20-30K"
    )

    assert [item.id for item in filter_jobs([job], conditions)] == ["new-job"]


def test_first_discovery_and_active_transition_pools_are_exclusive():
    jobs = [normalize_job(item) for item in json.loads(FIXTURE_PATH.read_text())]
    pools = select_candidate_pools(
        jobs,
        previous_buckets={"new-job": "inactive", "filtered-city": "active"},
        newly_seen={"new-job"},
    )

    assert [job.id for job in pools.new_published] == ["new-job"]
    assert pools.new_active == []


def test_changed_job_is_rechecked_without_a_pool_size_limit():
    source = normalize_job(json.loads(FIXTURE_PATH.read_text())[0])
    jobs = [source.model_copy(update={"id": f"job-{index}"}) for index in range(25)]
    pools = select_candidate_pools(
        jobs,
        previous_buckets={job.id: "recent" for job in jobs},
        newly_seen=set(),
        changed_ids={job.id for job in jobs},
    )

    assert [job.id for job in pools.new_published] == [f"job-{index}" for index in range(25)]
    assert pools.new_active == []


def test_new_active_pool_only_keeps_just_active_raw_status():
    just_active = normalize_job(json.loads(FIXTURE_PATH.read_text())[0])
    today_active = just_active.model_copy(update={"id": "today", "active_status_raw": "今日活跃", "active_bucket": "recent"})
    pools = select_candidate_pools(
        [just_active, today_active],
        previous_buckets={just_active.id: "recent", today_active.id: "inactive"},
        newly_seen=set(),
        previous_active_statuses={just_active.id: "今日活跃", today_active.id: "两周内活跃"},
    )

    assert [job.id for job in pools.new_active] == [just_active.id]


def test_first_scan_active_job_enters_new_active_pool():
    job = normalize_job(json.loads(FIXTURE_PATH.read_text())[0])
    pools = select_candidate_pools([job], previous_buckets={}, newly_seen=set())
    assert [item.id for item in pools.new_active] == ["new-job"]


def test_inactive_to_active_transition_enters_new_active_pool():
    job = normalize_job(json.loads(FIXTURE_PATH.read_text())[0])
    pools = select_candidate_pools([job], previous_buckets={"new-job": "inactive"}, newly_seen=set())
    assert [item.id for item in pools.new_active] == ["new-job"]


def test_sorting_is_descending_stable_and_retains_all_scored_jobs():
    scored = [
        ScoredJob(job_id=f"job-{index}", score=100 - index, reason="匹配", strengths=[], gaps=[])
        for index in range(12)
    ]
    result = sort_scored_results(scored)

    assert [item.job_id for item in result] == [f"job-{index}" for index in range(12)]
