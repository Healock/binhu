# 走访与星级自动获取接口核验记录

2026-08-14 已从生产服务器执行最小化只读采样，确认以下协议。采样未执行新增、修改、删除或登记操作，认证信息和业务正文均不写入仓库、日志或 PR。

## 已确认

- 认证：`POST /api/login`，用户名和密码按旧平台既有协议作为请求参数提交；成功响应的 `data` 是后续请求使用的 `Authorization` 值。
- 走访明细：`GET /api/enterHouse/queryEnterHouseClockInList`。
- 新星级评分管理：`GET /api/starHouse/queryStarHouseList`。
- 两类响应顶层均为 `code`、`data`、`message`，成功时 `code=200`，`data` 直接为记录数组。
- 日期参数使用 `startTime`、`endTime`，分页参数使用 `pageNum`、`pageSize`。
- 单位范围参数使用 `deptCode=320584710000`；走访明细同时发送同值 `pcsdm`。
- 走访字段映射：
  - 派出所名称：`pcsname`
  - 村社区：`jgmc`
  - 进入方式：`isPlate`
  - 地址：`dz`
  - 操作人：`trueName`
  - 操作人账号：`createBy`
  - 入户时间：`createTime`
  - 房间核查数量：`checkRoomCnt`
  - 新增、变更、注销：`cnt1`、`cnt2`、`cnt3`
- 星级字段映射：
  - 派出所名称：`pcsname`
  - 所属社区：`sssq`
  - 地址：`address`
  - 得分：`score`
  - 星级：`houselevelName`
  - 采集时间：`createtime`
  - 隐患详情：`yhxq`
- 星级响应的派出所名称为标准名称；走访响应可能带上级机构前缀。适配器只接受标准名称或以标准名称结尾的短机构前缀，并统一保存为标准名称，其他单位一律拒绝。

## 仍需运行观察

- 登录凭据或令牌的服务端有效期，以及平台是否存在并发登录限制。
- 空数据、权限不足、限流和服务异常的完整业务错误码清单。
- 大日期区间下分页是否始终按页前进；适配器已增加重复页和最大页数保护。
- 生产日常记录量及合理波动阈值。正式替换仍沿用预览、二次确认、空响应和数量骤降保护。

自动确认保持关闭。接口暂时失败时保留旧快照，人工 XLSX 上传继续作为兜底。
