# Windows 7 / Windows 10 安装问题修复与验收报告

报告日期：2026-08-25

当前版本：0.25.27

状态：代码修复及本机构建验证完成，等待 Windows 7 SP1 x64 与 Windows 10/11 实机复验。

## 一、问题现象与根因

### 1. Windows 7

安装过程中启动 `Binhu-Velopack-Setup.exe` 时出现：

```text
无法定位程序输入点 GetDpiForSystem 于动态链接库 user32.dll 上。
```

`GetDpiForSystem` 不存在于 Windows 7。原首装程序虽然先安装了 VxKex，但随后直接启动 Velopack Setup，导致 Velopack 安装器本身没有经过 VxKex 兼容层。

### 2. Windows 10/11

安装后启动 Tauri 客户端时出现：

```text
Could not find the WebView2 Runtime.
```

Velopack 包只携带 Tauri 主程序，不会自动执行 Tauri NSIS 安装器原有的 WebView2 前置安装逻辑。目标电脑没有可供当前用户使用的 WebView2 Runtime 时，Tauri 窗口无法创建。

### 3. Windows 7 增量更新进度

Win7 客户端下载增量更新时，界面会长时间停留在 `0%`。更新服务器已正确提供文件长度与断点下载响应；根因是当前 Velopack 版本在增量包下载阶段没有向 JavaScript 回调转发网络进度，单个增量包下载完成后才进入后续进度。

## 二、修复内容

### Windows 7 首装链路

- 安装或校验 VxKex `1.2.1.2229`。
- 为释放到临时目录的 Velopack Setup 写入 VxKex 配置。
- 通过 `VxKexLdr.exe` 启动 Velopack Setup，不再直接执行。
- 保留原有 VxKex 版本校验、安装失败提示和重启提示。

修复后的链路：

```text
Inno Setup -> 安装/校验 VxKex -> VxKexLdr.exe -> Velopack Setup -> 客户端
```

### Windows 10/11 首装链路

- 新增 WebView2 感知的 Inno Setup 首装引导器。
- 安装前检查系统级及当前用户级 WebView2 注册信息。
- 未检测到运行时时，执行微软 WebView2 Bootstrapper 的静默安装。
- 安装成功并再次检测到 WebView2 后，才启动 Velopack Setup。
- 已有 WebView2 的电脑直接跳过前置安装。

修复后的链路：

```text
Inno Setup -> 检测/安装 WebView2 -> Velopack Setup -> Tauri 客户端
```

当前嵌入的是微软 WebView2 Bootstrapper。目标电脑首次安装 WebView2 时需要能够访问微软下载服务；断网环境会显示明确的安装失败提示。若今后要求完全离线安装，应改为嵌入 Evergreen Standalone Installer，代价是首装包明显增大。

### Windows 7 增量更新进度

- 下载增量包期间，每 500 毫秒读取 Velopack 临时包的实际大小，换算为 `0%` 至 `69%` 的真实下载进度。
- `70%` 至 `100%` 继续由 Velopack 表示增量校验与合成阶段。
- 对所有进度值执行范围限制和单调递增保护，避免进度倒退或出现非法数值。
- 新增本地 `logs/updater.log`，记录检查、下载、完成和失败阶段，不记录 Cookie、账号或业务数据。

## 三、本地构建产物

```text
E:\bhzh-forth\artifacts\updates\win7-x64\Binhu-Win7-x64-Setup-0.25.27.exe
E:\bhzh-forth\artifacts\updates\win10-x64\Binhu-Win10-x64-Setup-0.25.27.exe
```

配套 SHA-256 文件与 `checksums.sha256` 已在各自目录生成。

本次验收安装器校验值：

```text
Win7  SHA-256: 96cf75268c61abd45604f19757198dd7a62ffcf3e216cf586a179a5357b11d06
Win10 SHA-256: 54e173d42e39f249b6b7de07cc752740109c4f8a23f4b0a6bebc4b9876447bce
```

这批包是本地实机验收包，未签名、未提交、未推送、未上传更新服务器。由于工作区没有上一版 full 包，本次使用显式的 full-only 模式生成，只包含 0.25.27 全量包，不作为正式增量发布资料。

## 四、已完成验证

- 桌面架构校验通过：`Desktop architecture OK: 0.25.27`。
- Win7 Electron 更新模块测试：6 项全部通过。
- 前端测试：233 项全部通过。
- 更新服务器网关测试：12 项全部通过。
- Win7 与 Win10 安装器脚本均通过 PowerShell 语法检查。
- Win7 与 Win10 的 Inno Setup 安装器均编译成功。
- 两个平台的 Velopack full 包、发布清单及 SHA-256 文件均重新生成。
- `git diff --check` 未发现空白错误。

## 五、仍需实机验收

### Windows 7 SP1 x64

1. 卸载旧测试客户端；如需验证完整首装链路，同时卸载旧 VxKex。
2. 运行新的 `Binhu-Win7-x64-Setup-0.25.27.exe`。
3. 确认不再出现 `GetDpiForSystem` 错误。
4. 确认安装完成后快捷方式可启动客户端。
5. 验证登录、关闭重开、登录状态保留及在线更新检查。
6. 从 0.25.27 下载生产更新时，确认进度在下载期间持续增长，不再长时间停留在 `0%`。
7. 若仍有异常，读取客户端用户数据目录下的 `logs/updater.log`，确认停留在下载、校验还是增量合成阶段。
8. 若 VxKex 安装要求重启，重启后重新执行安装器并完成安装。

### Windows 10/11 x64

1. 优先在未安装 WebView2 Runtime 的干净环境验证。
2. 运行新的 `Binhu-Win10-x64-Setup-0.25.27.exe`。
3. 确认安装器自动完成 WebView2 安装，客户端不再提示缺少 Runtime。
4. 在已安装 WebView2 的电脑上复测，确认不会重复安装。
5. 断网且未安装 WebView2 时复测，确认提示可读且不会留下半安装客户端。
6. 验证登录、关闭重开、登录状态保留及在线更新检查。

## 六、发布建议

实机验收通过后再升级正式版本并走统一发布流程。正式发布必须取得服务器当前上一版 full 包，以生成并核验 delta；不要将本次 full-only 验收包直接覆盖生产更新清单。

首批安装包仍未进行代码签名，会显示未知发布者并可能触发杀毒软件告警，这不属于本次两个安装故障的根因，但正式铺开前仍应安排签名证书。
