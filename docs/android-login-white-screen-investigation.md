# Android 客户端登录后白屏排查报告

报告日期：2026-08-24
客户端版本：0.25.27 Android Debug POC
应用包名：`com.bhzh.binhu.android`
最新验收包：`Binhu-Android-arm64-0.25.27-safe-area-dialer-fix-debug.apk`
SHA-256：`22115aae6d88bd95230a6988e8940a1fffbd3bf0ebcaed49982e8ecf7aa8d6eb`

> 2026-08-24 复核更新：白屏根因已经确认。模拟器使用 Android System WebView `91.0.4472.114`，登录后的业务组件调用 `Object.hasOwn()` 时产生 `Uncaught TypeError: Object.hasOwn is not a function`，导致 React 渲染中断。当前没有 Android 原生崩溃证据。本文后续排查项保留为历史记录和回归检查清单，不再代表本次白屏的首要原因。

## 已实施修复

- 在主 JavaScript 包执行前加载独立的 `Object.hasOwn` 兼容补丁。
- Android 构建目标显式设为 Chrome/WebView 91；该设置用于语法降级，内置方法兼容仍由补丁负责。
- Android WebView 使用系统栏和屏幕挖孔 Insets 设置安全区，避免顶部品牌栏与状态栏重叠。
- 电话拨打改用 Tauri opener 插件调用 Android 系统拨号应用，不再让 WebView直接加载 `tel:`。
- 系统拨号器打开失败时复制电话号码并显示错误提示。

## 一、问题现象

1. 应用可以正常安装和启动。
2. 登录页能够显示，账号密码可以提交。
3. 登录成功并进入系统后，页面变为纯白，无法继续操作。
4. 本轮竖屏修复已经生效，但登录后的业务页面尚未完成验收。

## 二、当前已确认事项

- APK 内已写入竖屏方向：`screenOrientation=portrait`。
- 已设置 `resizeableActivity=false` 和软键盘 `adjustResize`。
- Android TV 的 `LEANBACK_LAUNCHER` 入口已经移除。
- Android 构建启用了 `VITE_NATIVE_MOBILE=true`，前端会按手机端模式判断。
- 前端资源随 APK 一起打包，接口数据来自远程服务器。
- API 地址为 `https://www.h332a0a4b.nyat.app:48726/api`。
- APK 为 Debug 构建，可以使用 Android Studio、ADB 和 Chrome WebView 调试工具。
- 当前开发机未连接模拟器或手机，因此尚未取得白屏现场的 Logcat、Console 和 Network 记录，根因仍需现场日志确认。

## 三、优先排查方向

### 1. 登录后的 JavaScript 运行异常（最高优先级）

登录页和登录后的页面使用同一套 JavaScript 主程序，但认证成功后会首次挂载 `Layout`、`RoleDashboard`、`MobileDock`、在线状态和通知等组件。任何一个组件在 Android WebView 中抛出未捕获异常，都可能导致 React 根节点停止渲染并显示白屏。

请重点记录 Chrome DevTools Console 中出现的第一条红色错误。后续连锁错误通常只是结果，不应覆盖第一条错误。

### 2. 认证或维护状态触发了不适合本地资源的整页跳转（高优先级）

当前前端在接口返回 `401` 或 `503` 时会执行：

```text
window.location.href = '/login'
```

该方式适用于普通网站，但 Android Tauri 客户端加载的是 APK 内的本地前端。整页跳转到 `/login` 时，Tauri 本地资源服务器可能无法按 SPA 路由返回 `index.html`，最终表现为白屏。

需要确认白屏前是否有任何接口返回 `401` 或 `503`，以及白屏后的 WebView 地址是否变成了类似 `https://tauri.localhost/login`、`tauri://localhost/login` 或其他无法加载的本地地址。

如果证实为此原因，应把认证失效跳转改为 React Router 内部导航，或者统一回到本地 `index.html` 后再恢复 `/login` 路由，避免整页加载本地子路径。

### 3. Android WebView 的 Cookie、CORS 或来源白名单问题（高优先级）

登录请求本身可能成功，但登录后的 `/api/auth/me`、仪表盘、在线状态等请求仍可能因为 Cookie 未携带、预检被拒绝或 Android Tauri 来源未加入白名单而失败。

请核对以下内容：

- `POST /api/auth/login` 的状态码和响应体；
- 紧随其后的 `GET /api/auth/me` 是否为 `200`；
- 请求是否携带会话 Cookie；
- 请求头中的 `X-Binhu-Client-Platform` 是否为 `android`；
- 请求头中的 `X-Binhu-Device-Id`、`X-Binhu-Client-Version` 是否存在；
- 预检请求的 `Origin` 实际值；
- 服务端是否明确允许该 Android Tauri Origin，而不只是 Windows Tauri/Electron Origin。

### 4. 登录后动态 JavaScript 分包加载失败（中优先级）

部分业务页面和图表使用动态导入。登录后首次加载相关 `.js` 分包时，如果本地资源路径、CSP 或 Tauri Asset Protocol 处理异常，可能出现 `ChunkLoadError`、`Failed to fetch dynamically imported module` 或 CSP 拒绝信息。

请在 Network 中检查所有本地 `.js`、`.css` 请求，确认没有红色失败项，并在 Console 中搜索 `chunk`、`module`、`CSP`、`Refused`。

### 5. 强制手机模式触发了仅登录后才运行的移动端代码路径（中优先级）

本轮修复使 Android 原生构建始终返回手机模式。这可能让登录后首次进入 `MobileDock`、手机任务路由或手机专用组件，从而暴露网页手机版此前未覆盖的账号、权限或空配置问题。

建议临时对比两次运行：

- 保持 `VITE_NATIVE_MOBILE=true`；
- 临时改为 `VITE_NATIVE_MOBILE=false`，但仍保持竖屏。

如果第二种构建不再白屏，说明问题集中在手机端组件或手机路由，而不是登录、Cookie 或 Tauri 容器本身。

## 四、模拟器同事需要执行的取证步骤

### 1. 记录设备环境

请回传：

- 模拟器名称和 Android 版本；
- Android System WebView 版本；
- 屏幕分辨率和 DPI；
- 是否使用代理、VPN 或抓包证书；
- APK 是否覆盖安装，还是卸载旧版后全新安装。

### 2. 获取 Logcat

复现前清空日志：

```powershell
adb logcat -c
adb shell am force-stop com.bhzh.binhu.android
```

开始记录完整日志，然后启动应用、登录并等待白屏出现：

```powershell
adb logcat -v time | Tee-Object android-login-white-screen.log
```

白屏出现后等待约 10 秒，再停止记录。请保留完整日志，不要只截取最后几行。重点关注：

```text
chromium
CONSOLE
AndroidRuntime
FATAL EXCEPTION
cr_
tauri
com.bhzh.binhu.android
```

### 3. 使用 Chrome 检查 WebView

1. 在电脑 Chrome 打开 `chrome://inspect/#devices`。
2. 找到 `com.bhzh.binhu.android` 对应 WebView并点击 `inspect`。
3. Console 勾选保留日志，清空后重新登录。
4. Network 勾选 Preserve log，再重新登录。
5. 白屏后记录当前页面 URL。
6. 保存 Console 全部错误和 Network HAR 文件。
7. 在 Elements 中检查 `#root`：
   - `#root` 为空：优先判断 JavaScript 崩溃或错误的整页跳转；
   - `#root` 有大量节点但不可见：优先判断 CSS、尺寸或遮罩层问题；
   - 页面 URL 变为本地 `/login` 且资源失败：优先判断整页跳转问题。

### 4. 必须截图的 Network 请求

请至少提供以下请求的 URL、状态码、Origin、响应体摘要和 Cookie 情况：

```text
OPTIONS /api/auth/login
POST    /api/auth/login
GET     /api/auth/me
GET     /api/app/bootstrap
登录后第一个失败的 API 请求
登录后第一个失败的本地 .js 请求
```

## 五、建议的最小对照实验

按以下顺序测试，每次测试前清除应用数据：

1. 当前 `portrait-fix` APK，记录完整日志。
2. 同一 APK 使用另一个权限较低的普通账号登录。
3. 同一 APK 使用管理员账号登录。
4. 临时禁用原生手机模式的对照 APK。
5. 在同一模拟器的 Chrome 浏览器中登录现有网页手机版。

判断方式：

- Chrome 网页版也白屏：更可能是前端手机布局或账号数据问题；
- 只有 Tauri APK 白屏：更可能是本地路由、Tauri Origin、Cookie、CSP 或动态分包问题；
- 只有特定账号白屏：更可能是用户权限、手机 Dock 配置或首页数据结构问题；
- 禁用手机模式后正常：更可能是手机专用组件或路由问题。

## 六、期望回传材料

请将以下材料统一发回客户端开发侧：

1. `android-login-white-screen.log`；
2. Chrome DevTools Console 截图或导出文本；
3. Network HAR；
4. 白屏时的页面 URL；
5. `#root` 是否为空的检查结果；
6. 模拟器、Android System WebView 版本和分辨率；
7. 测试账号的角色、岗位和权限组名称，不需要提供密码；
8. 是否可以稳定复现以及具体操作步骤。

## 七、原始阶段结论（已由上述复核更新取代）

现阶段只能确认问题发生在“认证成功后切换到业务组件树”的阶段，不能仅根据白屏现象判断为竖屏或 CSS 问题。首要任务是取得 Console 第一条异常、登录后接口状态和白屏后的实际 URL。

优先级最高的两个候选原因是：

1. 登录后某个 React 组件在 Android WebView 中抛出未捕获异常；
2. `401/503` 触发 `window.location.href = '/login'`，使本地 Tauri SPA 跳转到无法加载的子路径。

拿到上述日志后，客户端侧可以进一步给出明确修复，不建议在缺少现场证据时继续盲目调整屏幕方向或页面宽度。
