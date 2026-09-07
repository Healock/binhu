# 0.28.8 独立 MySQL 发布验证

只在新建 `/srv/binhu-release-shadow-<run>` 目录中运行 `prepare.py`。项目不发布端口，使用独立 internal 网络、命名卷、随机密码和八个 `ReleaseShadow_` 数据库；runner 只读挂载本次 Backend 源码，不启动 `main` lifespan 或生产调度器。

现场核对并传入 MySQL 和 Backend 依赖镜像的精确 `sha256` ID。`prepare.py` 只生成私密配置，禁止覆盖已有运行。不得提交生成的 `.env`、`compose.json` 或任何凭据。

启动前检查目录、Compose 项目名、镜像 ID、网络 internal、挂载范围、无端口映射及宿主可用资源；用 `docker compose -f compose.json config --quiet` 检查。只启动当前项目 `mysql`，健康后使用当前项目 `runner` 依次执行 `/validation/verify.py` 和 `/validation/summary_verify.py`。身份检查通过才初始化及写入虚构数据。

`verify.py` 检查真实 MySQL/驱动上的初始化、重复入队和业务事务回滚，证据写入 `artifacts/mysql-bootstrap.json`。这只是发布验收的一部分，不等于完成在线汇总、WebSocket/SSE、页面或生产验收。

`summary_verify.py` 只使用带运行号的虚构任务，驱动真实 worker 后直接读取增量 inspector 表和公开汇总接口，覆盖同日/跨日工作量、revision 栅栏、第一核查人保持、事务回滚和 attendance 完整性；证据写入 `artifacts/summary-verify.json`。不得用快照 marker 或伪造汇总表替代 worker 输出。

清理先核对当前绝对目录、项目标签、容器和卷名，只对当前项目运行 Compose down；删除卷另核对其运行标签。不能使用全局 prune、不能修改 `/root/binhu`、`/srv/binhu`、旧压测或事件总线项目。保留安全验收日志和镜像/源码指纹。

清理前必须将证据复制到**项目目录之外**的独立留存位置，并核对文件清单、可读性、大小与 SHA-256。项目内的 `artifacts/backup` 不算备份；证据副本未验证时禁止 `down -v` 或删除目录。失败日志按尝试编号保留，不用后一次结果覆盖。重建使用新运行号，源码来源必须现场验证，禁止引用猜测的模板路径。
