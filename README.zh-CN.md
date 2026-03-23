# software-factory

[README in English](README.md) | 简体中文

`software-factory` 是一个基于 FastAPI 的轻量 issue/PR 驱动自动开发系统。

它关注的是把 issue intake、PR 审查反馈、自动修复执行和结果回写，串成一个可追踪、可扩展、可本地运行的闭环。

核心思路是：由 issue、Hook 和 GitHub Webhook 决定何时开始工作；由 Agent Worker 执行代码变更流程；页面只展示必要的运行态信息。

## 项目简介与目标

当前 review 流程里，常见重复动作通常是：

"看评论 -> 整理问题 -> 再次让 AI 修复"。

本项目希望把这段流程标准化为自动闭环：

- 用 issue 或人工提供的 issue-like 输入作为人工触发入口
- 用 Hook 记录受管开发会话，作为确定性触发来源
- 用 GitHub Webhook 感知 issue、PR review 和 comment 变化
- 用 Review Normalizer 把原始反馈归一化为结构化修复任务
- 用 Agent Worker 执行修复、检查、提交、推送和 GitHub 回写
- 用 Thin Web 展示最近任务、状态和错误摘要

非目标：

- 不做重型多租户后台
- 不做完整审批和权限中心

## 项目定位

本项目不追求做成通用 CI/CD 平台，也不追求覆盖完整 DevOps 生命周期。更准确地说，它是：

- `Issue/PR-driven Autonomous Development System`
- `AI-native GitHub Issue & PR Orchestrator`

它的关注点是：

- 由 issue、Hook 和 Webhook 决定何时触发执行
- 由 Normalizer 把 issue/review/comment 变成结构化修复输入
- 由 Agent Worker 执行修改、验证、提交、推送和 GitHub 回写

如果拿常见开源系统做类比，它更接近：

- `OpenHands` / `SWE-agent` 这类 AI 执行代理
- `Prow` / `Zuul` 这类事件驱动的 review / CI 编排系统

而不是 `Harness`、`GitLab` 这类覆盖范围更大的通用 DevOps 平台。

## 核心架构

```text
Hook (Claude Code lifecycle)
  -> Local Orchestrator API
    -> State Store / Queue
      -> GitHub Webhook Adapter
      -> Review Normalizer
      -> Agent Worker
      -> Thin Web
```

组件职责：

- Hook：上报受管会话事件，不做语义决策
- Webhook Adapter：接收 GitHub 事件，校验签名，并对原始事件做去重
- Normalizer：把 issue、review 和 comment 输入转换为结构化 autofix 任务
- Agent Worker：checkout 代码、执行修复、跑校验并回写结果
- Thin Web：展示运行记录、状态和错误摘要

## 当前进度

Milestone 概览：

- M1（已完成）：最小可跑骨架
  - FastAPI 服务、健康检查、SSR 页面
  - `/hook-events`、`/github/webhook` 占位接口
  - SQLite 初始化脚本和核心表结构
  - 基础 CI（`OpenCode Review` + `Pytest`）
- M2（已完成）：事件入库、幂等和会话关联
  - Hook/GitHub 事件的结构化解析与落库
  - 去重键、错误处理与状态推进
  - 会话与 PR 关联映射
- M3（已完成）：GitHub Webhook 完整接入
  - 签名验证、事件防抖
  - `pull_request_review`、`pull_request_review_comment`、`issue_comment` 事件处理
- M4（已完成）：Review Normalizer
  - 归一化 review/comment 为结构化修复任务
  - 去重、分级（`P0-P3`）和噪声过滤
- M5（已完成）：Agent Worker 执行链路
  - Git checkout / commit / push
  - 检查命令执行（`lint` / `test`）
  - PR 评论回写
- M6（已完成）：稳定性增强
  - 幂等键去重，防止重复任务
  - PR 锁，防止并发冲突
  - 指数退避重试
  - 单 PR 自动修复次数限制（`MAX_AUTOFIX_PER_PR`）
  - Bot / 噪声评论过滤
  - 日志归档和保留策略
- M7（进行中）：文档与测试完善
  - 系统架构文档
  - 故障排除指南
  - E2E 集成测试
  - 压力测试

本文档未明确列出的能力，默认视为不在当前项目阶段范围内。

## 安装

1. 创建 Python 3.11 虚拟环境并安装依赖。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

2. 创建本地环境配置。

```bash
cp example.env .env
```

3. 为所有本地进程设置同一个 `DB_PATH`。规则和错误模式见 [docs/local-runtime.md](docs/local-runtime.md)。

```bash
export DB_PATH="$(pwd)/data/software_factory.db"
```

4. 初始化数据库。

```bash
python scripts/init_db.py
```

5. 启动 Web 服务。

```bash
env DB_PATH="$DB_PATH" uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

6. 如有需要，用相同的 `DB_PATH` 启动 worker。

```bash
env DB_PATH="$DB_PATH" python scripts/run_worker.py --loop --workspace-dir "$(pwd)"
```

### 面向 LLM 的安装 Prompt

当你想让 Codex / Claude / OpenCode 帮你本地安装并验证项目时，可以直接复制下面这段：

```text
Install and verify this repository locally.

Requirements:
- Follow README.md and docs/local-runtime.md exactly.
- Use Python 3.11+ and install dependencies from requirements.txt in a virtual environment.
- Copy example.env to .env if needed.
- Choose one writable DB_PATH and use the exact same DB_PATH for every local process.
- Do not let the web service use ./data/software_factory.db while the worker uses a different database.
- Initialize the SQLite database with python scripts/init_db.py.
- Start the web service on port 8001.
- If you start the worker, it must use the same DB_PATH as the web service.
- Verify the setup with curl -i http://127.0.0.1:8001/healthz.
- If both web and worker are running, verify that both processes expose the same DB_PATH.
- Do not modify application code just to make local setup pass. Only change local env/config when needed.
- If something fails, report the exact failing command, the root cause, and the smallest fix.
```

## 本地运行

本地运行细节和 `DB_PATH` 约束见 [docs/local-runtime.md](docs/local-runtime.md)。

1. 创建虚拟环境并安装依赖。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

运行要求：

- Python 3.11+
- SQLite 3.35+（`autofix_runs` 队列 claim 使用 `RETURNING`；更低版本会自动回退到兼容路径）

2. 配置环境变量。

```bash
cp example.env .env
```

按需编辑 `.env`，括号内为默认值：

基础配置：

- `APP_ENV`（`development`）：运行环境标识
- `HOST`（`127.0.0.1`）：服务监听地址
- `PORT`（`8000`）：服务监听端口
- `DB_PATH`（`./data/software_factory.db`）：SQLite 文件路径
- `GITHUB_WEBHOOK_SECRET`（空字符串）：本地联调可留空；生产环境建议启用签名校验

Webhook 配置：

- `GITHUB_WEBHOOK_DEBOUNCE_SECONDS`（`60`）：防抖窗口秒数

稳定性配置（M6）：

- `MAX_AUTOFIX_PER_PR`（`3`）：单 PR 最大自动修复次数
- `MAX_CONCURRENT_RUNS`（`3`）：最大并发任务数
- `PR_LOCK_TTL_SECONDS`（`900`）：PR 锁 TTL 秒数
- `MAX_RETRY_ATTEMPTS`（`3`）：最大重试次数
- `RETRY_BACKOFF_BASE_SECONDS`（`30`）：重试基础延迟
- `RETRY_BACKOFF_MAX_SECONDS`（`1800`）：重试最大延迟

过滤配置（M6）：

- `BOT_LOGINS`（空）：Bot 账号列表，逗号分隔，例如 `github-actions[bot],dependabot[bot]`
- `NOISE_COMMENT_PATTERNS`（空）：噪声评论正则，逗号分隔，例如 `^/retest\b,^/resolve\b`
- `MANAGED_REPO_PREFIXES`（空）：纳管仓库前缀，逗号分隔，例如 `acme/,widgets/`
- `AUTOFIX_COMMENT_AUTHOR`（`software-factory[bot]`）：自动修复评论作者标识

日志配置（M6）：

- `LOG_DIR`（`logs`）：日志目录
- `LOG_ARCHIVE_SUBDIR`（`archive`）：日志归档子目录
- `LOG_RETENTION_DAYS`（`7`）：日志保留天数
- `WORKER_ID`（`worker-default`）：Worker 标识

3. 初始化数据库。

```bash
python scripts/init_db.py
```

会创建四张核心表：`sessions`、`pull_requests`、`review_events`、`autofix_runs`。

4. 启动 Web 服务。

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

5. 或者后台一起启动 `web + worker`。

```bash
chmod +x scripts/start_system_bg.sh
./scripts/start_system_bg.sh start
```

常用管理命令：

```bash
./scripts/start_system_bg.sh status
./scripts/start_system_bg.sh logs
./scripts/start_system_bg.sh stop
```

说明：

- 脚本会优先加载仓库根目录 `.env`
- `web` 和 `worker` 会强制共用同一个 `DB_PATH`
- PID 文件和日志默认写到 `.runtime/local/`

## 开发与调试命令

健康检查：

```bash
curl -i http://127.0.0.1:8001/healthz
```

模拟 Hook 事件（`/hook-events`）：

```bash
curl -i -X POST http://127.0.0.1:8001/hook-events \
  -H 'content-type: application/json' \
  -d '{"event":"UserPromptSubmit","session_id":"sess_demo","repo":"owner/repo","branch":"feat/demo","cwd":"/tmp/software-factory","timestamp":"2026-03-12T12:00:00Z"}'
```

说明：当前实现通过 JSON body 的 `event` 字段识别事件类型，不读取 `x-event-type` header。

模拟 GitHub Webhook（`/github/webhook`）：

```bash
curl -i -X POST http://127.0.0.1:8001/github/webhook \
  -H 'content-type: application/json' \
  -H 'x-github-event: pull_request_review' \
  -d '{"action":"submitted","review":{"id":123},"pull_request":{"number":10}}'
```

说明：`/github/webhook` 已实现签名校验；生产环境请务必配置 `GITHUB_WEBHOOK_SECRET`。

语法 / 字节码编译检查：

```bash
python -m compileall app scripts
```

`compileall` 仅用于语法和导入层面的快速检查，不替代静态分析。建议按需增加 `ruff`、`mypy` 等工具。

Worker 调试：

```bash
python scripts/run_worker.py --once
```

MVP 默认单 worker 串行执行；多 worker 并发通过 `MAX_CONCURRENT_RUNS` 控制。

## Requirements Workflow

仓库当前使用 OpenSpec 在代码合入前跟踪产品需求、遗漏的 review 项和实现范围。

常用命令：

```bash
openspec list
openspec show issue-to-pr-autofix
openspec status --change issue-to-pr-autofix
openspec validate issue-to-pr-autofix
```

仓库工作流见 [openspec/README.md](openspec/README.md)。

## CI 说明

仓库当前包含两个工作流：

- `OpenCode PR Review`：在 PR 上执行只读 AI 审查，并输出中文 review 建议
- `Pytest`：安装依赖并运行 `pytest -q`，用于基础回归检查

它们共同服务于“快速反馈 + 轻量治理”的目标。

## 目录结构

```text
app/        FastAPI 应用、路由、服务、模板和静态资源
scripts/    本地运行和维护脚本
tests/      测试集
docs/       架构、故障排除和 Hook 示例
openspec/   需求跟踪和变更规格
```

## 文档

- [系统架构文档](docs/architecture.md)
- [故障排除指南](docs/troubleshooting.md)
- [Hook 示例](docs/hook-samples.md)
- [本地运行说明](docs/local-runtime.md)
- [OpenSpec 工作流](openspec/README.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [行为准则](CODE_OF_CONDUCT.md)

## 常用页面

- `http://127.0.0.1:8001/`
- `http://127.0.0.1:8001/runs`
- `http://127.0.0.1:8001/runs/demo-run`

## Docker

构建主服务镜像：

```bash
docker build -t svtter/software-factory:latest .
```

运行 Web 应用：

```bash
docker run --rm -p 8000:8000 \
  -e PORT=8000 \
  -e DB_PATH=/app/data/software_factory.db \
  svtter/software-factory:latest
```

如果需要运行 worker，可复用同一个镜像并覆盖启动命令：

```bash
docker run --rm \
  -e DB_PATH=/app/data/software_factory.db \
  svtter/software-factory:latest \
  python scripts/run_worker.py --loop --workspace-dir /app
```

## 后续路线

- 短期（M7）：完善文档、E2E 测试和压力测试
- 中期（规划中）：多仓库支持、手动暂停/恢复、更丰富的策略控制
- 长期（规划中）：PostgreSQL 支持、多 worker 集群、可观测性增强

## 常见问题

- 数据库初始化失败：先确认 `DB_PATH` 的父目录存在且可写，再重试 `python scripts/init_db.py`
- `8000` 端口被占用：改用其他端口启动，例如 `--port 8001`
- Webhook 调试无响应：检查 `content-type`、`x-github-event` 和 JSON 格式是否正确
- Worker 不执行任务：检查队列状态、并发限制和 PR 锁；详见 [docs/troubleshooting.md](docs/troubleshooting.md)

## 开源协议

本项目采用 [Apache License 2.0](LICENSE)。
