# 系统架构文档

本文档描述 software-factory 的系统架构、核心组件和数据模型。

## 架构概览

```text
+-------------------+     +---------------------+
| Claude Code Hook  |     |   GitHub Webhook    |
| (UserPromptSubmit)|     | (PR Review Events)  |
+--------+----------+     +----------+----------+
         |                           |
         v                           v
+--------+----------------------------+--------+
|              FastAPI Orchestrator            |
|  /hook-events              /github/webhook   |
+--------+----------------------------+--------+
         |                           |
         v                           v
+--------+----------+     +----------+----------+
|   State Store     |<--->|   Review Normalizer |
|    (SQLite)       |     |   (归一化评论)      |
+--------+----------+     +---------------------+
         |
         v
+--------+----------+
|   Task Queue      |
|  (autofix_runs)   |
+--------+----------+
         |
         v
+--------+----------+
|   Agent Worker    |
|  (run_worker.py)  |
+--------+----------+
         |
         v
+--------+----------+
|    Git Ops        |
| (checkout/commit) |
+--------+----------+
         |
         v
+--------+----------+
|   Thin Web UI     |
|   (Jinja2 SSR)    |
+-------------------+
```

## 核心组件

### 1. Hook 事件处理器

**职责**: 接收 Claude Code 生命周期事件，注册开发会话。

**入口**: `POST /hook-events`

**支持的事件类型**:
- `UserPromptSubmit`: 注册新开发会话
- `PostToolUse`: 记录工具调用成功事件
- `PostToolUseFailure`: 记录工具调用失败事件

**核心模块**: `app/services/hooks.py`

### 2. GitHub Webhook 适配器

**职责**: 接收并解析 GitHub PR review 事件，进行去重和归档。

**入口**: `POST /github/webhook`

**支持的事件类型**:
- `pull_request_review`: PR 审查事件
- `pull_request_review_comment`: PR 审查评论
- `issue_comment`: PR issue 评论

**核心模块**: 
- `app/services/github_events.py`: 事件解析
- `app/services/github_signature.py`: 签名验证
- `app/services/debounce.py`: 防抖聚合

### 3. Review Normalizer

**职责**: 将不同来源的 review/comment 归一化为结构化修复任务。

**输入**: 原始 GitHub 事件 payload

**输出**: 归一化的 review JSON

```json
{
  "repo": "owner/repo",
  "pr_number": 123,
  "head_sha": "abc123",
  "must_fix": [
    {
      "source": "pull_request_review_comment",
      "path": "src/auth.ts",
      "line": 88,
      "severity": "P0",
      "text": "Missing null handling"
    }
  ],
  "should_fix": [],
  "ignore": [],
  "summary": "1 blocking issues, 0 suggestions, 0 ignored"
}
```

**核心模块**: `app/services/normalizer.py`

### 4. 任务队列

**职责**: 管理自动修复任务的生命周期。

**状态流转**:
```text
queued -> running -> success
                 -> failed
                 -> retry_scheduled -> queued
```

**核心功能**:
- 幂等键去重 (`idempotency_key`)
- 任务领取 (`claim_next_queued_run`)
- 状态更新 (`mark_run_finished`)
- 重试调度 (`schedule_retry`)

**核心模块**: `app/services/queue.py`

### 5. Agent Worker

**职责**: 执行自动修复任务。

**执行流程**:
1. 获取 PR 锁 (`acquire_pr_lock`)
2. checkout PR 分支
3. 执行检查命令 (lint/test)
4. commit 并 push
5. 发布 PR 评论
6. 释放 PR 锁 (`release_pr_lock`)

**核心模块**: `app/services/agent_runner.py`

### 6. Web UI

**职责**: 展示任务状态和执行历史。

**页面**:
- `/`: 首页，展示最近任务列表
- `/runs`: 任务列表页
- `/runs/{run_id}`: 任务详情页

**核心模块**: `app/routes/web.py`

## 数据模型

### sessions (开发会话)

记录由 Hook 注册的开发会话。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键 |
| repo | TEXT | 仓库名 (owner/repo) |
| branch | TEXT | 分支名 |
| cwd | TEXT | 工作目录 |
| source | TEXT | 来源 (默认 `claude_code`) |
| started_at | TEXT | 开始时间 |
| ended_at | TEXT | 结束时间 |
| metadata_json | TEXT | 扩展元数据 (JSON) |

### pull_requests (PR 记录)

记录纳管的 PR 及其状态。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键 |
| repo | TEXT | 仓库名 |
| pr_number | INTEGER | PR 编号 |
| head_sha | TEXT | HEAD commit SHA |
| branch | TEXT | 分支名 |
| state | TEXT | PR 状态 |
| linked_session_id | INTEGER | 关联会话 ID |
| autofix_count | INTEGER | 自动修复次数 |
| lock_owner | TEXT | 锁持有者 |
| lock_run_id | INTEGER | 锁关联的 run ID |
| lock_acquired_at | TEXT | 锁获取时间 |
| lock_expires_at | TEXT | 锁过期时间 |

### review_events (审查事件)

记录原始 GitHub review 事件。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键 |
| repo | TEXT | 仓库名 |
| pr_number | INTEGER | PR 编号 |
| event_type | TEXT | 事件类型 |
| event_key | TEXT | 去重键 (唯一) |
| actor | TEXT | 事件发起者 |
| head_sha | TEXT | HEAD SHA |
| raw_payload_json | TEXT | 原始 payload (JSON) |
| received_at | TEXT | 接收时间 |

### autofix_runs (自动修复任务)

记录自动修复任务的执行状态。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键 |
| repo | TEXT | 仓库名 |
| pr_number | INTEGER | PR 编号 |
| head_sha | TEXT | HEAD SHA |
| status | TEXT | 状态 (queued/running/success/failed/retry_scheduled) |
| trigger_source | TEXT | 触发来源 |
| idempotency_key | TEXT | 幂等键 (唯一) |
| normalized_review_json | TEXT | 归一化 review (JSON) |
| worker_id | TEXT | 执行 worker ID |
| claimed_at | TEXT | 领取时间 |
| started_at | TEXT | 开始时间 |
| logs_path | TEXT | 日志文件路径 |
| commit_sha | TEXT | 提交 SHA |
| attempt_count | INTEGER | 尝试次数 |
| max_attempts | INTEGER | 最大尝试次数 |
| retryable | INTEGER | 是否可重试 |
| retry_after | TEXT | 重试时间 |
| last_error_code | TEXT | 最后错误码 |
| last_error_at | TEXT | 最后错误时间 |
| error_summary | TEXT | 错误摘要 |
| finished_at | TEXT | 完成时间 |

## 状态机设计

### PR 状态流转

```text
                    +------+
                    | IDLE |
                    +--+---+
                       |
          +------------+------------+
          |                         |
          v                         v
+---------+-------+        +--------+--------+
| SESSION_REGISTERED|      |   PR_OPENED     |
+---------+-------+        +--------+--------+
          |                         |
          +------------+------------+
                       |
                       v
              +--------+--------+
              | REVIEW_PENDING  |
              +--------+--------+
                       |
                       v
              +--------+--------+
              | REVIEW_RECEIVED |
              +--------+--------+
                       |
          +------------+------------+
          |                         |
          v                         v
+---------+-------+        +--------+--------+
| AUTO_FIX_QUEUED |        |     HALTED      |
+---------+-------+        +--------+--------+
          |
          v
+---------+-------+
| AUTO_FIX_RUNNING|
+---------+-------+
          |
    +-----+-----+
    |           |
    v           v
+---+---+   +---+----+
|SUCCESS|   | FAILED |
+---+---+   +---+----+
    |           |
    v           v
+---+------------+----+
| WAITING_NEXT_REVIEW |
+---+------------+----+
    |
    v
+---+---+
| DONE  |
+-------+
```

### Run 状态流转

```text
+--------+     +--------+     +--------+
| queued | --> |running | --> |success |
+--------+     +---+----+     +--------+
                   |
                   v
               +---+----+
               | failed |
               +---+----+
                   |
                   v
           +-------+--------+
           |retry_scheduled |
           +-------+--------+
                   |
                   v
               +--------+
               | queued | (循环)
               +--------+
```

## 触发模型

### 内部触发 (Hook)

用于确定性捕获开发会话的开始和结束。

| Hook 事件 | 触发时机 | 动作 |
|----------|---------|------|
| `UserPromptSubmit` | 用户提交 prompt | 注册开发会话 |
| `PostToolUse` | 工具调用成功 | 记录上下文，更新 PR 关联 |
| `PostToolUseFailure` | 工具调用失败 | 记录失败信息 |

### 外部触发 (GitHub Webhook)

用于感知 PR review 变化。

| GitHub 事件 | 触发条件 | 动作 |
|------------|---------|------|
| `pull_request_review` | PR review 提交 | 解析并归一化 review |
| `pull_request_review_comment` | PR review 评论 | 解析 inline comment |
| `issue_comment` | PR issue 评论 | 解析通用评论 |

## M1-M7 功能清单

| Milestone | 核心能力 | 状态 |
|-----------|---------|------|
| M1 | FastAPI 骨架、SQLite、Web UI 基础 | 已完成 |
| M2 | Hook 事件入库、幂等处理、会话关联 | 已完成 |
| M3 | GitHub Webhook 接收、签名验证、防抖 | 已完成 |
| M4 | Review Normalizer、归一化输出 | 已完成 |
| M5 | Agent Worker、Git 操作、检查命令执行 | 已完成 |
| M6 | 稳定性增强：幂等键、PR 锁、重试、限流、日志归档 | 已完成 |
| M7 | 文档完善、E2E 测试、压力测试 | 进行中 |

### M6 稳定性模块详解

| 模块 | 文件 | 功能 |
|------|------|------|
| 过滤器 | `filter.py` | Bot/噪声过滤、仓库白名单 |
| 策略控制 | `policy.py` | PR 自动修复次数限制 |
| 并发控制 | `concurrency.py` | PR 锁、运行数限制 |
| 重试机制 | `retry.py` | 指数退避重试调度 |
| 日志管理 | `logging_config.py` | 日志归档、保留策略 |

## 部署要求

### 运行环境

- Python 3.11+
- SQLite 3.35+ (支持 `RETURNING` 子句，低版本自动降级)

### 依赖

```
fastapi>=0.115.0
uvicorn>=0.30.0
pydantic>=2.0.0
pydantic-settings>=2.0.0
jinja2>=3.0.0
```

### 配置项

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `APP_ENV` | `development` | 运行环境 |
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `DB_PATH` | `./data/software_factory.db` | 数据库路径 |
| `GITHUB_WEBHOOK_SECRET` | (空) | Webhook 签名密钥 |
| `GITHUB_WEBHOOK_DEBOUNCE_SECONDS` | `60` | 防抖窗口 (秒) |
| `MAX_AUTOFIX_PER_PR` | `3` | 单 PR 最大修复次数 |
| `MAX_CONCURRENT_RUNS` | `3` | 最大并发任务数 |
| `PR_LOCK_TTL_SECONDS` | `900` | PR 锁 TTL (秒) |
| `MAX_RETRY_ATTEMPTS` | `3` | 最大重试次数 |
| `RETRY_BACKOFF_BASE_SECONDS` | `30` | 重试基础延迟 (秒) |
| `RETRY_BACKOFF_MAX_SECONDS` | `1800` | 重试最大延迟 (秒) |
| `BOT_LOGINS` | (空) | Bot 账号列表 (逗号分隔) |
| `NOISE_COMMENT_PATTERNS` | (空) | 噪声评论正则 (逗号分隔) |
| `MANAGED_REPO_PREFIXES` | (空) | 纳管仓库前缀 (逗号分隔) |
| `AUTOFIX_COMMENT_AUTHOR` | `software-factory[bot]` | 自动修复评论作者 |
| `LOG_DIR` | `logs` | 日志目录 |
| `LOG_ARCHIVE_SUBDIR` | `archive` | 日志归档子目录 |
| `LOG_RETENTION_DAYS` | `7` | 日志保留天数 |
| `WORKER_ID` | `worker-default` | Worker 标识 |

### 目录结构

```text
.
|-- app/
|   |-- main.py              # FastAPI 入口
|   |-- config.py            # 配置管理
|   |-- db.py                # 数据库连接
|   |-- models.py            # 数据模型
|   |-- routes/
|   |   |-- hooks.py         # /hook-events
|   |   |-- github.py        # /github/webhook
|   |   `-- web.py           # Web 页面
|   |-- services/
|   |   |-- hooks.py         # Hook 事件处理
|   |   |-- github_events.py # GitHub 事件解析
|   |   |-- normalizer.py    # Review 归一化
|   |   |-- queue.py         # 任务队列
|   |   |-- agent_runner.py  # Agent 执行器
|   |   |-- git_ops.py       # Git 操作
|   |   |-- filter.py        # 过滤器
|   |   |-- policy.py        # 策略控制
|   |   |-- concurrency.py   # 并发控制
|   |   |-- retry.py         # 重试机制
|   |   `-- logging_config.py# 日志管理
|   |-- templates/           # Jinja2 模板
|   `-- static/              # 静态资源
|-- scripts/
|   |-- init_db.py           # 数据库初始化
|   `-- run_worker.py        # Worker 启动脚本
|-- tests/                   # 测试
|-- docs/                    # 文档
|-- .github/workflows/       # CI 配置
|-- example.env              # 环境变量示例
`-- requirements.txt         # 依赖列表
```
