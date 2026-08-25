# Android 客户端 POC 构建与验收记录

报告日期：2026-08-24
代码版本：0.25.27（提交 `c881eb7`）
应用标识：`com.bhzh.binhu.android`

## 一、结论

Android 第一阶段架构已经搭建完成，并成功生成可安装的 ARM64 Debug APK。

本 POC 使用 Tauri 2 封装现有 React/Vite 手机端布局。HTML、JavaScript、CSS、图片等前端资源随 APK 一同安装，不从服务器下载前端页面；登录、API 和业务数据仍通过 HTTPS 访问生产服务。

本阶段未实现离线工具、推送通知、相机集成、离线数据库和客户端自更新。

## 二、已完成内容

- 新增独立的 `mobile` Android 工程，不复用 Windows 窗口和更新逻辑。
- Android 前端使用 `frontend/.env.android` 指向远程 API。
- 构建时将生产前端复制到 `mobile/apps/shell-ui`，并封装进 APK。
- 原生客户端平台请求头使用 `android`。
- Android 不显示 Windows 自定义标题栏和 Velopack 更新控件。
- 启用 WebView Cookie 和第三方 Cookie，用于跨来源的远程登录会话。
- 启用 Android 系统返回键的 WebView 历史导航。
- 使用项目现有警徽生成 Android 自适应应用图标。
- Android SDK、NDK、Gradle 和 Rust 构建缓存位于 `E:\bhzh-forth`。
- 根目录 `VERSION` 可同步 Android 的 npm、Cargo 和 Tauri 版本。

## 三、自动验证结果

| 项目 | 结果 |
| --- | --- |
| 前端单元测试 | 通过，230 项 |
| Android 前端生产构建 | 通过，14,590 个模块 |
| Rust ARM64 原生库 | 通过 |
| Gradle Debug APK 构建 | 通过 |
| 包名 | `com.bhzh.binhu.android` |
| 版本 | `0.25.27`，versionCode `25027` |
| 最低系统 | Android 7.0，API 24 |
| 目标系统 | API 36 |
| CPU 架构 | `arm64-v8a` |
| APK 签名 | Android Debug 签名，仅用于 POC |

构建过程存在前端大分块和 Gradle/Tauri 弃用提醒，但没有阻止 APK 生成。大分块可能影响旧手机的首次加载时间和内存占用，需要真机测量。

## 四、产物

```text
E:\bhzh-forth\artifacts\Binhu-Android-arm64-0.25.27-debug.apk
E:\bhzh-forth\artifacts\Binhu-Android-arm64-0.25.27-debug.sha256
```

APK 大小：139,223,992 字节。
SHA-256：`ec984b99966223f7d4f336e8d90e3f968e0ba7dbcfd23912a3c3ea80afddf56b`

## 五、真机验收清单

以下项目不能仅靠构建环境确认，必须在真实 Android 手机上测试：

- 安装后能显示本地登录页面，断网启动时页面仍能出现。
- 联网登录成功，不出现 `Failed to fetch`。
- 刷新、杀进程和重新打开后仍保持登录状态。
- Android 与 Windows 同时登录，两个客户端均能使用。
- Android 退出登录时只退出当前设备。
- 系统返回键优先关闭弹窗或返回上一页，不意外退出应用。
- 状态栏、挖孔屏、安全区和底部导航不遮挡内容。
- 软键盘弹出时不遮挡账号、密码、搜索框和表单按钮。
- 文件选择、XLSX/PDF 上传、下载和外部打开正常。
- Univer 表格和 Rete 编辑器的触控、滚动、缩放、内存占用可接受。
- 弱网、断网和网络恢复后应用不会白屏或丢失本地页面。

## 六、正式发布前待办

- 创建并妥善保管正式 Android 签名密钥，禁止使用 Debug 签名发布。
- 决定首批支持的 CPU 架构；当前 POC 只支持 ARM64。
- 在至少一台 Android 7/8 旧设备和一台当前主流设备上验收。
- 确认服务器生产环境允许 Android Tauri 的精确 Origin 和请求头。
- 明确 APK 分发、全量更新和版本强制升级策略。
- 第二阶段再设计离线模式入口、离线工具数据和本地存储边界。
