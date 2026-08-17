# BOSS 简历匹配器

> 本地运行的简历解析与 BOSS 岗位匹配 MVP：管理 PDF 简历，按条件采集岗位，并通过大模型生成匹配评分、优势与能力缺口。

![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.116.1-009688?logo=fastapi&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

## 项目定位

这个项目用于验证一条尽量精简的本地求职辅助流程：

1. 上传 PDF 简历并提取文本；
2. 使用兼容 OpenAI API 的大模型生成结构化候选人画像；
3. 为每份简历设置岗位关键词、城市、经验、学历、薪资和采集页数；
4. 复用 [`boss-zhipin-scraper`](https://github.com/eatmoreduck/boss-zhipin-scraper)，通过本机 BOSS 专用 Chrome 的登录状态采集岗位列表和 JD；
5. 先执行确定性条件过滤，再由大模型评分；
6. 分别展示“本次新发现或内容变化”和“新活跃（刚刚活跃）”岗位。

当前仓库实现的是**本地单用户 Web 应用**，FastAPI 仅监听 `127.0.0.1`，简历、配置和结果保存在本机 SQLite 中。它不是 Chrome 扩展，也不是已经部署到 Vercel、Cloudflare 或其他云平台的在线服务。

## 功能

- 最多保存 10 份 PDF 简历；单份不超过 10 MB；
- 从可提取文本的 PDF 中生成职位、摘要、技能、经历、学历和目标岗位等结构化信息；
- 为每份简历独立保存岗位匹配条件；
- 启动、检查并复用隔离的 BOSS 专用 Chrome 登录状态；
- 采集岗位列表、岗位详情 JD、招聘者活跃状态和岗位市场摘要；
- 按城市、经验、学历、薪资等硬条件预筛选岗位；
- 由大模型输出匹配分、匹配理由、优势和缺口；
- 保存最近一次完整匹配结果，并在页面中展示采集进度和错误状态；
- API Key 仅保存在进程环境或项目根目录的本地 `.env` 文件中，不通过查询接口返回明文。

## 结果池说明

项目将结果分成两个独立池，二者含义不同：

- **新发布 / 本次新发现**：某个 `job_id` 首次被当前简历的匹配任务发现，或岗位的匹配相关内容发生变化。没有可靠发布时间时，“本次新发现”不等于“刚刚发布”。
- **新活跃（刚刚活跃）**：已存在的岗位，其招聘者状态本轮首次变为“刚刚活跃”。

## 运行前提

- macOS；仓库提供的 `start.command` 使用了 macOS 的 `open` 命令；
- Python `>=3.12,<3.13`（可由 uv 管理安装）；
- [`uv`](https://docs.astral.sh/uv/) `0.12.x`；
- 已安装 Google Chrome；
- 可正常使用的 BOSS 账号，登录必须由用户在专用 Chrome 中手动完成；
- 一个兼容 OpenAI API 的大模型服务及 API Key；
- 首次同步依赖时可以访问清华 PyPI 镜像和 GitHub。项目通过 `uv.lock` 固定了依赖版本和下载来源。

> 其他操作系统可以尝试使用手动启动命令，但当前仓库只提供 macOS 一键启动脚本，也未声明已经完成跨平台兼容性验证。

## 快速开始

### 1. 获取代码

使用 Git 克隆仓库并进入项目目录（如果仓库地址不同，请替换为 GitHub 页面 **Code** 菜单中的实际地址）：

```bash
git clone https://github.com/jacl2010/BOSS-scraper.git
cd BOSS-scraper
```

也可以在 GitHub 页面下载 ZIP。获取代码后，请进入包含 `pyproject.toml` 和 `uv.lock` 的项目根目录。

### 2. 安装 uv 与 Python

macOS 可以使用 uv 官方安装脚本安装 uv：

```bash
curl -LsSf https://astral.sh/uv/0.12.1/install.sh | sh
```

安装脚本完成后重新打开终端，然后确认 uv 版本：

```bash
uv --version
```

项目要求 uv `0.12.x`；`start.command` 会检查版本，不满足时会停止启动。

使用 uv 官方命令安装项目所需的 Python 3.12：

```bash
uv python install 3.12
uv python find 3.12
```

`uv python install 3.12` 会安装符合 `>=3.12,<3.13` 要求的最新 Python 3.12 补丁版本。也可以参考 [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/) 和 [Python 官方 macOS 下载页面](https://www.python.org/downloads/macos/) 选择其他安装方式。

### 3. 启动应用

macOS 可以直接双击 `start.command`，也可以在终端执行：

```bash
./start.command
```
```

## 使用流程

### 1. 配置大模型

打开“LLM API Key”页面，填写服务地址、模型标识和 API Key，然后点击“测试并保存”。

- 页面保存的新 Key 会写入项目根目录 `.env` 中的 `BOSS_MATCHER_LLM_API_KEY`；
- 也可以在启动前通过环境变量提供 Key：
```

### 2. 上传并配置简历

进入“简历管理”页面，上传可直接提取文本的 PDF。解析成功后，为该简历设置：

- 岗位关键词；
- 城市；
- 工作经验；
- 学历；
- 薪资；
- 采集页数（1～10 页）。

保存条件并打开“参与匹配”开关。扫描件、图片型 PDF、加密 PDF 和 OCR 不在当前 MVP 范围内。

### 3. 准备 BOSS 专用 Chrome

进入“BOSS 岗位匹配”页面：

1. 点击“打开 Chrome”；
2. 在新打开的 BOSS 专用 Chrome 中手动登录；
3. 回到应用点击“检查状态”；
4. 状态显示“已就绪”后选择一份简历并开始匹配。

应用不会绕过验证码或平台限制。遇到登录失效、验证码、访问频率限制或风控提示时，请停止任务并人工处理。

### 4. 查看结果

匹配过程依次执行状态检查、岗位采集、条件过滤、AI 评分和结果保存。完成后可以查看：

- 本次采集条件和岗位市场摘要；
- “新发布 / 本次新发现”结果池；
- “新活跃（刚刚活跃）”结果池；
- 每个岗位的匹配分、理由、优势、缺口、JD 和 BOSS 原始链接。

## 数据与隐私

默认本地数据位置：

| 数据 | 位置 | 说明 |
| --- | --- | --- |
| SQLite 数据库 | `data/app.db` | 保存 LLM 非敏感配置、简历画像、岗位和最近结果 |
| 原始简历 | `data/resumes/` | 以随机 ID 命名保存 |
| LLM API Key | 环境变量或 `.env` | 变量名为 `BOSS_MATCHER_LLM_API_KEY` |
| BOSS 登录状态 | `boss-zhipin-scraper` 管理的本机 Chrome profile | 不写入本项目数据库 |


需要注意：文件和数据库保存在本机，不代表所有处理都完全离线。为了完成简历解析和岗位评分，应用会把**提取后的简历文本、结构化简历画像和岗位 JD**发送给你配置的大模型服务。请自行确认该服务商的数据处理、保留和隐私政策。

## 技术栈

- 后端：Python 3.12、FastAPI、Pydantic；
- 前端：Vue 3 全局生产构建，无前端构建步骤；
- 数据库：SQLite；
- PDF 文本提取：PyMuPDF；
- 大模型接入：LangChain、`langchain-openai`；
- 岗位采集：固定版本的 `boss-zhipin-scraper`、Chrome DevTools Protocol；
- 依赖管理：uv。

## 目录结构

```text
.
├── app/
│   ├── main.py            # FastAPI 入口与 HTTP API
│   ├── boss.py            # BOSS 采集器适配与岗位标准化
│   ├── resumes.py         # PDF 校验、文本提取与简历生命周期
│   ├── matching.py        # 条件过滤、结果池和评分流程
│   ├── llm.py             # 大模型配置、解析与评分调用
│   ├── database.py        # SQLite 持久化
│   ├── prompts/           # 简历解析与岗位匹配提示词
│   └── web/               # Vue 单页界面及静态资源
├── licenses/              # 随源码分发的第三方许可证
├── tests/                 # 自动化测试
├── THIRD_PARTY_NOTICES.md # 第三方组件与许可证说明
├── pyproject.toml         # 项目元数据和依赖声明
├── uv.lock                # 锁定依赖版本和来源
└── start.command          # macOS 一键启动脚本
```

## 当前限制

- 仅支持可提取文本的 PDF，不支持 DOCX、图片简历或 OCR；
- 单份简历正文最多提取 24,000 个字符；
- 同一时间只运行一个内存中的匹配任务；重启进程不会恢复正在执行的任务；
- 没有定时任务、后台常驻监控、多用户、账号系统或权限隔离；
- 只保留每份简历最近一次完整匹配结果，不提供完整任务历史；
- BOSS 登录状态、网页结构和平台限制变化都可能影响采集；
- “本次新发现”不能作为岗位真实发布时间的证明；
- 当前应用只监听回环地址，不应直接暴露到公网。

## 合规声明

本项目仅供个人研究与技术验证，不隶属于 BOSS 直聘，也未获得 BOSS 直聘授权。

开源许可证只授权使用本项目代码，不代表授予任何第三方平台的数据访问、采集或商业使用权。使用者应自行阅读并遵守 BOSS 直聘用户协议、目标网站规则、适用法律法规及大模型服务商条款，并对账号、数据和使用行为负责。

请勿使用本项目绕过验证码、登录控制、访问频率限制或其他技术措施；遇到平台限制时应停止自动操作。公开部署、商业化、多账号或大规模采集均不属于本项目当前设计范围。

## 第三方许可

仓库内包含 Vue 和 `boss-zhipin-scraper` 的第三方代码或分发文件，具体版本、来源及许可证见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 和 [`licenses/`](licenses/)。Python 依赖的许可证信息由各上游发行包保留。

## License

本项目代码基于 [MIT License](LICENSE) 发布，版权归 `jacl2010` 所有。
