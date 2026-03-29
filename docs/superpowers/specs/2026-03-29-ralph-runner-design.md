# Ralph Runner Design

## Goal

在现有 Software Factory runner 中新增一个最小可交付的 `ralph` agent mode，使其可以像 `openhands` 和 `claude_agent_sdk` 一样被配置、选择并由后台 worker 执行。

## Scope

第一版只做最小接入：

- 在 feature flag 解析和保存逻辑中支持 `ralph`
- 在设置页中支持 Ralph 开关、primary mode、command、timeout
- 在 runner 执行分发中支持 Ralph
- 为 Ralph 增加明确的 failure code
- 补齐自动化测试和基础文档

第一版不做：

- Ralph 的 PRD/task-loop 配置化
- Ralph 引擎选择 UI
- Ralph 项目配置管理界面的扩展
- Ralph 特有高级能力的深度适配

## Design

### Feature flags

`app/services/feature_flags.py` 目前只认识 `openhands` 和 `claude_agent_sdk`。本次新增：

- `RALPH_AGENT_MODE = "ralph"`
- `FEATURE_FLAG_RALPH_ENABLED_KEY`
- `FEATURE_FLAG_RALPH_COMMAND_KEY`
- `FEATURE_FLAG_RALPH_TIMEOUT_KEY`

默认模式顺序保持不变，不把 Ralph 加入默认启用列表，避免影响现有部署。`_normalize_agent_modes()` 和 `_resolve_enabled_modes()` 会扩展为支持第三种 mode。`build_feature_flag_context()` 和 `save_agent_feature_flags()` 也会暴露并持久化 Ralph 相关字段。

### Settings UI

`app/templates/settings.html` 和 `app/routes/web.py` 会加入 Ralph 配置：

- `agent_ralph_enabled`
- `agent_primary_sdk=ralph`
- `ralph_command`
- `ralph_command_timeout_seconds`

表单保存仍走现有 `AgentFeatureFlags` 聚合对象，不新增独立保存路径。

### Runner execution

`app/services/agent_runner.py` 新增：

- `RALPH_AGENT_MODE = "ralph"`
- `RALPH_FAILURE_CODE_COMMAND = "agent_ralph_failed"`

执行方式复用现有 `_run_agent_command()` 通用命令执行器，避免新建一套子进程管理逻辑。Ralph 第一版只要求：

- 在当前 `workspace` 中运行
- 复用现有环境过滤逻辑
- 使用专属 argv builder，为 prompt 提供最小可预测的 CLI 参数注入
- 在 `_execute_agent_sdks()` 中按配置顺序参与 fallback

### Ralph command contract

第一版使用最小命令契约：

- 命令默认值为 `ralph`
- 若命令中未显式提供任务参数，则补 `--task <prompt>`
- 若命令已提供 `--task` 或 `--task=...`，则不重复注入

这样做的目的是先满足 runner 的单次任务执行模型，而不是假设所有 Ralph 上游高级能力都要暴露给 Software Factory。

### Testing

测试覆盖三层：

- `tests/test_feature_flags.py`
  - Ralph mode 归一化
  - primary mode 选择
  - context 暴露 Ralph 字段
- `tests/test_web_settings.py`
  - `/settings` 保存 Ralph 配置并写入数据库
  - 页面渲染包含 Ralph 配置项
- `tests/test_agent_runner.py`
  - mode 归一化默认/fallback 顺序包含 Ralph
  - `_execute_agent_sdks()` 可从 Ralph 回退到 Claude/OpenHands，或将 Ralph 作为 primary
  - Ralph argv builder 行为
  - Ralph 执行路径返回专属 failure code

### Docs

在 `README.md` 和 `README.zh-CN.md` 中补一条 runner 支持 Ralph 的说明，并注明需要在 runner 环境可执行 `ralph` 命令。
