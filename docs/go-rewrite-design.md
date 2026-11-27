# Go 重写设计（rewrite/go 分支）

状态：M0 脚手架已落地，按里程碑推进。

## 目标与决策记录

- 目标：用 Go 全功能重写 Python 版 software-factory（FastAPI + SQLite + omp worker），完成后替换 Python 版。
- **放弃 gascity**（2026-11-27 决策）：其编排核心全部位于 `internal/`（Go 规定跨模块禁止导入），公开面只有 `pkg/eventexport`；"作为库内嵌"只能 fork 加 SDK 层并长期维护，且依赖树重（Dolt/K8s、Go 1.27.4+）。仅借鉴其 formula/convergence 思想。
- **基座框架：trpc-agent-go**（`trpc.group/trpc-go/trpc-agent-go`，go 1.21+）：负责 normalizer 等 LLM 逻辑、graph/workflow 编排、session；agent 执行仍走 omp（oh-my-pi）CLI 子进程，与 Python 版 `agent_runner` 模型一致。
- 分支策略：`rewrite/go` 长期分支 + worktree 开发，走 PR 合入 main；Python 版在切换完成前保持可用。

## 模块映射（Python → Go）

| Python（app/） | Go | 说明 |
|---|---|---|
| main.py, config.py, db.py, models.py | cmd/web, cmd/worker, internal/config, internal/db | 入口、配置、SQLite（modernc.org/sqlite 纯 Go） |
| routes/hooks.py + services/hooks.py | internal/hooks | Claude Code 生命周期事件（UserPromptSubmit / PostToolUse / PostToolUseFailure） |
| routes/github.py + github_events + github_signature + debounce + task_source | internal/events | webhook 接入、签名校验、去重（event_key 唯一索引）、防抖聚合、手动任务 |
| services/normalizer.py + ai_client.py | internal/normalizer | trpc-agent-go 结构化输出 must_fix/should_fix/ignore + summary |
| queue.py + retry.py + concurrency.py | internal/queue | 队列状态机 queued→running→success/failed/retry_scheduled，attempt_count/max_attempts |
| agent_runner.py + omp_client.py + agent_prompt.py + agent_modes.py + patch_applier.py | internal/agent | omp CLI 执行、prompt 构建、patch 应用（最大移植面，agent_runner 约 122 个函数） |
| git_ops.py | internal/gitops | checkout/commit/push/PR 创建 |
| providers/github.py + gitee.py + registry.py + types.py | internal/forge | forge 抽象 + GitHub/Gitee 双实现 |
| byok.py + user_api_keys 表 | internal/byok | BYOK 密钥管理 |
| policy.py + filter.py + run_hints.py | internal/policy | 策略/过滤/运行提示 |
| runtime_settings.py + feature_flags.py + logging_config.py | internal/settings | 运行时配置（DB 覆盖 env 的所有权规则见 docs/runtime-config.md） |
| routes/web.py + templates + static | internal/webui | SSR（html/template），运行列表/状态/错误 |
| scripts/run_worker.py | cmd/worker | worker 入口（启动脚本保持 token 隔离约束：web 不持有 AI token） |

## DB Schema

对齐 `app/models.py` 的 7 张表 + 索引（sessions / pull_requests / review_events / autofix_runs / app_feature_flags / app_config_audit_log / user_api_keys），已移植到 `internal/db/db.go`，默认路径 `data/software_factory.db`，`DB_PATH` 可覆盖。列名、默认值、索引与 Python 版逐字对齐，保证切换时可直接复用现有库文件。

## 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| M0 | 脚手架：go.mod、cmd/web + cmd/worker、internal/db 建表、Go CI workflow | `go build ./...` + `go test ./...` 绿；web `/healthz` 可访问 |
| M1 | 状态存储 + 入口层：hooks 事件、GitHub webhook（签名/去重/防抖）、手动任务 | 对齐 tests/ 中 webhook 与 hooks 用例 |
| M2 | normalizer：trpc-agent-go agent，anthropic/openai 双 provider，结构化输出 | 对齐 normalizer 单测场景 |
| M3 | agent worker：omp CLI 执行、patch 应用、gitops、校验命令、回写 PR | 端到端跑通一条 issue→fix→PR |
| M4 | forge 双 provider（GitHub/Gitee）+ BYOK | Gitee webhook/回写对齐 |
| M5 | settings/flags/policy/agent_modes 完整对齐 | runtime settings DB-vs-env 规则对齐 |
| M6 | Web UI SSR | 运行列表/详情/错误可视化 |
| M7 | docker/部署/stress test、切换方案、移除 Python 版 | 按测试用例实测（视觉/上线验证纪律） |

## 技术决策

- HTTP：标准库 `net/http`（Go 1.22+ 路由增强足够，不引框架）。
- SQLite 驱动：`modernc.org/sqlite`（纯 Go，无 CGO）；trpc-agent-go 自带的 mattn/go-sqlite3 仅其 session sqlite backend 使用，我们不用该 backend，验证 `CGO_ENABLED=0` 可构建。
- Go 工具链：go.mod 声明 go 1.24（本机 1.24.1 可用）；trpc-agent-go 要求 1.21+。
- 配置读取顺序与 Python 版一致：DB（runtime settings）优先于 env 优先于默认值，具体对齐 docs/runtime-config.md。

## 风险

- agent_runner.py 移植面最大（122 个函数），按调用链拆批次移植，每批配对齐单测。
- trpc-agent-go 版本迭代快（pre-1.0），go.mod 锁定版本，升级单独走 PR。
- 双版本共存期：web/worker 启动脚本需保持「web 不持有 AI token」的隔离约束（docs/local-runtime.md）。
