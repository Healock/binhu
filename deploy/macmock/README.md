# MAC mock 服务

该服务兼容离线居住证客户端原有的 `GET /` 合同，并增加受保护的 `POST /`：

- `GET /` 返回 `{"mac":"AA:BB:CC:DD:EE:FF"}`；
- `POST /` 接收 `{"mac":"..."}`，要求 `X-Macmock-Token` 与 `MACMOCK_WRITE_TOKEN` 一致；
- `OPTIONS /` 支持浏览器 CORS 预检；
- `GET /health` 只返回服务状态，不返回令牌或配置正文。

MAC 允许冒号、连字符、点号或连续 12 位十六进制输入，服务端统一保存为大写冒号格式。写入使用临时文件加 `os.replace`，容器重建时通过 `/data` 卷保留当前值。

生产部署必须把 `MACMOCK_WRITE_TOKEN` 放在受保护的环境文件中，不能进入 Git、镜像层、日志或前端源码。由于服务端口可能暴露在公网，不能省略令牌；若只在本机使用，应将 `MACMOCK_PUBLISH_HOST` 设为 `127.0.0.1`。

本目录只提供合同实现和部署示例，不会自动修改现有生产 `macmock` 容器。升级生产时应先备份当前容器配置和 MAC 状态，再在受控维护窗口中替换并验证 GET、POST、回读和健康检查。
