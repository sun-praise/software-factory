# 故障排除指南

本文档提供 software-factory 的常见问题诊断和解决方案。

## 常见问题 (FAQ)

### Q1: 数据库初始化失败

**症状**: 运行 `python scripts/init_db.py` 报错

**可能原因**:
- `DB_PATH` 目录不存在或无写权限
- SQLite 版本过低 (< 3.35)
- 数据库文件已损坏

**解决方案**:
```bash
# 检查目录权限
mkdir -p ./data
ls -la ./data

# 检查 SQLite 版本
sqlite3 --version

# 删除损坏的数据库重新初始化
rm -f ./data/software_factory.db
python scripts/init_db.py
```

### Q2: 服务启动失败，端口被占用

**症状**: `Address already in use` 错误

**解决方案**:
```bash
# 查找占用端口的进程
lsof -i :8000

# 使用其他端口启动
uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

### Q3: Webhook 请求无响应或返回 400

**症状**: GitHub webhook 触发后服务无反应

**检查清单**:
1. 确认 `content-type` 为 `application/json`
2. 确认 `x-github-event` header 已设置
3. 确认 JSON 格式正确
4. 检查签名验证是否失败

**调试命令**:
```bash
# 模拟 webhook 请求
curl -i -X POST http://127.0.0.1:8000/github/webhook \
  -H 'content-type: application/json' \
  -H 'x-github-event: pull_request_review' \
  -d '{"action":"submitted","review":{"id":123},"pull_request":{"number":10}}'
```

### Q4: Hook 事件未被处理

**症状**: Hook 请求返回 200 但数据未入库

**可能原因**:
- 事件类型不被支持
- payload 格式不正确
- 数据库写入失败

**调试步骤**:
```bash
# 检查服务日志
tail -f logs/app.log

# 模拟 Hook 请求
curl -i -X POST http://127.0.0.1:8000/hook-events \
  -H 'content-type: application/json' \
  -d '{"event":"UserPromptSubmit","session_id":"test-123","repo":"owner/repo","branch":"main"}'

# 验证数据入库
sqlite3 ./data/software_factory.db "SELECT * FROM sessions ORDER BY id DESC LIMIT 5"
```

### Q5: Worker 任务不执行

**症状**: `autofix_runs` 表有 queued 状态的任务，但 worker 不处理

**可能原因**:
- Worker 未启动
- 达到并发限制 (`MAX_CONCURRENT_RUNS`)
- PR 锁被其他任务持有

**诊断命令**:
```bash
# 检查队列状态
sqlite3 ./data/software_factory.db "SELECT id, status, pr_number, created_at FROM autofix_runs ORDER BY id DESC LIMIT 10"

# 检查运行中的任务
sqlite3 ./data/software_factory.db "SELECT COUNT(*) FROM autofix_runs WHERE status = 'running'"

# 检查 PR 锁
sqlite3 ./data/software_factory.db "SELECT repo, pr_number, lock_owner, lock_expires_at FROM pull_requests WHERE lock_owner IS NOT NULL"

# 手动启动 worker (单次)
python scripts/run_worker.py --once
```

### Q6: 任务一直处于 retry_scheduled 状态

**症状**: 任务失败后未自动重试

**可能原因**:
- 重试时间未到 (`retry_after`)
- 达到最大重试次数 (`MAX_RETRY_ATTEMPTS`)
- 错误码不可重试

**诊断命令**:
```bash
# 查看重试状态
sqlite3 ./data/software_factory.db "
SELECT id, status, attempt_count, max_attempts, retry_after, last_error_code, error_summary
FROM autofix_runs
WHERE status = 'retry_scheduled'
"

# 手动触发重试 (将 retry_after 设为过去时间)
sqlite3 ./data/software_factory.db "
UPDATE autofix_runs SET retry_after = '2020-01-01T00:00:00Z' WHERE id = <run_id>
"
```

### Q7: 自动修复次数达到上限

**症状**: PR 不再触发自动修复

**原因**: `autofix_count` >= `MAX_AUTOFIX_PER_PR`

**解决方案**:
```bash
# 检查修复次数
sqlite3 ./data/software_factory.db "
SELECT repo, pr_number, autofix_count FROM pull_requests WHERE pr_number = <number>
"

# 重置计数 (谨慎操作)
sqlite3 ./data/software_factory.db "
UPDATE pull_requests SET autofix_count = 0 WHERE pr_number = <number>
"
```

### Q8: Bot 评论导致无限循环

**症状**: 系统不断响应自己的评论

**解决方案**:
1. 确认 `AUTOFIX_COMMENT_AUTHOR` 配置正确
2. 将系统 bot 添加到 `BOT_LOGINS`
3. 配置 `NOISE_COMMENT_PATTERNS` 过滤匹配模式

```bash
# .env 配置示例
AUTOFIX_COMMENT_AUTHOR=software-factory[bot]
BOT_LOGINS=dependabot[bot],renovate[bot],software-factory[bot]
NOISE_COMMENT_PATTERNS=^Auto-generated,^LGTM
```

### Q9: 日志文件过大或丢失

**症状**: 日志目录占用空间过大或日志文件不存在

**解决方案**:
```bash
# 检查日志目录
ls -la logs/
du -sh logs/

# 检查归档目录
ls -la logs/archive/

# 手动清理过期日志
find logs/archive -name "*.log" -mtime +7 -delete
```

**配置调整**:
```bash
# 减少保留天数
LOG_RETENTION_DAYS=3
```

### Q10: SQLite 数据库锁定

**症状**: `database is locked` 错误

**可能原因**:
- 多个进程同时访问数据库
- 长时间事务未提交

**解决方案**:
```bash
# 检查持有锁的进程
fuser ./data/software_factory.db

# 重启服务释放锁
pkill -f "uvicorn app.main"

# 使用 WAL 模式 (推荐)
sqlite3 ./data/software_factory.db "PRAGMA journal_mode=WAL"
```

## 日志查看指南

### 日志位置

| 类型 | 路径 | 说明 |
|------|------|------|
| 应用日志 | `logs/app.log` | FastAPI 应用日志 |
| Run 日志 | `logs/runs/{run_id}.log` | 单个任务执行日志 |
| 归档日志 | `logs/archive/` | 过期日志归档 |

### 日志格式

```
2024-03-12 10:30:45,123 INFO [app.services.agent_runner] run_id=123 repo=owner/repo pr_number=10 status=success
```

### 查看命令

```bash
# 实时查看应用日志
tail -f logs/app.log

# 查看特定 run 的日志
cat logs/runs/123.log

# 搜索错误
grep -r "ERROR" logs/

# 按时间过滤
grep "2024-03-12 10:" logs/app.log
```

## 状态诊断命令

### 查看会话状态

```bash
sqlite3 ./data/software_factory.db "
SELECT id, repo, branch, started_at, ended_at
FROM sessions
ORDER BY id DESC
LIMIT 10
"
```

### 查看 PR 状态

```bash
sqlite3 ./data/software_factory.db "
SELECT repo, pr_number, state, autofix_count, lock_owner, lock_expires_at
FROM pull_requests
ORDER BY updated_at DESC
LIMIT 10
"
```

### 查看 Run 状态

```bash
sqlite3 ./data/software_factory.db "
SELECT id, repo, pr_number, status, attempt_count, error_summary, created_at
FROM autofix_runs
ORDER BY id DESC
LIMIT 20
"
```

### 查看审查事件

```bash
sqlite3 ./data/software_factory.db "
SELECT id, event_type, actor, pr_number, received_at
FROM review_events
ORDER BY id DESC
LIMIT 10
"
```

### 统计任务状态

```bash
sqlite3 ./data/software_factory.db "
SELECT status, COUNT(*) as count
FROM autofix_runs
GROUP BY status
"
```

## 错误码说明

| 错误码 | 含义 | 是否可重试 |
|-------|------|-----------|
| `pr_locked` | PR 被其他任务锁定 | 是 |
| `head_sha_mismatch` | HEAD SHA 不匹配 | 否 |
| `checkout_failed` | Git checkout 失败 | 是 |
| `git_failed` | Git 操作失败 | 是 |
| `checks_failed` | 检查命令失败 (lint/test) | 否 |
| `unsupported_project_type` | 不支持的项目类型 | 否 |
| `pr_comment_failed` | 发布 PR 评论失败 | 是 |
| `no_changes` | 无代码变更 | - (视为成功) |
| `unknown_failure` | 未知错误 | 是 |

## 性能问题排查

### 响应慢

**症状**: HTTP 请求响应时间过长

**诊断步骤**:
```bash
# 1. 检查数据库大小
ls -lh ./data/software_factory.db

# 2. 检查表记录数
sqlite3 ./data/software_factory.db "
SELECT 'sessions' as tbl, COUNT(*) FROM sessions
UNION ALL SELECT 'pull_requests', COUNT(*) FROM pull_requests
UNION ALL SELECT 'review_events', COUNT(*) FROM review_events
UNION ALL SELECT 'autofix_runs', COUNT(*) FROM autofix_runs
"

# 3. 检查数据库完整性
sqlite3 ./data/software_factory.db "PRAGMA integrity_check"

# 4. 优化数据库
sqlite3 ./data/software_factory.db "VACUUM"
sqlite3 ./data/software_factory.db "ANALYZE"
```

### 队列堆积

**症状**: queued 状态任务数量持续增长

**诊断步骤**:
```bash
# 1. 检查堆积数量
sqlite3 ./data/software_factory.db "
SELECT status, COUNT(*) FROM autofix_runs GROUP BY status
"

# 2. 检查 worker 是否运行
ps aux | grep run_worker

# 3. 检查并发限制
sqlite3 ./data/software_factory.db "
SELECT COUNT(*) FROM autofix_runs WHERE status = 'running'
"

# 4. 增加 worker 数量或并发限制
# 修改 .env: MAX_CONCURRENT_RUNS=5
```

### 内存占用高

**症状**: 服务内存持续增长

**诊断步骤**:
```bash
# 1. 检查进程内存
ps aux | grep uvicorn

# 2. 重启服务
pkill -f "uvicorn app.main" && sleep 2 && uvicorn app.main:app &

# 3. 检查是否有内存泄漏
# 使用 memory_profiler 或 tracemalloc
```

## 数据库问题

### 迁移失败

**症状**: 添加新字段后服务启动失败

**解决方案**:
```bash
# 1. 备份数据库
cp ./data/software_factory.db ./data/software_factory.db.bak

# 2. 检查缺失的列
sqlite3 ./data/software_factory.db ".schema autofix_runs"

# 3. 手动添加列 (示例)
sqlite3 ./data/software_factory.db "
ALTER TABLE autofix_runs ADD COLUMN new_column TEXT DEFAULT ''
"

# 4. 重新初始化 (最后手段)
rm ./data/software_factory.db
python scripts/init_db.py
```

### 数据恢复

**症状**: 数据库损坏或误删除

**解决方案**:
```bash
# 1. 从备份恢复
cp ./data/software_factory.db.bak ./data/software_factory.db

# 2. 尝试恢复损坏的数据库
sqlite3 ./data/software_factory.db ".recover" > recover.sql
sqlite3 ./data/software_factory_new.db < recover.sql

# 3. 检查数据完整性
sqlite3 ./data/software_factory.db "PRAGMA integrity_check"
```

## 健康检查

### 快速健康检查脚本

```bash
#!/bin/bash
# health_check.sh

echo "=== Service Health Check ==="

# 1. 检查服务是否运行
echo "1. Service status:"
curl -s http://127.0.0.1:8000/healthz || echo "FAILED"

# 2. 检查数据库
echo -e "\n2. Database status:"
sqlite3 ./data/software_factory.db "PRAGMA integrity_check"

# 3. 检查队列
echo -e "\n3. Queue status:"
sqlite3 ./data/software_factory.db "
SELECT status, COUNT(*) FROM autofix_runs GROUP BY status
"

# 4. 检查最近错误
echo -e "\n4. Recent errors:"
sqlite3 ./data/software_factory.db "
SELECT id, error_summary, last_error_at
FROM autofix_runs
WHERE status = 'failed'
ORDER BY id DESC
LIMIT 5
"

# 5. 检查日志
echo -e "\n5. Recent log errors:"
grep -r "ERROR" logs/ | tail -5
```

### 使用方法

```bash
chmod +x health_check.sh
./health_check.sh
```
