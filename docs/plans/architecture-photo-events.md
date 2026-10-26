# 照片请求与附件事件域

本 PR 只增加 `photo.requested` 和 `photo.attached` 元数据事件合同及计数投影。
照片二进制、对象存储凭据、下载链接和附件写入不会进入 Kafka/Flink/Redis；
文件存储与权限仍由 Python/MySQL/OSS 受控路径负责。
