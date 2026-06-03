# robomp — 相关工作

> 状态: 调研记录
> 添加于: 2026-06-03
> 来源 issue: sun-praise/software-factory#222 (已关闭, 内容沉淀到此)

## TL;DR

`can1357/oh-my-pi` 仓库下的 `python/robomp/` 子模块, 是一个和我们方向高度重合的自托管 GitHub issue triage + auto-fix bot. 它驱动 `omp --mode rpc` 作为子进程, 围绕 GitHub webhook、SQLite 队列、per-issue git worktree 构建, 和 software-factory 在问题空间上几乎完全重合 (自动 triage、自动开 PR、agent worker 调度), 但实现栈和取舍不同.

## 基本信息

| 字段 | 值 |
|---|---|
| 仓库 | `can1357/oh-my-pi` |
| 子目录 | `python/robomp/` |
| 描述 | "self-hosted GitHub triage-and-fix bot driving `omp --mode rpc`" |
| 在 oh-my-pi 中首次出现 | 2026-05-16 (通过 `git subtree add` 合入) |
| 源码仓库 | 私有/已删除 (can1357/robomp 与 can1357/roboomp 均 404) |
| 维护者 | can1357 (oh-my-pi 作者, 2026-02 写过 ["The harness problem"](https://blog.can.ac/2026/02/12/the-harness-problem/)) |
| 父项目背景 | oh-my-pi 基于 Pi (Mario Zechner, 现迁至 [earendil-works/pi](https://github.com/earendil-works/pi)) 的代码独立创建 (非 GitHub fork), 强调 harness 优化; 2025-08-09 起开发 (与 Pi 首 commit 同日), 2025-12-31 公开, 当前 ~6,900 commits |

## 时间线对比

| 事件 | 时间 |
|---|---|
| `sun-praise/software-factory` 仓库创建 | 2026-03-12 |
| `sun-praise/software-factory` 首个 commit | 2026-03-12 |
| `robomp` 在 oh-my-pi 公开 (subtree 合入) | 2026-05-16 |
| 时间差 | **software-factory 公开开发比 robomp 早约 2 个月** |

注: robomp 的源码仓库私有/已删除, 实际开发起点可能更早, 从公开数据无法判断.

## 架构

数据流: `Webhook → durable queue → async dispatcher → per-issue git worktree → omp RPC subprocess + host tools`

| 层 | 实现 | 备注 |
|---|---|---|
| 入口 | `POST /webhook/github` | HMAC-SHA256 校验, bad signature 返回 401; 用 `X-GitHub-Delivery` 做去重 |
| 队列 | SQLite + WAL | `BEGIN IMMEDIATE` 抢锁, 进程内 `_inflight` 集合按 `(owner, repo, number)` 串行化; 默认 `ROBOMP_MAX_CONCURRENCY=8` |
| 隔离 | per-issue git worktree | 路径 `/data/workspaces/<owner>__<repo>__<n>/repo`; 分支命名 `farm/<8hex>/<slug>`; 后端是共享 `--filter=blob:none` clone pool; 凭据 URL 每次重新设置 |
| 执行 | `omp --mode rpc` 子进程 | 持久化 `session_dir`; 重启/续期用 `omp --continue`; 同一 issue 跨多轮 webhook 共享 session |
| 模型路由 | `ROBOMP_MODEL` (CSV) 随机挑 | 模式: `ROBOMP_THINKING`; provider: `ROBOMP_PROVIDER` |
| Agent 工具面 | omp 内置工具 + `host_tools.py` | omp 工具 (read/edit/write/bash/lsp) 限定到 worktree; host_tools 是唯一能 mutate GitHub 的接口 |
| 认证 | gh-proxy sidecar 或 direct PAT | 二选一; sidecar 持有 PAT, 主进程只持有 HMAC 密钥; `Settings._validate_proxy_or_pat` 拒绝两个都设 |
| 凭据卫生 | `sandbox.redact_credentials()` | 剥掉 `user:pass@` 才进日志/审计/异常 |
| 审计 | `tool_calls` 表 | 审计只看到 agent 提供的参数, 看不到内部凭据 |
| 容器 | 多 stage Dockerfile | `natives-builder` → `wheel-builder` → `pi-base` → `pi-runtime`; robomp 的镜像 `FROM ${PI_BASE}` 继承 |

### host_tools 工具清单

agent 唯一能 mutate GitHub 的接口 (10 个):

- `classify_issue` — 分类 issue
- `set_issue_labels` — 打标签
- `gh_post_comment` — 发评论
- `repro_record` — 记录复现
- `gh_push_branch` — 推分支
- `gh_open_pr` — 开 PR
- `gh_request_review` — 请求 review
- `mark_unable_to_reproduce` — 标记无法复现
- `abort_task` — 中止任务
- `fetch_issue_thread` — 拉 issue 完整对话

## 与 software-factory 的差异

| 维度 | robomp | software-factory |
|---|---|---|
| 栈 | Rust + Node (oh-my-pi 自身), Python (robomp); 绑死在 oh-my-pi | FastAPI 通用栈, 我们自己有完整控制 |
| 隔离 | Docker + per-issue git worktree + 共享 clone pool | (我们的方案待对齐) |
| Session 恢复 | omp 自带 `--continue` 从 `session_dir` 续 | (我们的方案待对齐) |
| 模型路由 | 简单 CSV pool, 随机 | 我们的 provider fallback / 多模型策略更复杂 |
| GitHub 凭据 | 走 gh-proxy sidecar (HMAC), PAT 不进主进程 | 走 GITHUB_TOKEN 直接 |
| 部署 | docker compose v2, 单容器 | (我们的方案待对齐) |
| 调度并发 | `WorkerPool` + SQLite, 进程内 in-flight 锁 | (我们的方案待对齐) |
| 失败恢复 | dispatcher 重启后 `omp --continue` 从 `session_dir` 续 | (我们的方案待对齐) |
| 标签/dashboard | 单一页面 `/` | web 界面有运行历史 |
| Forge 多样性 | 看上去只支持 GitHub | Gitee provider (#205) 已合并 |

## 启发 / 借鉴点

1. **HMAC proxy 模式** — `gh-proxy` sidecar 持有 PAT, 主进程永远不接触, 大幅降低 PAT 泄露面. 对"agent 在容器里跑、凭据要可控" 的场景很有参考价值.
2. **per-issue worktree + clone pool** — `--filter=blob:none` 共享 clone 大幅降低多 issue 并行时的磁盘占用和克隆时间.
3. **`omp --continue` 跨 webhook 续 session** — follow-up 评论/PR review 评论能继承 agent 的先前推理, 不用每次重新读 issue. 这是个 "session 复用" 的好模式.
4. **`farm/<8hex>/<slug>` 分支命名** — 短 hash + slug, 可读且避免冲突.
5. **`abort_task` / `mark_unable_to_reproduce`** — 把"放弃"和"无法复现"作为一等公民状态, 而不是默默失败. 状态机里显式建模失败原因.
6. **多 stage Dockerfile + `FROM ${PI_BASE}`** — 把 natives 编译和运行时分离, Python-only 改动不触发 natives 重新编译, 镜像构建缓存利用率高.
7. **`_inflight` 集合串行化** — 用进程内集合, 不依赖 DB lock, 简单但只对单进程有效; 多副本部署时需注意.
8. **`sandbox.redact_credentials()`** — 集中处理凭据 URL 脱敏, 统一在边界完成, 不散落到各调用点.

## 潜在差异化方向

- **CLI / MCP 接口** (issue #186 已经在做) — robomp 只暴露 HTTP/容器, 我们走 CLI + MCP 双面, 更易集成到本地工作流
- **多 forge 支持** (Gitee 已合并) — robomp 看上去只支持 GitHub
- **多模型策略** — 我们 provider fallback 机制更复杂, 可以做更智能的路由
- **可见性 / 审计** — 我们运行历史 web 界面, 可观察性比 robomp 的单页 dashboard 更细

## 风险

- **功能重叠**: 几乎完全重合, 差异化需要持续明确
- **被卡 API 风险**: can1357 在博文里抱怨 Anthropic 和 Google 封 API, 独立开发者做 harness 调优被掐脖子, 我们的处境类似
- **依赖单一 agent 栈**: robomp 完全绑 oh-my-pi, 我们走通用栈是更稳的选择

## Refs

- 博文 (the harness problem): https://blog.can.ac/2026/02/12/the-harness-problem/
- oh-my-pi 仓库: https://github.com/can1357/oh-my-pi
- robomp 目录: https://github.com/can1357/oh-my-pi/tree/main/python/robomp
- robomp AGENTS.md (架构详细描述): https://github.com/can1357/oh-my-pi/blob/main/python/robomp/AGENTS.md
- robomp README.md: https://github.com/can1357/oh-my-pi/blob/main/python/robomp/README.md
- Pi (父项目, Mario Zechner, 现由 earendil-works 维护): https://github.com/earendil-works/pi
