# Claude Agent SDK + Hook 驱动的 PR 审查自动修复系统

版本：v0.1  
面向对象：Codex 实现  
文档类型：项目说明书 + 实施计划书  

---

## 1. 项目背景

当前工作流如下：

1. 开发者通过 Claude Code（下文简称 `cc`）或 Codex 编写代码
2. 代码进入 GitHub Pull Request
3. 第三方代码审查工具或人工 reviewer 将审查意见写入 PR
4. 开发者再手动要求 cc 查看审查意见并改代码

这个流程在人工参与时是合理的，但存在两个问题：

- **重复操作明显**：开发者需要反复下达“去看 review / 根据意见修复”的指令
- **反馈闭环不自动**：PR 已经出现可执行的修复意见，但 cc 不会自动接手

本项目希望将其改造为一个**确定性触发 + AI 执行修复**的系统：

- **Hook 负责确定性触发与流程控制**
- **GitHub 事件负责外部状态感知**
- **Claude Agent SDK 负责真正的代码理解、修改、测试与提交**
- **Web 界面仅用于查看必要状态，保持简洁，不做重后台产品**

---

## 2. 核心目标

### 2.1 目标

构建一个轻量自动化系统，使其满足下列要求：

1. 当开发者通过 cc 发起编码任务时，系统可被**确定性 hook** 捕获
2. 当 GitHub PR 收到新的审查意见后，系统可自动感知
3. 如果审查意见需要修复，系统自动调用 Claude Agent SDK 读取 PR 上下文并修改代码
4. 修改完成后自动提交到原 PR 分支，并留下可追踪记录
5. 系统避免无限循环、重复执行和无效修复
6. 提供一个**极简 Web 页面**，只展示必要信息，不做复杂管理后台

### 2.2 非目标

以下内容不在本期范围内：

- 不做复杂 SaaS 平台
- 不做多租户
- 不做重型审批工作流
- 不做完整项目管理系统
- 不做复杂权限中心
- 不追求在 Web 页面中操作所有细节

---

## 3. 设计原则

### 3.1 Hook 优先，AI 次之

系统中“是否触发”“何时触发”“触发哪条流水线”必须由**确定性机制**控制，而不是由模型自己判断。

### 3.2 AI 只负责需要语义理解的部分

Claude Agent SDK 只处理以下问题：

- 理解 PR 审查意见
- 将意见映射为代码修改
- 执行代码编辑、测试、提交说明生成

### 3.3 Web 必须轻

Web 只做展示，不做复杂编排：

- 查看最近任务
- 查看任务状态
- 查看关联 PR / commit / 错误摘要
- 提供简单重试按钮（可选）

不做：

- 富交互管理台
- 复杂筛选面板
- 大量图表
- 复杂权限模型

### 3.4 单机可跑，后续可扩展

首版必须支持单机或单容器部署，依赖尽量少；如后续需要，可扩展为多 worker。

---

## 4. 用户需求整理

### 4.1 用户真实诉求

用户当前使用 cc 和 Codex 进行开发，并使用线上代码审查工具在 GitHub PR 中给出反馈。用户认为现有流程本身是正确的，但不希望继续手工重复以下动作：

- 手工查看 PR 评论
- 手工整理 review 意见
- 手工要求 cc 再次修复

用户希望做到：

- **只要我通过 cc 开始这次开发，后续 review 闭环应该自动继续**
- **审查意见一旦进入 PR，如果确实需要修复，就自动拉起修复流程**
- **不要依赖 AI 自己“想起来”，而要通过 hook / event 确定性触发**
- **界面尽量轻，能看就行，不要重型 Web 系统**

### 4.2 转换后的系统需求

系统需要具备以下能力：

1. 捕获本地或 CI 中的 Claude Code 生命周期事件
2. 捕获 GitHub PR 的 review / comment 事件
3. 对 review 事件进行归并与去重
4. 判定是否需要自动修复
5. 启动 Agent SDK 修复任务
6. 将结果推回 PR
7. 提供简单可见的执行状态

---

## 5. 总体架构

```text
Claude Code Hook
    -> Local Orchestrator API
        -> Task Queue / State Store
            -> GitHub Adapter
            -> Review Normalizer
            -> Claude Agent SDK Worker
            -> Minimal Web UI
```

### 5.1 组件说明

#### A. Claude Code Hook 层（确定性触发器）

负责在 Claude Code 生命周期关键点触发本地命令或 HTTP 调用。

建议使用的 hook 事件：

- `UserPromptSubmit`：用户提交新任务时记录一次开发会话
- `PostToolUse`：在关键工具成功调用后采集必要上下文
- `PostToolUseFailure`：记录失败信息
- `SubagentStart` / `SubagentStop`：跟踪多代理子任务
- `Notification`：必要时提示用户

**注意**：Hook 是控制层，不负责复杂语义决策。

#### B. Local Orchestrator（本地编排器）

本系统核心服务。职责：

- 接收 hook 事件
- 接收 GitHub webhook
- 归并任务
- 管理状态机
- 决定是否启动 Agent SDK
- 为 Web UI 提供极简查询接口

#### C. GitHub Adapter

负责：

- 校验并解析 GitHub webhook
- 拉取 PR metadata / review / inline comments / commit 信息
- 回写 comment / status
- 拉取待修复 PR 上下文

#### D. Review Normalizer

把不同来源的 review/comment 归一化成结构化修复输入。

#### E. Claude Agent SDK Worker

负责实际修复：

- checkout PR 分支
- 读取 review 摘要
- 修改代码
- 跑测试 / lint
- commit / push
- 回写执行结果

#### F. Minimal Web UI

只提供：

- 最近 20~50 个任务
- 当前运行状态
- 最近失败原因
- 关联仓库 / PR / commit
- 手工重试（可选）

---

## 6. 触发模型

### 6.1 内部触发：Hook

用于确定性捕获“开发任务已经开始/运行/结束”。

#### 用途

1. 将一次 cc 开发过程注册为“受管会话”
2. 将当前分支、仓库、任务 ID 写入本地状态
3. 为后续 GitHub review 自动回流修复建立映射

#### 示例场景

用户在 cc 中输入：

- “实现某个功能并提交 PR”
- “修复 issue #123 并更新 PR”

在 `UserPromptSubmit` 时，hook 将该会话登记到 orchestrator：

- repo
- branch
- session_id
- issue/pr 关联信息（如果能确定）
- 启动时间

### 6.2 外部触发：GitHub Webhook

用于检测 PR 外部状态变化。

建议监听：

- `pull_request_review`
- `pull_request_review_comment`
- `issue_comment`
- 可选：`pull_request`
- 可选：`check_suite` / `check_run`

#### 作用

- 收到 reviewer 的 `changes_requested`
- 收到 inline review comment
- 收到机器人审查工具的总结评论
- 收到 CI 失败后的附加建议（可选扩展）

### 6.3 触发策略

系统使用以下规则：

1. **Hook 决定会话纳管**
2. **GitHub Webhook 决定 review 闭环是否启动**
3. **Agent SDK 只在满足策略时执行**

也就是说：

- Hook 不是替代 GitHub webhook
- Hook 负责内部确定性控制
- GitHub webhook 负责 PR 外部事件感知
- 两者共同完成自动闭环

---

## 7. 状态机设计

### 7.1 PR 级状态

```text
IDLE
  -> SESSION_REGISTERED
  -> PR_OPENED
  -> REVIEW_PENDING
  -> REVIEW_RECEIVED
  -> AUTO_FIX_QUEUED
  -> AUTO_FIX_RUNNING
  -> AUTO_FIX_PUSHED
  -> WAITING_NEXT_REVIEW
  -> DONE / HALTED / FAILED
```

### 7.2 状态说明

- `IDLE`：未纳管
- `SESSION_REGISTERED`：由 hook 注册开发会话
- `PR_OPENED`：PR 已建立并关联到会话
- `REVIEW_PENDING`：等待审查
- `REVIEW_RECEIVED`：收到新 review/comment
- `AUTO_FIX_QUEUED`：已进入自动修复队列
- `AUTO_FIX_RUNNING`：Agent SDK 正在执行
- `AUTO_FIX_PUSHED`：修复代码已推回 PR
- `WAITING_NEXT_REVIEW`：等待 reviewer 下一轮反馈
- `DONE`：完成
- `HALTED`：人工暂停
- `FAILED`：执行失败

---

## 8. 任务判定规则

### 8.1 何时自动修复

首版建议仅在以下条件下自动触发：

1. PR 已被系统纳管
2. 当前 review 来自允许来源
3. review 为 `changes_requested`，或命中配置规则
4. 当前 PR 未超过最大自动修复次数
5. 当前 head SHA 尚未被同一组 review 处理过

### 8.2 何时不自动修复

以下情况不自动修复：

- 机器人自己的回写评论
- 非阻断性评论且未命中规则
- PR 已关闭或已 merge
- 已达到当次 PR 自动修复上限
- 当前已有相同 SHA 的修复任务在运行

### 8.3 优先级建议

- P0：逻辑错误、测试失败、安全问题
- P1：边界条件、异常处理、空值问题
- P2：结构或可维护性建议
- P3：纯风格建议

首版自动修复建议优先处理 P0/P1。

---

## 9. Review Normalizer 设计

### 9.1 输入来源

- PR review body
- inline review comments
- 普通 PR comments
- 可选：CI 结果摘要

### 9.2 归一化输出

统一输出为如下结构：

```json
{
  "repo": "owner/repo",
  "pr_number": 123,
  "head_sha": "abc123",
  "must_fix": [
    {
      "source": "review_comment",
      "path": "src/auth.ts",
      "line": 88,
      "severity": "P0",
      "text": "Missing null handling"
    }
  ],
  "should_fix": [],
  "ignore": [],
  "summary": "2 blocking issues, 1 optional suggestion"
}
```

### 9.3 归一化原则

- 去重相似评论
- 将散乱评论合并为少量明确任务
- 保留原始评论引用信息
- 区分 must-fix / should-fix / ignore

---

## 10. Claude Agent SDK Worker 设计

### 10.1 职责

Worker 不是长期服务的大脑，只是执行单次修复任务。

执行步骤：

1. 拉取仓库并 checkout 到 PR 分支
2. 获取当前 head SHA
3. 读取 normalizer 输出
4. 生成结构化 prompt
5. 调用 Claude Agent SDK
6. 运行测试 / lint / typecheck
7. 生成 commit message
8. 推送到原分支
9. 回写 PR 评论
10. 更新状态

### 10.2 Prompt 约束

Prompt 必须显式限制：

- 只修复 review 指定问题
- 不做无关重构
- 不扩大改动范围
- 优先通过已有测试
- 如果修复失败，输出原因并停止

### 10.3 建议工具权限

- Read
- Edit
- Bash
- Grep / Glob（如 SDK 中存在对应能力）

并明确允许的命令白名单，例如：

- `git status`
- `git diff`
- `pytest`
- `npm test`
- `pnpm test`
- `go test ./...`
- `cargo test`

---

## 11. Hook 设计

### 11.1 目标

Hook 的角色不是“修复代码”，而是：

- 确定性捕获事件
- 将上下文登记到 orchestrator
- 在关键时点触发本地命令或 HTTP 请求
- 提供可审计、可复现的控制流

### 11.2 推荐 hook 用法

#### `UserPromptSubmit`

用途：

- 注册新会话
- 识别当前 repo / branch / cwd
- 标记该会话为受管开发会话

#### `PostToolUse`

用途：

- 当 `Edit` / `Write` / `Bash` 等关键动作完成时，记录必要状态
- 如果检测到创建分支、提交、推 PR 等动作，可更新映射

#### `PostToolUseFailure`

用途：

- 记录失败
- 在必要时提醒用户

#### `SubagentStart` / `SubagentStop`

用途：

- 跟踪复杂任务中的子代理
- 辅助调试，不参与核心决策

### 11.3 Hook 输出形式

建议统一为 HTTP POST 到本地 orchestrator，例如：

`POST /hook-events`

body 示例：

```json
{
  "event": "UserPromptSubmit",
  "session_id": "sess_xxx",
  "repo": "owner/repo",
  "branch": "feature/abc",
  "cwd": "/workspace/repo",
  "timestamp": "2026-03-11T22:00:00+08:00"
}
```

---

## 12. GitHub Webhook 设计

### 12.1 接口

`POST /github/webhook`

### 12.2 必要能力

- 验签
- 事件类型识别
- 解析 PR 编号、repo、review id、comment id、head SHA
- 将事件入库
- 进行 debounce / 去重
- 尝试触发 review 归一化

### 12.3 Debounce 策略

因为一次 review 可能在短时间内产生多条 inline comment，建议：

- 同一 PR 在 30~90 秒内聚合事件
- 聚合窗口结束后只生成一次修复任务

---

## 13. Web UI 设计（极简）

### 13.1 设计要求

必须满足：

- 页面简约
- 依赖简单
- 只展示部分内容
- 不做厚重后台

### 13.2 推荐形式

一个极简页面即可，建议：

- 后端：FastAPI / Go net/http
- 前端：服务端渲染模板，或极少量 HTMX / Alpine.js
- 样式：原生 CSS 或 Pico.css 级别轻量方案

### 13.3 页面内容

#### 首页

展示最近任务列表：

- 时间
- 仓库
- PR 编号
- 状态
- 最近一次动作
- 错误摘要（若有）

#### 详情页

展示：

- 会话 ID
- PR 链接
- 当前 head SHA
- 最近一次 review 摘要
- 最近一次 agent run 摘要
- 最近错误
- 重试按钮（可选）

### 13.4 明确不做

- 不做复杂图表
- 不做多标签管理页
- 不做复杂筛选器
- 不做拖拽式编排
- 不做“像 Jira 一样”的系统

---

## 14. 数据模型建议

### 14.1 `sessions`

记录由 hook 注册的开发会话。

字段建议：

- id
- repo
- branch
- cwd
- source (`claude_code`)
- started_at
- ended_at
- metadata_json

### 14.2 `pull_requests`

记录纳管 PR。

字段建议：

- id
- repo
- pr_number
- head_sha
- branch
- state
- linked_session_id
- autofix_count
- updated_at

### 14.3 `review_events`

记录原始 GitHub review 事件。

字段建议：

- id
- repo
- pr_number
- event_type
- event_key
- actor
- head_sha
- raw_payload_json
- received_at

### 14.4 `autofix_runs`

记录自动修复任务。

字段建议：

- id
- repo
- pr_number
- head_sha
- status
- trigger_source
- normalized_review_json
- logs_path
- commit_sha
- error_summary
- created_at
- finished_at

---

## 15. 防循环与安全设计

### 15.1 幂等键

建议使用：

`repo + pr_number + head_sha + review_batch_id`

同一幂等键只允许执行一次。

### 15.2 自动修复上限

建议：

- 每个 PR 自动修复次数默认不超过 3
- 超过后转为只提醒人工处理

### 15.3 忽略来源

忽略以下评论来源：

- 系统自己回写的机器人评论
- 已配置的噪声 bot
- 非纳管 PR 的 review

### 15.4 命令白名单

Agent SDK 执行 Bash 时必须受限：

- 只允许测试、lint、git 读操作、少量安全写操作
- 严禁高风险系统命令

---

## 16. 实施范围（MVP）

### 16.1 MVP 必做

1. Claude Code hook 注册开发会话
2. GitHub webhook 接收 review 事件
3. review 归一化
4. 自动修复任务入队
5. Claude Agent SDK 执行修复
6. git push 回 PR 分支
7. Web 页面展示最近任务

### 16.2 MVP 可后置

1. 多仓库统一管理
2. 手工暂停 / 恢复
3. 重试按钮
4. 复杂优先级策略
5. CI 信号融合
6. 多 worker 并发

---

## 17. 技术选型建议

### 17.1 推荐技术栈（Python 版）

- Orchestrator：FastAPI
- Web：Jinja2 模板 + 极简 CSS
- DB：SQLite（MVP） / PostgreSQL（后续）
- Queue：SQLite 轮询 / 内存队列（MVP）
- Worker：Python + Claude Agent SDK
- GitHub：PyGithub 或直接 REST API

### 17.2 推荐技术栈（Go + Python 混合）

- Orchestrator：Go
- Web：Go template
- Worker：Python + Claude Agent SDK
- DB：SQLite / PostgreSQL

如果首版目标是快速交付，建议先用 **Python 全栈**，减少跨语言复杂度。

---

## 18. 目录结构建议

```text
project/
  app/
    main.py
    config.py
    db.py
    models.py
    schemas.py
    services/
      hooks.py
      github_webhook.py
      normalizer.py
      queue.py
      orchestrator.py
      agent_runner.py
      git_ops.py
      policy.py
    routes/
      hooks.py
      github.py
      web.py
      api.py
    templates/
      index.html
      run_detail.html
    static/
      app.css
  scripts/
    run_worker.py
    init_db.py
  tests/
  docs/
    architecture.md
    hook-samples.md
```

---

## 19. Codex 实施任务拆分

### Phase 1：基础骨架

- 创建 FastAPI 项目
- 建立 SQLite schema
- 创建 Web 首页与详情页
- 增加 `/hook-events` 与 `/github/webhook`

### Phase 2：Hook 接入

- 定义 hook payload schema
- 实现 `UserPromptSubmit` 入库
- 实现 `PostToolUse` 记录
- 提供 hook 配置样例

### Phase 3：GitHub Webhook 接入

- 实现验签
- 实现 `pull_request_review` 处理
- 实现 `pull_request_review_comment` 处理
- 增加 debounce 逻辑

### Phase 4：Review Normalizer

- 提取 review body
- 提取 inline comments
- 去重与分组
- 生成 normalized review JSON

### Phase 5：Agent SDK 执行器

- checkout PR 分支
- 调用 Claude Agent SDK
- 跑测试
- 生成 commit
- push 回 PR
- 回写 comment

### Phase 6：策略与稳定性

- 幂等控制
- 最大自动修复次数
- 错误恢复
- 日志归档
- 简单重试功能

---

## 20. 验收标准

### 20.1 功能验收

系统上线后，应满足：

1. 用户通过 cc 发起任务时，hook 可稳定记录会话
2. PR 收到新的 blocking review 后，系统能自动创建修复任务
3. 修复任务可调用 Agent SDK 修改代码并推回原分支
4. 相同 review 不会重复触发多次修复
5. Web 页能显示最近任务与失败原因

### 20.2 体验验收

- 页面打开快
- 依赖简单
- 不需要额外重型前端构建链也能运行
- 排错路径直观

### 20.3 稳定性验收

- Bot 评论不会导致无限循环
- review comment 风暴不会造成多任务雪崩
- worker 失败后状态清晰可见

---

## 21. 风险与开放问题

### 21.1 风险

1. 第三方 review 工具写回 GitHub 的格式可能不统一
2. Agent SDK 对大仓库上下文的处理成本可能偏高
3. 如果 prompt 约束不够严格，可能出现超范围修改
4. hook 与 webhook 的会话关联在部分仓库规范下可能需要额外规则

### 21.2 开放问题

1. 是否只处理 `changes_requested`，还是允许部分 comment 也触发
2. 是否支持人工“暂停自动修复”开关
3. 是否允许不同 repo 使用不同测试命令模板
4. 是否允许 Codex 与 Claude Agent SDK 作为双执行器切换

---

## 22. 明确给 Codex 的实现约束

1. **优先实现最小可跑版本，不要过度设计**
2. **Web 必须极简，不要引入重前端框架**
3. **Hook 必须是确定性控制层，不要把触发判断交给 AI**
4. **Review 归一化与幂等控制是核心，不得省略**
5. **Agent SDK 只做代码理解与执行，不做系统编排主脑**
6. **实现时先支持单仓库，再抽象多仓库**
7. **实现时先支持 SQLite，再考虑 PostgreSQL**

---

## 23. 推荐首版结论

本项目首版应采用如下方案：

- **Hook-first**：用 Claude Code hook 做确定性会话注册与本地控制
- **Webhook-driven**：用 GitHub webhook 感知 PR review 变化
- **Agent-executed**：用 Claude Agent SDK 执行修复
- **Thin Web**：用极简页面展示少量必要状态

换句话说：

> 触发用 hook / webhook，执行用 Agent SDK，展示用轻 Web。

这符合当前需求，也能避免系统做得过重。

---

## 24. 参考实现提示（给 Codex）

可以优先交付以下内容：

1. `README.md`
2. `docs/architecture.md`
3. `app/main.py`
4. `app/routes/hooks.py`
5. `app/routes/github.py`
6. `app/services/normalizer.py`
7. `app/services/agent_runner.py`
8. `app/templates/index.html`
9. `app/templates/run_detail.html`
10. `example_hooks.json`
11. `example.env`

---

## 25. 附录：与官方能力对齐的要点

- Claude Code hooks 可以在会话生命周期的多个固定事件点触发，支持命令、HTTP 和 prompt-based hooks，因此适合承担**确定性控制层**。citeturn340211search0turn340211search2
- Claude Agent SDK 提供和 Claude Code 相同的 agent loop、工具与上下文管理，适合承担**代码修复执行层**。citeturn340211search1turn340211search4
- Claude Code 也支持通过 CLI 的 `-p` 方式程序化执行；这一点可作为调试通道或降级路径，但本方案主执行器仍以 Agent SDK 为主。citeturn340211search8

