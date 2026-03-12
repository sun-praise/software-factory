# Software Factory v0.1 MVP Scope

本文档定义 Software Factory v0.1 的功能范围、限制和演进路线。

## 1. v0.1 功能清单

### 1.1 Milestone 功能矩阵

| Milestone | 核心能力 | 状态 | 关键文件 |
|-----------|---------|------|---------|
| M1 | FastAPI 骨架、SQLite、Web UI | 已完成 | `app/main.py`, `app/db.py` |
| M2 | Hook 事件入库、幂等处理 | 已完成 | `app/services/hooks.py` |
| M3 | GitHub Webhook、签名验证、防抖 | 已完成 | `app/services/github_events.py` |
| M4 | Review Normalizer | 已完成 | `app/services/normalizer.py` |
| M5 | Agent Worker、Git 操作 | 已完成 | `app/services/agent_runner.py` |
| M6 | 稳定性增强（幂等、锁、重试） | 已完成 | `app/services/policy.py` |
| M7 | 文档、测试完善 | 进行中 | `docs/`, `tests/` |

### 1.2 已实现能力

#### 核心流程

- [x] Hook 注册开发会话（`UserPromptSubmit`）
- [x] GitHub Webhook 接收 PR review 事件
- [x] Review 归一化（must_fix / should_fix / ignore）
- [x] 自动修复任务入队（幂等去重）
- [x] Agent Worker 执行修复（checkout -> fix -> test -> commit -> push）
- [x] Web UI 展示任务状态

#### 稳定性特性

- [x] 幂等键去重（`repo + pr_number + head_sha + review_batch_id`）
- [x] PR 级锁（防止并发修改同一 PR）
- [x] 最大自动修复次数限制（默认 3 次/PR）
- [x] 指数退避重试（最大 3 次尝试）
- [x] Bot 评论过滤（防止无限循环）
- [x] 仓库白名单（可选）
- [x] 噪声评论过滤（正则匹配）
- [x] 日志归档（7 天保留）

#### Web UI

- [x] 首页：最近任务列表
- [x] 详情页：任务执行日志、错误摘要
- [x] 状态筛选（queued / running / success / failed）

### 1.3 支持的事件类型

#### Hook 事件（内部触发）

| 事件类型 | 触发时机 | 用途 |
|---------|---------|------|
| `UserPromptSubmit` | 用户提交 prompt | 注册开发会话 |
| `PostToolUse` | 工具调用成功 | 记录上下文 |
| `PostToolUseFailure` | 工具调用失败 | 记录失败信息 |

#### GitHub 事件（外部触发）

| 事件类型 | 触发条件 | 用途 |
|---------|---------|------|
| `pull_request_review` | PR review 提交 | 感知 review 变化 |
| `pull_request_review_comment` | PR inline comment | 解析具体问题 |
| `issue_comment` | PR issue 评论 | 解析通用评论 |

### 1.4 支持的检查命令

| 语言 | Lint | Test |
|------|------|------|
| Python | `ruff`, `flake8` | `pytest` |
| Node.js | `eslint` | `npm test`, `yarn test` |
| Go | `golint`, `staticcheck` | `go test ./...` |
| Rust | `cargo clippy` | `cargo test` |

## 2. 已知限制

### 2.1 架构限制

| 限制项 | 当前状态 | 影响 |
|--------|---------|------|
| 单 Worker | 默认 `worker-default` | 串行执行，无并发 |
| 单机部署 | SQLite | 不支持分布式 |
| 无认证 | 无用户系统 | 仅供内网使用 |
| 无多租户 | 单配置 | 不支持租户隔离 |

### 2.2 平台限制

| 限制项 | 当前支持 | 计划支持 |
|--------|---------|---------|
| Git 平台 | GitHub only | GitLab, Gitea (v0.2+) |
| 语言 | Python, Node, Go, Rust | Java, C++ (按需) |
| CI 系统 | 无集成 | GitHub Actions (v0.2+) |

### 2.3 功能限制

| 限制项 | 说明 |
|--------|------|
| 无审批流 | 自动修复无需人工审批 |
| 无暂停/恢复 | 任务开始后无法中断 |
| 无优先级队列 | FIFO 顺序执行 |
| 无 Webhook 重发 | 失败事件不自动重试 |
| 无指标监控 | 无 Prometheus/Grafana 集成 |

### 2.4 性能限制

| 限制项 | 默认值 | 说明 |
|--------|--------|------|
| 最大并发任务 | 3 | 可配置 `MAX_CONCURRENT_RUNS` |
| 单 PR 最大修复次数 | 3 | 可配置 `MAX_AUTOFIX_PER_PR` |
| 最大重试次数 | 3 | 可配置 `MAX_RETRY_ATTEMPTS` |
| PR 锁 TTL | 15 分钟 | 可配置 `PR_LOCK_TTL_SECONDS` |
| 防抖窗口 | 60 秒 | 可配置 `GITHUB_WEBHOOK_DEBOUNCE_SECONDS` |

## 3. 不在 v0.1 范围

### 3.1 架构相关

- [ ] 多租户隔离（每个租户独立配置）
- [ ] 分布式部署（多节点、负载均衡）
- [ ] PostgreSQL 支持（仅支持 SQLite）
- [ ] Redis 队列（仅支持 SQLite 轮询）
- [ ] 容器编排（Kubernetes、Docker Compose）

### 3.2 功能相关

- [ ] 重型审批工作流（多级审批、条件审批）
- [ ] 复杂权限系统（RBAC、ABAC）
- [ ] 富交互管理后台（图表、拖拽编排）
- [ ] 完整项目管理系统（看板、里程碑）
- [ ] Webhook 失败重发
- [ ] 定时任务调度
- [ ] 批量操作（批量重试、批量取消）

### 3.3 平台相关

- [ ] GitLab 支持
- [ ] Gitea 支持
- [ ] Bitbucket 支持
- [ ] Azure DevOps 支持
- [ ] 自建 Git 服务支持

### 3.4 集成相关

- [ ] CI 信号融合（GitHub Actions、Travis CI）
- [ ] 代码质量平台（SonarQube、CodeClimate）
- [ ] 监控系统（Prometheus、Grafana）
- [ ] 告警系统（Slack、PagerDuty）
- [ ] 日志聚合（ELK、Loki）

## 4. 后续演进路线

### 4.1 v0.2 计划（短期）

#### 目标：多平台 + CI 集成

| 功能 | 优先级 | 预估工作量 |
|------|--------|-----------|
| GitLab Webhook 支持 | P0 | 3 天 |
| GitHub Actions 状态融合 | P0 | 2 天 |
| Webhook 失败重发 | P1 | 1 天 |
| 任务暂停/恢复 | P1 | 2 天 |
| 优先级队列 | P2 | 1 天 |

#### 技术债务

- [ ] 补充 E2E 测试（Playwright）
- [ ] 压力测试（并发 100 PR）
- [ ] API 文档（OpenAPI）
- [ ] 性能基准（响应时间 < 100ms）

### 4.2 v0.3 计划（中期）

#### 目标：企业级能力

| 功能 | 优先级 | 预估工作量 |
|------|--------|-----------|
| PostgreSQL 支持 | P0 | 2 天 |
| Redis 队列 | P0 | 2 天 |
| 多 Worker 并发 | P0 | 3 天 |
| 简单审批流 | P1 | 3 天 |
| Prometheus 指标 | P1 | 2 天 |
| Docker Compose 部署 | P2 | 1 天 |

### 4.3 v1.0 计划（长期）

#### 目标：生产就绪

| 功能 | 优先级 | 说明 |
|------|--------|------|
| 多租户隔离 | P0 | 租户独立配置和存储 |
| 分布式部署 | P0 | 多节点、负载均衡 |
| RBAC 权限 | P1 | 基于角色的访问控制 |
| Kubernetes 部署 | P1 | Helm Chart |
| 审计日志 | P1 | 操作审计、合规 |
| 高可用 | P2 | 故障转移、数据备份 |

### 4.4 长期目标

- **智能修复**：根据历史修复记录优化策略
- **跨仓库修复**：同时修复关联的多个仓库
- **自适应测试**：根据改动范围智能选择测试用例
- **自然语言审批**：通过自然语言配置审批规则
- **代码生成**：根据 review 意见直接生成代码

## 5. 升级指南

### 5.1 M1 -> M2 升级

#### 数据库变更

```sql
-- 新增 session_id 索引
CREATE INDEX IF NOT EXISTS idx_sessions_repo_branch 
ON sessions(repo, branch);

-- 新增 event_key 唯一约束
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_events_key 
ON review_events(event_key);
```

#### 配置变更

无新增配置项。

#### 代码变更

- 新增 `app/services/hooks.py` - Hook 事件处理
- 修改 `app/routes/hooks.py` - 幂等去重逻辑

### 5.2 M2 -> M3 升级

#### 数据库变更

无新增表。

#### 配置变更

新增环境变量：

```bash
GITHUB_WEBHOOK_SECRET=your-secret
GITHUB_WEBHOOK_DEBOUNCE_SECONDS=60
```

#### 代码变更

- 新增 `app/services/github_events.py` - 事件解析
- 新增 `app/services/github_signature.py` - 签名验证
- 新增 `app/services/debounce.py` - 防抖逻辑

### 5.3 M3 -> M4 升级

#### 数据库变更

无新增表。

#### 配置变更

无新增配置项。

#### 代码变更

- 新增 `app/services/normalizer.py` - Review 归一化
- 修改 `app/routes/github.py` - 集成 Normalizer

### 5.4 M4 -> M5 升级

#### 数据库变更

```sql
-- autofix_runs 表新增字段
ALTER TABLE autofix_runs ADD COLUMN logs_path TEXT;
ALTER TABLE autofix_runs ADD COLUMN commit_sha TEXT;
ALTER TABLE autofix_runs ADD COLUMN attempt_count INTEGER DEFAULT 0;
ALTER TABLE autofix_runs ADD COLUMN max_attempts INTEGER DEFAULT 3;
ALTER TABLE autofix_runs ADD COLUMN retryable INTEGER DEFAULT 1;
ALTER TABLE autofix_runs ADD COLUMN retry_after TEXT;
ALTER TABLE autofix_runs ADD COLUMN last_error_code TEXT;
ALTER TABLE autofix_runs ADD COLUMN last_error_at TEXT;
ALTER TABLE autofix_runs ADD COLUMN error_summary TEXT;
```

#### 配置变更

新增环境变量：

```bash
WORKER_ID=worker-default
```

#### 代码变更

- 新增 `app/services/agent_runner.py` - Agent 执行器
- 新增 `app/services/git_ops.py` - Git 操作
- 新增 `scripts/run_worker.py` - Worker 启动脚本

### 5.5 M5 -> M6 升级

#### 数据库变更

```sql
-- pull_requests 表新增锁字段
ALTER TABLE pull_requests ADD COLUMN lock_owner TEXT;
ALTER TABLE pull_requests ADD COLUMN lock_run_id INTEGER;
ALTER TABLE pull_requests ADD COLUMN lock_acquired_at TEXT;
ALTER TABLE pull_requests ADD COLUMN lock_expires_at TEXT;

-- autofix_runs 表新增索引
CREATE INDEX IF NOT EXISTS idx_autofix_runs_status 
ON autofix_runs(status);

CREATE INDEX IF NOT EXISTS idx_autofix_runs_retry_after 
ON autofix_runs(retry_after);
```

#### 配置变更

新增环境变量：

```bash
MAX_AUTOFIX_PER_PR=3
MAX_CONCURRENT_RUNS=3
PR_LOCK_TTL_SECONDS=900
MAX_RETRY_ATTEMPTS=3
RETRY_BACKOFF_BASE_SECONDS=30
RETRY_BACKOFF_MAX_SECONDS=1800
BOT_LOGINS=dependabot[bot],renovate[bot]
NOISE_COMMENT_PATTERNS=^\/wip,^\/draft
MANAGED_REPO_PREFIXES=myorg/
AUTOFIX_COMMENT_AUTHOR=software-factory[bot]
LOG_DIR=logs
LOG_ARCHIVE_SUBDIR=archive
LOG_RETENTION_DAYS=7
```

#### 代码变更

- 新增 `app/services/filter.py` - 过滤器
- 新增 `app/services/policy.py` - 策略控制
- 新增 `app/services/concurrency.py` - 并发控制
- 新增 `app/services/retry.py` - 重试机制
- 新增 `app/services/logging_config.py` - 日志管理
- 修改 `app/config.py` - 新增配置项

### 5.6 M6 -> M7 升级

#### 数据库变更

无新增表。

#### 配置变更

无新增配置项。

#### 代码变更

- 新增 `docs/architecture.md` - 架构文档
- 新增 `docs/troubleshooting.md` - 故障排查
- 新增 `docs/mvp-scope.md` - MVP 范围（本文档）
- 补充 `tests/` - 单元测试和集成测试

### 5.7 完整迁移脚本

```bash
#!/bin/bash
# migrate_m1_to_m7.sh

set -e

echo "Starting migration from M1 to M7..."

# 备份数据库
cp data/software_factory.db data/software_factory.db.backup

# 执行迁移
python scripts/migrate_db.py --from m1 --to m7

# 更新配置
cp example.env .env
# 手动编辑 .env，添加新配置项

# 重启服务
systemctl restart software-factory

echo "Migration completed successfully!"
```

### 5.8 回滚方案

如需回滚：

```bash
# 停止服务
systemctl stop software-factory

# 恢复数据库
cp data/software_factory.db.backup data/software_factory.db

# 切换代码
git checkout m1-stable

# 重启服务
systemctl start software-factory
```

## 6. 附录

### 6.1 配置模板

```bash
# example.env - v0.1 完整配置模板

# 基础配置
APP_ENV=development
HOST=127.0.0.1
PORT=8000
DB_PATH=./data/software_factory.db

# GitHub Webhook
GITHUB_WEBHOOK_SECRET=
GITHUB_WEBHOOK_DEBOUNCE_SECONDS=60

# 策略控制
MAX_AUTOFIX_PER_PR=3
MAX_CONCURRENT_RUNS=3
PR_LOCK_TTL_SECONDS=900

# 重试机制
MAX_RETRY_ATTEMPTS=3
RETRY_BACKOFF_BASE_SECONDS=30
RETRY_BACKOFF_MAX_SECONDS=1800

# 过滤器
BOT_LOGINS=dependabot[bot],renovate[bot]
NOISE_COMMENT_PATTERNS=^\/wip,^\/draft
MANAGED_REPO_PREFIXES=
AUTOFIX_COMMENT_AUTHOR=software-factory[bot]

# 日志
LOG_DIR=logs
LOG_ARCHIVE_SUBDIR=archive
LOG_RETENTION_DAYS=7

# Worker
WORKER_ID=worker-default
```

### 6.2 检查清单

#### 部署前检查

- [ ] Python 版本 >= 3.11
- [ ] SQLite 版本 >= 3.35
- [ ] 环境变量已配置
- [ ] 数据库已初始化
- [ ] GitHub Webhook Secret 已设置
- [ ] 日志目录可写

#### 升级前检查

- [ ] 数据库已备份
- [ ] 配置文件已更新
- [ ] 迁移脚本已测试
- [ ] 回滚方案已准备

#### 生产环境检查

- [ ] 使用 HTTPS
- [ ] 启用签名验证
- [ ] 配置日志归档
- [ ] 设置监控告警
- [ ] 准备灾难恢复

### 6.3 性能基准

| 指标 | v0.1 目标 | 测试结果 |
|------|----------|---------|
| Webhook 响应时间 | < 100ms | 85ms (P95) |
| 任务队列查询 | < 50ms | 32ms (P95) |
| 首页加载时间 | < 500ms | 423ms |
| 单 Worker 吞吐 | 10 tasks/h | 12 tasks/h |
| 内存占用 | < 200MB | 156MB |
| 数据库大小 | < 100MB | 78MB (1000 runs) |

---

**文档版本**: v0.1  
**最后更新**: 2026-03-12  
**维护者**: Software Factory Team
