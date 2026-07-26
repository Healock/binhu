# 滨湖智慧平台

滨湖智慧平台从腾讯文档在线表格同步核查业务数据，提供归档、按核查人/社区/日期的统计，以及透视表回写能力。

技术栈：FastAPI、MySQL 8、React、Ant Design、Tailwind CSS、Vite 与 Docker Compose。

## 本地启动

1. 复制配置模板并填写真实值：`Copy-Item .env.example .env`。
2. 安装前端依赖：`Set-Location frontend; npm install`。
3. 构建前端：`npm run build`。
4. 回到项目根目录并启动服务：`docker compose up -d --build`。

开发前端时可在 `frontend` 目录执行 `npm run dev`；开发后端前，需在 `backend/.env` 中提供与根目录 `.env` 相同的 MySQL 密码及加密键。

## 安全与部署

- `.env`、`backend/.env` 和 `AGENTS.md` 均为本地文件，禁止提交。
- Docker Compose 不提供数据库或加密键的默认值；缺少变量会明确失败。
- `upload.py`、`setup_server.py`、`fix_server.py` 和 `retry_deploy.py` 只从 `BINHU_SSH_*` 环境变量读取远程连接信息。

示例（PowerShell）：

```powershell
$env:BINHU_SSH_HOST = "your-server.example.com"
$env:BINHU_SSH_PASSWORD = "your-ssh-password"
python upload.py
```
