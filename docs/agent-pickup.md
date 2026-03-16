# agent pickup

## 当前目标

提升 autofix agent 的收敛效率，同时保持通用性，不把仓库特例硬编码进 prompt。

当前正在做两件事：

1. 隐藏 per-run workspace 里的 git `alternates`
2. 在进入 agent 前，把结构化 PR metadata 注入 prompt

---

## 当前分支

- branch: `feat/agent-context-hints`
- worktree: `/home/svtter/work/project/software-factory-agent-context`

---

## 相关 issue

- `#99`
  - https://github.com/sun-praise/software-factory/issues/99
  - 标题：`improve agent context: hide git alternates and inject PR metadata`

---

## 已完成但未提交的改动

### 1. prompt 注入 PR metadata

文件：

- `app/services/agent_prompt.py`
- `app/services/agent_runner.py`

已做：

- `build_autofix_prompt(...)` 新增 `pr_metadata`
- prompt 现在会附带：
  - `PR Title`
  - `Base Ref`
  - `Head Ref`
  - `Changed Files`
  - `Diff Stats`
  - `PR Body` 摘要

### 2. 运行前收集 PR metadata

文件：

- `app/services/agent_runner.py`

已做：

- 新增 `_collect_pull_request_metadata(...)`
- 使用：
  - `gh pr view <pr> --json title,body,baseRefName,headRefName,headRefOid,changedFiles,additions,deletions`
- `run_once(...)` 里先收 metadata，再：
  - 回填 `head_sha`
  - 回填 `branch`
  - 注入 prompt

### 3. 不再把 alternates 暴露给 agent workspace

文件：

- `app/services/agent_runner.py`

已做：

- `git clone --reference-if-able ...` 改成：
  - `git clone --dissociate --reference-if-able ...`

目的：

- 让 agent 看到的是普通完整 clone
- 不再因为 `.git/objects/info/alternates` 反复做 repo integrity 自检

---

## 当前运行实例

- code worktree:
  - `/home/svtter/work/project/software-factory-main`
- runtime root:
  - `/home/svtter/work/project/software-factory-runtime`
- database:
  - `/home/svtter/work/project/software-factory-homepage/data/software_factory.db`

当前本地服务：

- web
- worker

都还是跑在最新 `main` 上，不包含当前 `feat/agent-context-hints` 这条分支的未提交改动。

---

## 当前真实样本

- run: `#39`
- PR: `#59`

现象：

- 平台层已基本稳定
- repo cache / per-run workspace / bootstrap / gh PR head 都已通
- 但 agent 仍然会反复做：
  - `gh pr view`
  - `git log / branch / fsck`
  - `alternates` 检查
  - 大范围 repo 探索

这就是当前继续做 `1` 和 `2` 的直接动机。

---

## 下一步

1. 提交当前 `feat/agent-context-hints` 分支
2. 开 PR
3. 合并后把本地 `web/worker` 重启到最新 `main`
4. 重新提交 `PR #59`
5. 观察：
   - agent 是否减少重复上下文探索
   - 是否更快进入 `write -> check -> commit -> push`

---

## 注意

- 不要再直接使用坏掉的旧目录：
  - `/home/svtter/work/project/software-factory-aider`
- 运行目录和代码目录必须继续分离
- 当前优化方向是“减少 agent 的环境噪音”，不是“限制 agent 探索”
