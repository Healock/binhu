# 场所码云端部署单元

该目录用于 `47.100.44.36` 的场所码接收服务，不与 `/srv/binhu-updates`、客户端更新网关或滨湖主业务数据库共用目录、数据库账号和发布命令。

## 首次准备

1. 在服务器控制台核对磁盘、Docker、Nginx、443 站点和现有 `/updates/` 路由。
2. 运行 `install-server.sh` 安装受限发布账号和目录；脚本不会启动容器或修改 Nginx。
3. 将真实配置写入 `/etc/binhu-venue/receiver.env`，权限保持 `0600`。
4. 放置 RSA 公钥、滨湖请求验签公钥、云端响应签名私钥和 mTLS CA；私钥不得进入 Git 或发布包。服务启动时会拒绝缺少活动 RSA 公钥或 Ed25519 签名密钥的配置。
5. 将 `nginx-http-context.conf` 加入 `http {}`，将 `nginx-server-locations.conf` 加入现有 443 server；必须先保存原配置并运行 `nginx -t`。
   宝塔 Nginx 的场所码脱敏日志写入 `/www/wwwlogs/`，不要使用该服务器上不存在的 `/var/log/nginx/`。
6. 复用现有 `binhu-ip-cert-renew.timer`。不得再安装第二个公网 IP 证书续签任务。

### 预检、迁移与启动顺序

在正式服务器上先执行只读预检。它不会安装 Docker、修改 Nginx 或启动容器：

```sh
./validate-server.sh
```

预检通过、完成磁盘和 MySQL 网络暴露评估后，再由管理员安装 Docker Compose，并将本目录文件安装到 `/etc/binhu-venue`。数据库 schema 可以重复执行：

```sh
./migrate.sh
docker compose -f /etc/binhu-venue/docker-compose.yml up -d
docker compose -f /etc/binhu-venue/docker-compose.yml ps
```

`migrate.sh` 默认读取 `/srv/binhu-venue/state/current.env` 中的不可变镜像标签，只启动 MySQL 并运行 `python -m app.migrate`，不会修改生产 Nginx，也不会启用场所码开关。首次候选验证也可通过 `BINHU_VENUE_COMPOSE_ENV` 指向经核验的候选标签文件。Receiver 的 `/health/ready` 会同时检查应用进程和 MySQL 连通性，发布网关也只以该就绪接口作为切换成功条件。

首次生产切换仍需单独人工批准。发布前必须备份云端 MySQL 和 `/srv/binhu-venue/photos`，发布失败只回退接收服务镜像，不自动恢复数据库。

### Docker 与 Nginx 管理入口

`binhu-venue-install-docker` 只安装 Docker Engine、Buildx 和 Compose 插件并启动 Docker 服务，不启动场所码容器。它要求至少 10 GiB 可用空间；发现冲突包时停止并要求人工审计，不会自动卸载系统组件。

`binhu-venue-activate-nginx` 只能在 Receiver 的 `/health/ready` 已同时通过应用与 MySQL 检查后运行。它会备份当前 HTTPS 配置、HTTP 上下文配置和重复的 `bt_proxy.conf`，再把重复文件改为非 `.conf` 后缀，将场所码 include 加入现有更新站点。`nginx -t`、更新清单、无证书内部接口或场所码反代任一验收失败时，脚本恢复原配置并重新加载 Nginx。

现有宝塔 MySQL 不属于本部署单元。正式安装 Docker 前必须从服务器外部确认公网 TCP 3306 不可达；若仍可达，应先在云厂商安全组收紧，不能由本脚本擅自修改现有 MySQL 或启用主机防火墙。

`/etc/binhu-venue/mysql.env` 只保存 MySQL 根密码和应用数据库密码；`/etc/binhu-venue/receiver.env` 保存同一应用数据库密码及接收服务密钥，不能包含根密码。两处 `MYSQL_PASSWORD` 必须一致。

接收容器固定使用 UID/GID `10001`。`/srv/binhu-venue/photos` 必须为 `10001:10001`、权限 `0700`；三份挂载密钥及证书文件必须为 `root:10001`、权限 `0640`，公钥目录 `/etc/binhu-venue/venue-encryption-public` 必须为 `root:10001`、权限 `0750`。

## 安全边界

- MySQL 与 Receiver 只加入 `venue_private` 内部网络，均不映射宿主机端口。
- 无密钥、无数据库凭据的 Ingress 只做固定 TCP 转发，并将宿主机 `127.0.0.1:48727` 转给 Receiver；这是因为 Docker 的 internal 网络本身不会创建宿主机端口网关。
- Docker 网络标记为 `internal`，接收容器不能主动访问滨湖内网或生产数据库。
- 公共接口由 Nginx 做 IP 限流，应用用 MySQL 做全局、场所和设备限流。
- `/api/internal/` 同时要求 mTLS 和 Ed25519 签名；Nginx 只向上游传递验证结果。
- 日志 URI 会把 `/venue/{token}` 记录为 `/venue/[redacted]`，应用关闭 Uvicorn access log。
- 加密登记最长排队 7 天；成功确认的正文和照片默认 24 小时内清理，审计只保留安全原因码。
