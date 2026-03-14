# Skill: 启动本地 software-factory 服务

## 适用场景
在本地机器上启动 `software-factory` 后端服务，并确保可以通过浏览器访问。

## 前置要求
- 已在仓库根目录：`/home/svtter/work/project/software-factory`
- 已安装依赖（`pip install -r requirements.txt`）
- 已有数据库初始化：`python scripts/init_db.py`

## 启动步骤

1. 保证工作区在 `main` 分支并同步最新代码。
```bash
cd /home/svtter/work/project/software-factory
git checkout main
git pull --ff-only origin main
```

2. 检查 8000 端口是否被其他项目占用（很多机器会有本地旧服务）。
```bash
lsof -i :8000 || true
```

3. 选择监听端口。
- 若 8000 空闲：
```bash
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
- 若 8000 被占用：
```bash
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

4. 推荐：后台常驻启动（避免 Shell 退出后退出）。
```bash
nohup python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload </dev/null >/tmp/software-factory-main-uvicorn.log 2>&1 &
```

5. 验证服务。
```bash
curl -sS http://127.0.0.1:8001/healthz
curl -sS http://<SERVER_IP>:8001/healthz
curl -I http://127.0.0.1:8001/
```

期望结果：
- `/healthz` 返回 JSON：`{"ok":true}`
- 首页返回 `200 OK` 和 HTML 页面

说明：
- 若你在另一台机器访问，把 `<SERVER_IP>` 替换为本机内网 IP（例如 `192.168.2.14`）。

6. 检查监听和日志。
```bash
ss -ltnp | rg ":8001|:8000"
tail -n 80 /tmp/software-factory-main-uvicorn.log
```

## 常见问题

- 页面能否访问 127.0.0.1 失败，但 IP 可访问：多半是服务只监听了 `0.0.0.0` 或只监听了某个网卡。
  - 优先按实际访问场景选择 `--host`。
- `Port already in use`：先停掉占用端口进程，再重启；或直接换到 `8001`。
- `curl /healthz` 正常，但浏览器空白：检查 URL 是不是带了错误端口，确认不是访问旧服务端口。
