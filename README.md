# software-factory

一个基于 FastAPI 的轻量自动化编排项目，目标是把 PR 审查反馈闭环做成可追踪、可扩展、可本地运行的最小系统。

核心思路是：触发由 Hook 和 GitHub Webhook 决定，执行由 Agent Worker 负责，页面只做必要状态展示。

## 项目简介与目标

当前开发流程里，review 之后常见重复动作是"看评论 -> 整理问题 -> 再次让 AI 修复"。

本项目希望把这段流程标准化为自动闭环：

- 用 Hook 记录受管开发会话（确定性触发）
- 用 GitHub Webhook 感知 PR review/comment 变化
- 用 Review Normalizer 归一化修复输入
- 用 Agent Worker 执行代码修复、测试与回写
- 用 Thin Web 提供任务状态可视化

非目标：不做重型多租户后台、不做复杂审批和权限中心。

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

- Hook：在固定事件点上报会话与上下文，不做语义决策
- Webhook Adapter：接收并解析 GitHub 事件，做基础去重/归档
- Normalizer：把 review/comment 统一成结构化修复任务
- Agent Worker：按策略执行修复任务并回写 PR
- Thin Web：展示最近任务、状态、错误摘要

## 当前进度

Milestone 概览：

- M1（已完成）：最小可跑骨架
  - FastAPI 服务、健康检查、SSR 页面
  - `/hook-events`、`/github/webhook` 占位接口
  - SQLite 初始化脚本和核心表结构
  - 基础 CI（OpenCode Review + Pytest）
- M2（已完成）：事件入库、幂等和会话关联
  - Hook/GitHub 事件的结构化解析与落库
  - 去重键、错误处理与状态推进
  - 会话与 PR 关联映射
- M3（已完成）：GitHub Webhook 完整接入
  - 签名验证、事件防抖
  - `pull_request_review`、`pull_request_review_comment`、`issue_comment` 事件处理
- M4（已完成）：Review Normalizer
  - 归一化 review/comment 为结构化修复任务
  - 去重、分级 (P0-P3)、过滤噪声
- M5（已完成）：Agent Worker 执行链路
  - Git checkout/commit/push
  - 检查命令执行 (lint/test)
  - PR 评论回写
- M6（已完成）：稳定性增强
  - 幂等键去重，防止重复任务
  - PR 锁，防止并发冲突
  - 指数退避重试机制
  - 自动修复次数限制 (MAX_AUTOFIX_PER_PR)
  - Bot/噪声评论过滤
  - 日志归档与保留策略
- M7（进行中）：文档与测试完善
  - 系统架构文档
  - 故障排除指南
  - E2E 集成测试
  - 压力测试

说明：本文档描述已实现功能和明确规划中的能力，未列出的功能视为不在当前范围内。

## 本地运行

1) 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

运行要求：

- Python 3.11+
- SQLite 3.35+（`autofix_runs` 队列 claim 使用 `RETURNING`，低版本会自动降级到兼容路径）

2) 配置环境变量

```bash
cp example.env .env
```

按需编辑 `.env`（括号内为默认值）：

**基础配置**:
- `APP_ENV`（`development`）：运行环境标识
- `HOST`（`127.0.0.1`）：服务监听地址
- `PORT`（`8000`）：服务监听端口
- `DB_PATH`（`./data/software_factory.db`）：SQLite 文件路径
- `GITHUB_WEBHOOK_SECRET`（空字符串）：本地联调可留空；生产环境建议配置并启用签名校验

**Webhook 配置**:
- `GITHUB_WEBHOOK_DEBOUNCE_SECONDS`（`60`）：防抖窗口秒数

**稳定性配置 (M6)**:
- `MAX_AUTOFIX_PER_PR`（`3`）：单 PR 最大自动修复次数
- `MAX_CONCURRENT_RUNS`（`3`）：最大并发任务数
- `PR_LOCK_TTL_SECONDS`（`900`）：PR 锁 TTL 秒数
- `MAX_RETRY_ATTEMPTS`（`3`）：最大重试次数
- `RETRY_BACKOFF_BASE_SECONDS`（`30`）：重试基础延迟秒数
- `RETRY_BACKOFF_MAX_SECONDS`（`1800`）：重试最大延迟秒数

**过滤配置 (M6)**:
- `BOT_LOGINS`（空）：Bot 账号列表，逗号分隔，如 `github-actions[bot],dependabot[bot]`
- `NOISE_COMMENT_PATTERNS`（空）：噪声评论正则，逗号分隔，如 `^/retest\b,^/resolve\b`
- `MANAGED_REPO_PREFIXES`（空）：纳管仓库前缀，逗号分隔，如 `acme/,widgets/`
- `AUTOFIX_COMMENT_AUTHOR`（`software-factory[bot]`）：自动修复评论作者

**日志配置 (M6)**:
- `LOG_DIR`（`logs`）：日志目录
- `LOG_ARCHIVE_SUBDIR`（`archive`）：日志归档子目录
- `LOG_RETENTION_DAYS`（`7`）：日志保留天数
- `WORKER_ID`（`worker-default`）：Worker 标识

3) 初始化数据库

```bash
python scripts/init_db.py
```

会创建四张核心表：`sessions`、`pull_requests`、`review_events`、`autofix_runs`。

4) 启动服务

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

常用页面：

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/runs`
- `http://127.0.0.1:8000/runs/demo-run`

## 开发与调试命令

健康检查：

```bash
curl -i http://127.0.0.1:8000/healthz
```

模拟 Hook 事件（`/hook-events`）：

```bash
curl -i -X POST http://127.0.0.1:8000/hook-events \
  -H 'content-type: application/json' \
  -d '{"event":"UserPromptSubmit","session_id":"sess_demo","repo":"owner/repo","branch":"feat/demo","cwd":"/tmp/software-factory","timestamp":"2026-03-12T12:00:00Z"}'
```

说明：当前实现通过 JSON body 的 `event` 字段识别事件类型，不读取 `x-event-type` header。

模拟 GitHub Webhook（`/github/webhook`）：

```bash
curl -i -X POST http://127.0.0.1:8000/github/webhook \
  -H 'content-type: application/json' \
  -H 'x-github-event: pull_request_review' \
  -d '{"action":"submitted","review":{"id":123},"pull_request":{"number":10}}'
```

说明：当前 `/github/webhook` 已实现签名校验；生产环境请务必配置 `GITHUB_WEBHOOK_SECRET`。

语法/字节码编译检查：

```bash
python -m compileall app scripts
```

说明：`compileall` 仅用于语法与导入层面的快速检查，不替代静态分析。建议按需增加 `ruff`、`mypy` 等工具。

Worker 调试（M5）：

```bash
python scripts/run_worker.py --once
```

说明：MVP 默认单 worker 串行执行；多 worker 并发执行通过 `MAX_CONCURRENT_RUNS` 控制。

## CI 说明

仓库当前包含两个工作流：

- `OpenCode PR Review`：在 PR 上执行只读 AI 审查，输出中文 review 建议
- `Pytest`：安装依赖并运行 `pytest -q`，用于基础回归检查

这两个工作流都服务于"快速反馈 + 轻量治理"的目标。

## 目录结构（简化）

```text
.
|-- app/
|   |-- main.py              # FastAPI 入口
|   |-- config.py            # 环境变量配置
|   |-- db.py                # SQLite 连接与初始化
|   |-- models.py            # 数据表定义
|   |-- routes/
|   |   |-- hooks.py         # /hook-events
|   |   |-- github.py        # /github/webhook
|   |   `-- web.py           # 首页与 run 详情页
|   |-- services/
|   |   |-- hooks.py         # Hook 事件处理
|   |   |-- github_events.py # GitHub 事件解析
|   |   |-- normalizer.py    # Review 归一化
|   |   |-- queue.py         # 任务队列
|   |   |-- agent_runner.py  # Agent 执行器
|   |   |-- git_ops.py       # Git 操作
|   |   |-- filter.py        # 过滤器 (M6)
|   |   |-- policy.py        # 策略控制 (M6)
|   |   |-- concurrency.py   # 并发控制 (M6)
|   |   |-- retry.py         # 重试机制 (M6)
|   |   `-- logging_config.py# 日志管理 (M6)
|   |-- templates/
|   `-- static/
|-- scripts/
|   |-- init_db.py
|   `-- run_worker.py
|-- tests/
|-- docs/
|   |-- architecture.md      # 系统架构文档
|   |-- troubleshooting.md   # 故障排除指南
|   `-- hook-samples.md      # Hook 示例
|-- .github/workflows/
|   |-- opencode-review.yml
|   `-- pytest.yml
|-- example.env
`-- claude_agent_sdk_pr_autofix_plan.md
```

## 文档

- [系统架构文档](docs/architecture.md)：架构概览、核心组件、数据模型、状态机设计
- [故障排除指南](docs/troubleshooting.md)：常见问题、日志查看、诊断命令、错误码说明
- [Hook 示例](docs/hook-samples.md)：Hook 配置示例与调试建议
- [设计文档](claude_agent_sdk_pr_autofix_plan.md)：完整的项目设计说明书

## 后续路线

- 短期（M7）：完善文档、E2E 测试、压力测试
- 中期（规划中）：多仓库支持、手动暂停/恢复、更丰富的策略控制
- 长期（规划中）：PostgreSQL 支持、多 worker 集群、可观测性增强

## 常见问题

- 数据库初始化失败：先确认 `DB_PATH` 的目录存在且有写权限，再重试 `python scripts/init_db.py`
- `8000` 端口被占用：改用其他端口启动（例如 `--port 8001`）
- Webhook 调试无响应：检查 `content-type`、`x-github-event` 和 JSON 格式是否正确
- Worker 不执行任务：检查队列状态、并发限制、PR 锁；详见 [故障排除指南](docs/troubleshooting.md)

## Hook Samples

- Hook 配置示例：`example_hooks.json`
- Hook 事件说明与调试建议：`docs/hook-samples.md`
