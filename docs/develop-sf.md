# software-factory 修复过程总结

## 目标

把 `software-factory` 从“本地能启动但经常跑挂”的状态，推进到“可以稳定创建 run、创建隔离 workspace、启动 Claude agent、执行 baseline checks、开始真正改代码”的状态。

当前跟踪的核心案例是：

- PR: `https://github.com/sun-praise/software-factory/pull/59`

---

## 一、最初暴露出来的问题

### 1. 共享 workspace 模型不稳定

最早的运行模型依赖一个长期存在的基准目录，例如：

- `/home/svtter/work/project/software-factory-aider`

这个目录既承担：

- 运行实例目录
- git 仓库目录
- per-run worktree 的父目录

最后导致的问题是：

- `web` / `worker` 进程仍然绑定在一个已经被删除的目录 inode 上
- 磁盘上的同名目录已经不是 git 仓库，只剩 `logs/` 和 `.pytest_cache/`
- 新 run 启动后，agent 看到的 `/workspace` 不是有效仓库
- 数据库里出现假 `running`

### 2. Claude CLI 链路不稳定

在接 Claude agent 的过程中，先后遇到：

- `stream-json` 模式参数不完整
  - 缺 `--verbose`
- `write /dev/stdout: broken pipe`
- 超时后 Docker/Claude 子进程没清干净
- 失败后回退到 `OpenHands`
- `OpenHands` 又卡在交互式 TUI

### 3. 容器环境不完整

Docker runtime 接进来后，又暴露出：

- 容器里没有 `gh`
- 容器里没有 GitHub auth
- 容器命令行和日志会泄露 API key
- 容器内 agent workspace 的 `.git/objects/info/alternates` 会干扰 git 行为

### 4. baseline checks 环境不完整

后面又发现：

- 工作区 `.venv` 只安装了 `requirements.txt`
- 但实际 checks 依赖：
  - `pytest`
  - `ruff`
  - `mypy`
- 导致 run 并不是卡在代码，而是卡在环境没有准备好

### 5. PR head 解析不稳定

把共享 workspace 重构为 repo cache 模型后，新的卡点变成：

- `unable to resolve PR head branch`

根因是：

- 通过 GitHub API 获取 PR head 信息时走了匿名 fallback
- 对当前场景不稳定
- 解析失败后没有拿到 `headRefName` / `headSha`

---

## 二、已经完成的关键修复

### 1. Docker runtime 接入与稳定化

完成了以下工作：

- 新增 Claude 容器 runtime
- 仓库内增加 Dockerfile
  - `docker/claude-agent/Dockerfile`
- 默认支持用容器运行 Claude，而不是直接在宿主机跑

同时修掉了：

- 容器里缺 `gh`
- 容器里 `GH_TOKEN` / `GITHUB_TOKEN` 不可用
- Docker 启动参数和日志暴露 secrets
- `broken pipe`
- 运行 heartbeat 不刷新 `updated_at`
- Claude 失败后静默 fallback 到 OpenHands

### 2. 把 “只剩 baseline 失败也可以继续 push” 这条链路打通

之前逻辑是：

- 只要 checks 不全绿，就不 commit / push

后来改成：

- 先记录 baseline failures
- 只把“新增失败”当阻塞
- 如果修改后只剩 baseline failures，允许继续 commit / push

这是让“agent 真正把修复同步回 PR”成立的关键。

### 3. repo cache + per-run workspace 重构

这是这次最大的架构修复。

现在模型改成：

- `runtime root`
  - 只放运行时数据
  - 例如：
    - `logs/`
    - `.software-factory-repo-cache/`
    - `.software-factory-run-workspaces/`
- `repo cache`
  - 每个 repo 一个 mirror/cache
- `per-run workspace`
  - 每个 run 单独 clone
  - agent 只在自己的独立 workspace 内读写

这样避免了：

- 常驻 worker 绑定共享 git worktree
- 基准目录被删后整条链路损坏

### 4. runtime root 保护和 stale run 回收

新增了：

- worker 启动时的 runtime root 校验
- stale run 回收逻辑
- `running/cancel_requested` 超时回收

目的是避免：

- 假 `running`
- 旧进程消失后数据库状态不恢复

### 5. PR head 解析改为直接使用 `gh`

最终改成：

- 直接调用：
  - `gh pr view <pr> --json headRefName,headRefOid`

不再依赖：

- GitHub API 匿名 fallback

这样解决了：

- `unable to resolve PR head branch`

这是当前 repo-cache 架构真正跑起来的关键修复。

### 6. bootstrap 自动补 checks 工具

新增逻辑：

- 从实际 check commands 推断 Python 工具模块
- 若仓库没有显式的：
  - `requirements-dev.txt`
  - `requirements-test.txt`
- 就自动把这些工具装进 workspace `.venv`

当前会自动补：

- `pytest`
- `ruff`
- `mypy`

这解决了：

- baseline 一开始就死在 “No module named pytest/ruff/mypy”

---

## 三、相关 PR / Issue 记录

### 已创建的 issue

- `#95`
  - 允许用户注入自定义 prompt 逻辑

### 关键 PR

- `#96`
  - repo cache + per-run workspace 重构
- `#97`
  - 用 `gh` 解析 PR head
- `#98`
  - bootstrap 自动安装 `pytest/ruff/mypy`

此外，前面已经合并过一批铺路 PR，包括：

- Docker runtime
- Docker image
- secret 脱敏
- `gh` 注入容器
- `updated_at` heartbeat
- stale run recovery
- baseline / preexisting checks 语义修正

---

## 四、当前运行实例状态

当前本地实例采用的是：

- 代码目录：
  - `/home/svtter/work/project/software-factory-main`
- runtime root：
  - `/home/svtter/work/project/software-factory-runtime`
- 数据库：
  - `/home/svtter/work/project/software-factory-homepage/data/software_factory.db`

这意味着：

- 代码 worktree 和 runtime root 已经彻底分离
- 不再复用坏掉的 `software-factory-aider` 目录

---

## 五、run #39 当前说明

`run #39` 证明了下面这些链路已经打通：

### 已打通

- run 能正常入队和被 worker 领取
- repo cache 能创建
- per-run workspace 能创建
- workspace 内有真实 `.git`
- Claude 容器能正常启动
- `gh pr view 59` 可以正常执行
- bootstrap 能自动补：
  - `pytest`
  - `ruff`
  - `mypy`
- baseline `pytest` 已通过

### 当前仍在处理中的问题

当前已经不是 runtime bug，而是仓库代码本身的 baseline 问题：

- `ruff` 仍有 13 个问题
- `mypy` 仍有 3 个问题

Claude 已经进入：

- 读上下文
- 写文件
- 跑测试

也就是说，现在 agent 已经在做真正的代码修复，而不是被运行框架本身绊住。

---

## 六、经验结论

### 1. 共享 workspace 模型不可靠

这次最关键的结论是：

- 不要让常驻 worker 绑定一个共享、长期可变的 git worktree

正确方式是：

- `repo cache + per-run workspace`

### 2. 不要依赖 agent 临时猜环境

如果 checks 需要：

- `pytest`
- `ruff`
- `mypy`

这些要么来自仓库显式 dev 依赖，
要么由 bootstrap 层根据 checks 自动补齐。

不能默认把“环境准备”完全甩给 agent 临场决定。

### 3. PR metadata 获取要走稳定路径

对当前这个项目，直接用：

- `gh pr view`

比匿名 GitHub API fallback 更可靠。

### 4. 容器是必要前提，但不是全部

Docker runtime 解决了：

- 权限
- 宿主机污染
- 命令隔离

但还需要：

- 正确的 token 注入
- 正确的 repo checkout
- 正确的 bootstrap

否则只是“换了运行位置”，不会自动变稳定。

---

## 七、后续建议

### 1. 继续观察 `run #39`

这是当前最关键的真实样本。

如果它最终：

- 成功 commit + push 回 PR #59

就说明这条链路已经基本可用。

### 2. 后续继续考虑 repo-specific runtime image

后面可以考虑：

- 基于仓库依赖和技术栈自动构建可复用 runtime image

这样会进一步减少：

- 每次 run 都重复装依赖
- 环境波动

### 3. 用户注入 prompt 逻辑

这个需求已记录到：

- issue `#95`

这会让用户把额外规则插入到系统 prompt 里，提升灵活性。

---

## 八、当前一句话总结

这次修复已经把 `software-factory` 从“运行框架经常先挂”推进到“运行框架基本稳定，agent 开始真正修代码”的阶段。

目前剩下的主要问题，已经不再是 runtime 基础设施，而是 agent 能否把当前 PR 的实际代码问题修完并成功 push 回去。
