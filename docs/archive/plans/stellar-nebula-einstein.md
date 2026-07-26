# 全链条汇总工具 - 服务器部署计划

> [!WARNING]
> 历史证据，不可直接执行。内容可能与当前源码、配置和运行环境不一致；现行规则见 [文档索引](../../README.md) 与 [风险登记](../../known-risks.md)。

## 目标
将全链条汇总工具部署到 `$BINHU_SSH_USER@$BINHU_SSH_HOST:$BINHU_SSH_PORT`，路径 `/root/bhzh`。

## 部署架构

```
用户浏览器 (80端口)
    │
    ▼
Nginx (宿主机, 80端口)
    ├── /          → /root/bhzh/frontend/dist/  (前端静态文件)
    ├── /api/*     → http://localhost:8000       (后端 API 代理)
    └── SPA 回退   → index.html (支持 React Router)

Docker Compose 管理:
    ├── MySQL 8.0 容器   (内部 3306, 暴露 3306 给宿主机)
    └── Backend 容器     (内部 8000, 暴露 8000 给宿主机供 Nginx 代理)
```

### 关键设计决策
- **同源代理**: 前端用 `/api` 相对路径请求，Nginx 反向代理到后端，无需 CORS
- **MySQL 持久化**: Docker volume `mysql_data` 确保数据不丢失
- **前端构建**: 本地构建 `dist/` 后传输到服务器，Nginx 直接托管

---

## 步骤

### 1. 本地：修改后端生产配置

**文件**: `backend/main.py`
- 移除或放宽 CORS 限制（同源代理下不需 CORS，但保留以备 API 外部调用）

**文件**: `backend/Dockerfile`
- 移除 `--reload` 参数（生产模式）

**文件**: `docker-compose.yml`
- 移除 backend 的 `volumes: ./backend:/app`（生产环境不需要代码热挂载）
- 保留 MySQL port 3306 暴露（便于调试）
- 保留 backend port 8000 暴露（Nginx 需要）

### 2. 本地：构建前端

```bash
cd frontend && npm run build
```
产出 `frontend/dist/` 目录。

### 3. 本地：创建 Nginx 配置

**新建**: `nginx/bhzh.conf`
- 监听 80 端口
- `/api/` 代理到 `localhost:8000`
- 静态文件根目录指向 `/root/bhzh/frontend/dist`
- SPA 模式 `try_files` 回退到 `index.html`

### 4. 传输文件到服务器

通过 SCP (sshpass) 将以下内容传输到 `/root/bhzh/`:
- `docker-compose.yml`
- `backend/` 整个目录
- `frontend/dist/` 构建产物
- `nginx/bhzh.conf`

### 5. 服务器：安装 Nginx

```bash
apt-get install -y nginx
```

### 6. 服务器：配置 Nginx

- 将 `bhzh.conf` 复制到 `/etc/nginx/conf.d/`
- 移除默认站点
- 测试配置并重启 Nginx

### 7. 服务器：启动 Docker Compose

```bash
cd /root/bhzh && docker-compose up -d
```
按顺序启动 MySQL → Backend，等待健康检查通过。

### 8. 验证

- `curl localhost:8000/api/health` → 后端健康检查
- `curl localhost/` → 前端页面可访问
- `curl localhost/api/health` → Nginx 代理正常工作
- 浏览器访问 `http://$BINHU_SSH_HOST` → 全功能验证

---

## 需要修改的文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/Dockerfile` | 修改 | 移除 `--reload` |
| `backend/main.py` | 修改 | CORS 调整（可选） |
| `docker-compose.yml` | 修改 | 移除 dev volume 挂载 |
| `nginx/bhzh.conf` | **新建** | Nginx 站点配置 |

---

## 风险与注意

1. **SSH 密码传输**: 使用 `sshpass` 自动登录，密码仅在内存中
2. **MySQL 数据安全**: Docker volume 持久化，容器重启不丢数据
3. **首次启动**: MySQL 首次初始化需要约 30 秒，Backend 依赖健康检查
4. **防火墙**: 确保服务器防火墙开放 80 端口（Nginx）
5. **腾讯文档 API**: 需要在 OAuth 设置中配置回调地址为服务器 IP
> **历史归档，禁止直接执行。** 本计划描述旧版本实现，路径、端口、变量和操作顺序可能已失效。请先阅读 `docs/README.md` 与 `docs/known-risks.md`，并重新验证当前源码和运行状态。
