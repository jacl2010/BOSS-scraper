# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本地单用户简历匹配 MVP：上传 PDF 简历 → LLM 生成结构化画像 → 通过 `boss-zhipin-scraper`（固定 commit 的 git 依赖）复用本机 BOSS 专用 Chrome 登录态采集岗位 → 确定性条件过滤 → LLM 评分。FastAPI 仅监听 `127.0.0.1`，数据存本机 SQLite（`data/app.db`）。

## 常用命令

```bash
# 启动（macOS，检查 uv 0.12.x、uv sync、起 8765 端口并打开浏览器）
./start.command

# 手动启动
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765

# 测试（全部 / 单个文件 / 单个用例）
uv run pytest
uv run pytest tests/test_matching.py
uv run pytest tests/test_matching.py::test_normalizes_boss_job_and_active_status

# Lint
uv run ruff check .

# 依赖同步（走 uv.lock，默认清华镜像）
uv sync --frozen
```

LLM API Key 通过环境变量 `BOSS_MATCHER_LLM_API_KEY` 或根目录 `.env` 提供。

## 架构

后端全部在 `app/` 下，前端是 `app/web/` 中无构建步骤的 Vue 3 全局构建（Vue 从 `app/web/vendor/` 加载，无 npm）。

**依赖注入**：`create_app()`（[app/main.py](app/main.py)）接受 `database/llm/boss` 可选参数并在 `app.state` 上暴露各 service——测试通过注入 fake 替身（见 `tests/test_api_smoke.py` 用 `TestClient` + stub `BossAdapter`/`LlmService`）避免真实采集和 LLM 调用。新增路由时保持这个模式。

**模块职责**：
- `main.py` — 路由与错误格式。所有错误统一为 `{"error": {"code", "message"}}` 结构（`_error()` helper），LLM 异常映射为中文用户提示（`_llm_configuration_message()`）。
- `boss.py` — 适配 `boss-zhipin-scraper` CLI 输出并标准化为 `CanonicalJob`（含薪资 K 值解析、活跃状态分桶 `active_bucket`）。
- `resumes.py` — PDF 校验（≤10MB、可提取文本、最多 10 份、正文截断 24,000 字符）与生命周期。
- `matching.py` — 核心匹配逻辑，监控与匹配拆分为两个任务（共用一个带锁的单例 `MatchRunner`，同一时刻只允许一个任务运行，进程重启不恢复）：
  - `start_monitor()`（POST /api/monitor）：检查 → 采集 → 过滤。按全局监控条件（`monitor_settings` 表）采集岗位写入 jobs 表，保存全局采集摘要，不写 resume_job_states。
  - `start_match(resume_id)`（POST /api/matches）：评分 → 整理。从 jobs 表读岗位，按全局条件过滤后分池评分；已评分且 `matching_content_hash` 未变的岗位跳过；结果按 (resume_id, job_id) **累积 upsert**。
  - `filter_jobs()` 确定性过滤（关键词/城市/经验/学历/薪资）。注意城市条件可能是 9 位数字代码，此时跳过城市比对。
  - `select_candidate_pools()` 把岗位分入两个结果池：**new_published**（首次见到或 `matching_content_hash` 变化）和 **new_active**（招聘者状态首次变为"刚刚活跃"）。
- `llm.py` — LangChain OpenAI 兼容调用；简历解析与岗位评分按 10 个/批调用。
- `prompts/` — Markdown 提示词文件（`resume_parse_v1.md`、`job_match_v1.md`）。
- `database.py` — SQLite 持久化；监控条件（`monitor_settings`，全局单行）与采集摘要（`monitor_summaries`，全局单行）独立于简历；匹配结果按简历累积保留，rank 读取时按分数计算。
- `schemas.py` — Pydantic 模型（`CanonicalJob`、`ResumeProfile`、`ScoredJob`、`MatchStatus` 等）。

**测试特点**：`tests/test_api_smoke.py` 除 API 外还断言前端静态资源内容（`app.js`/`app.css` 的特定字符串，如 v-cloak、select-menu 弹层逻辑）——修改前端时这些测试会失败，需同步更新。

## 规格文档

`specs/001-local-resume-job-matching/` 含完整 spec（spec.md、data-model.md、ui-spec.md、tasks.md 等），是需求的权威来源。

## 约束

- 不绕过验证码/风控；遇平台限制应停止任务（产品原则，也是合规声明要求）。
- API Key 不通过任何查询接口返回明文（只返回 masked）。
- Python 锁定 `>=3.12,<3.13`；ruff line-length 100。
