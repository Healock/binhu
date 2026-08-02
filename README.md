# 滨湖智慧平台

滨湖智慧平台从腾讯文档在线表格同步核查业务数据，也支持管理员导入走访明细 XLSX。平台提供归档、按核查人/社区/日期的统计，以及透视表回写能力。

技术栈：FastAPI、MySQL 8、React、Ant Design、Tailwind CSS、Vite 与 Docker Compose。

## 本地启动

1. 复制配置模板并填写真实值：`Copy-Item .env.example .env`。
2. 安装前端依赖：`Set-Location frontend; npm install`。
3. 构建前端：`npm run build`。
4. 回到项目根目录并启动服务：`docker compose up -d --build`。

开发前端时可在 `frontend` 目录执行 `npm run dev`；开发后端前，需在 `backend/.env` 中提供与根目录 `.env` 相同的 MySQL 密码及加密键。

## 安全与部署

- `AGENTS.md` 是共享协作入口，必须随 Git 同步；`.env`、`backend/.env` 和 `AGENTS.local.md` 是本地文件，禁止提交。
- Docker Compose 不提供数据库或加密键的默认值；缺少变量会明确失败。
- 项目不长期保留一次性部署或迁移脚本。正式发布、回退和备份要求统一以
  [开发与运维手册](docs/operations.md) 为准；真实服务器信息只保存在各电脑自己的
  `AGENTS.local.md` 中。

## 项目文档

开发前先阅读 [共享协作规则](AGENTS.md)，再从 [文档索引](docs/README.md) 进入架构、运维和风险登记。`docs/archive/` 只保存历史证据，不可直接执行。
