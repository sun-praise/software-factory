# software-factory

一个基于 FastAPI 的轻量自动化编排项目，目标是把 PR 审查反馈闭环做成可追踪、可扩展、可本地运行的最小系统。

核心思路是：触发由 Hook 和 GitHub Webhook 决定，执行由 Agent Worker 负责，页面只做必要状态展示。

## 项目简介与目标

当前开发流程里，review 之后常见重复动作是“看评论 -> 整理问题 -> 再次让 AI 修复”。

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
- Normalizer：把 review/comment 统一成结构化修复任务（开发中）
- Agent Worker：按策略执行修复任务并回写 PR（开发中）
- Thin Web：展示最近任务、状态、错误摘要（M1 已有基础页面）

## 当前进度

Milestone 概览：

- M1（已完成）：最小可跑骨架
  - FastAPI 服务、健康检查、SSR 页面
  - `/hook-events`、`/github/webhook` 占位接口
  - SQLite 初始化脚本和核心表结构
  - 基础 CI（OpenCode Review + Pytest）
- M2（进行中）：事件入库、幂等和会话关联
  - Hook/GitHub 事件的结构化解析与落库
  - 去重键、错误处理与状态推进
  - Normalizer/Worker 链路打通（部分能力处于占位或规划中）

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

- `APP_ENV`（`development`，可选）：运行环境标识
- `HOST`（`127.0.0.1`，可选）：服务监听地址
- `PORT`（`8000`，可选）：服务监听端口
- `DB_PATH`（`./data/software_factory.db`，可选）：SQLite 文件路径
- `GITHUB_WEBHOOK_SECRET`（空字符串，可选）：本地联调可留空；生产环境建议配置并启用签名校验

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

说明：当前 `/github/webhook` 仅做事件接收与回显，签名校验计划在后续 Milestone 实现；生产环境请务必配置 `GITHUB_WEBHOOK_SECRET` 并在入口层启用校验。

语法/字节码编译检查：

```bash
python -m compileall app scripts
```

说明：`compileall` 仅用于语法与导入层面的快速检查，不替代静态分析。建议按需增加 `ruff`、`mypy` 等工具。

Worker 调试（M5）：

```bash
python scripts/run_worker.py --once
```

说明：MVP 默认单 worker 串行执行；多 worker 并发执行不在当前保证范围。

## CI 说明

仓库当前包含两个工作流：

- `OpenCode PR Review`：在 PR 上执行只读 AI 审查，输出中文 review 建议
- `Pytest`：安装依赖并运行 `pytest -q`，用于基础回归检查

这两个工作流都服务于“快速反馈 + 轻量治理”的目标。

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
|   |-- templates/
|   `-- static/
|-- scripts/
|   `-- init_db.py
|-- .github/workflows/
|   |-- opencode-review.yml
|   `-- pytest.yml
|-- example.env
`-- claude_agent_sdk_pr_autofix_plan.md
```

## 后续路线

- 短期（M2）：完善事件模型、幂等、状态机与错误处理
- 中期（M3，规划中）：接入可运行的 Normalizer + Agent Worker 执行链路
- 长期（规划中）：更稳健的策略控制、可选重试和多 worker 扩展

## 常见问题

- 数据库初始化失败：先确认 `DB_PATH` 的目录存在且有写权限，再重试 `python scripts/init_db.py`
- `8000` 端口被占用：改用其他端口启动（例如 `--port 8001`）
- Webhook 调试无响应：检查 `content-type`、`x-github-event` 和 JSON 格式是否正确

## Hook Samples

- Hook 配置示例：`example_hooks.json`
- Hook 事件说明与调试建议：`docs/hook-samples.md`
