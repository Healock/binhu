use std::{
    path::{Path, PathBuf},
    time::Duration,
};

use fs2::available_space;
use futures_util::StreamExt;
use reqwest::{header, Client, StatusCode};
use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
#[cfg(target_os = "android")]
use tauri::Runtime;
use tauri::{
    plugin::{Builder as PluginBuilder, TauriPlugin},
    AppHandle, Emitter, Manager, State, Wry,
};
use tokio::{
    fs::{self, OpenOptions},
    io::AsyncWriteExt,
    sync::Mutex,
};

#[cfg(target_os = "android")]
use tauri::plugin::PluginHandle;

const DEFAULT_UPDATE_BASE_URL: &str = "https://47.100.44.36/updates/android-arm64/";
const MANIFEST_NAME: &str = "manifest.stable.json";
const POLICY_NAME: &str = "policy.stable.json";
const POLICY_CACHE_NAME: &str = "android-update-policy.json";
const PENDING_MANIFEST_NAME: &str = "pending-manifest.json";
const PACKAGE_NAME: &str = "com.bhzh.binhu.android";
const DOWNLOAD_RESERVE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_APK_BYTES: u64 = 1024 * 1024 * 1024;
const EVENT_NAME: &str = "client:update-state";

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateState {
    state: String,
    platform: String,
    current_version: String,
    current_version_code: u64,
    available_version: Option<String>,
    progress: Option<u8>,
    mandatory: bool,
    requires_install_permission: bool,
    error: Option<String>,
}

impl Default for UpdateState {
    fn default() -> Self {
        Self {
            state: "idle".into(),
            platform: "android".into(),
            current_version: env!("CARGO_PKG_VERSION").into(),
            current_version_code: version_code(env!("CARGO_PKG_VERSION")).unwrap_or_default(),
            available_version: None,
            progress: None,
            mandatory: false,
            requires_install_permission: false,
            error: None,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct UpdateManifest {
    schema_version: u32,
    channel: String,
    version: String,
    version_code: u64,
    commit: String,
    published_at: String,
    apk: ApkAsset,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct ApkAsset {
    filename: String,
    size: u64,
    sha256: String,
    signer_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct UpdatePolicy {
    minimum_version: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct NativeAppInfo {
    package_name: String,
    version_name: String,
    version_code: u64,
    signer_sha256: String,
}

#[derive(Default)]
struct RuntimeState {
    state: UpdateState,
    manifest: Option<UpdateManifest>,
    downloaded_path: Option<PathBuf>,
}

pub struct AndroidUpdateManager {
    client: Client,
    runtime: Mutex<RuntimeState>,
    operation: Mutex<()>,
}

impl AndroidUpdateManager {
    fn new() -> Self {
        Self {
            client: Client::builder()
                .connect_timeout(Duration::from_secs(15))
                .timeout(Duration::from_secs(120))
                .user_agent(format!("Binhu-Android/{}", env!("CARGO_PKG_VERSION")))
                .build()
                .expect("Android updater HTTP client must be created"),
            runtime: Mutex::new(RuntimeState::default()),
            operation: Mutex::new(()),
        }
    }

    async fn snapshot(&self) -> UpdateState {
        self.runtime.lock().await.state.clone()
    }

    async fn publish<F>(&self, app: &AppHandle, update: F) -> UpdateState
    where
        F: FnOnce(&mut RuntimeState),
    {
        let snapshot = {
            let mut runtime = self.runtime.lock().await;
            update(&mut runtime);
            runtime.state.clone()
        };
        let _ = app.emit(EVENT_NAME, snapshot.clone());
        snapshot
    }
}

#[cfg(target_os = "android")]
struct AndroidNativeUpdater<R: Runtime> {
    handle: PluginHandle<R>,
}

pub fn native_plugin() -> TauriPlugin<Wry> {
    PluginBuilder::<Wry>::new("android-native-updater")
        .setup(|app, api| {
            #[cfg(target_os = "android")]
            {
                let handle =
                    api.register_android_plugin("com.bhzh.binhu.android", "AndroidUpdaterPlugin")?;
                app.manage(AndroidNativeUpdater { handle });
            }
            #[cfg(not(target_os = "android"))]
            let _ = (app, api);
            Ok(())
        })
        .build()
}

fn update_base_url() -> Result<String, String> {
    let candidate = option_env!("BINHU_ANDROID_UPDATE_BASE_URL")
        .unwrap_or(DEFAULT_UPDATE_BASE_URL)
        .trim()
        .trim_end_matches('/');
    let is_https = candidate.starts_with("https://");
    let local_http_allowed = option_env!("BINHU_ANDROID_UPDATE_ALLOW_LOCAL_HTTP") == Some("1")
        && (candidate.starts_with("http://127.0.0.1")
            || candidate.starts_with("http://localhost")
            || candidate.starts_with("http://10.0.2.2"));
    if !is_https && !local_http_allowed {
        return Err("Android 更新地址必须使用 HTTPS".into());
    }
    Ok(format!("{candidate}/"))
}

fn version_code(value: &str) -> Result<u64, String> {
    let version = Version::parse(value).map_err(|_| "版本号格式无效".to_string())?;
    if !version.pre.is_empty()
        || !version.build.is_empty()
        || version.minor >= 1000
        || version.patch >= 1000
    {
        return Err("Android 版本号必须是 major.minor.patch，且 minor/patch 小于 1000".into());
    }
    Ok(version.major * 1_000_000 + version.minor * 1_000 + version.patch)
}

fn normalize_digest(value: &str) -> String {
    value
        .chars()
        .filter(|character| character.is_ascii_hexdigit())
        .flat_map(char::to_lowercase)
        .collect()
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn validate_manifest(manifest: &UpdateManifest) -> Result<(), String> {
    if manifest.schema_version != 1 || manifest.channel != "stable" {
        return Err("更新清单版本或频道无效".into());
    }
    let parsed = Version::parse(&manifest.version).map_err(|_| "更新版本号无效".to_string())?;
    if !parsed.pre.is_empty() || !parsed.build.is_empty() {
        return Err("正式更新不能使用预发布版本号".into());
    }
    if version_code(&manifest.version)? != manifest.version_code {
        return Err("更新清单的 Android versionCode 不一致".into());
    }
    if manifest.commit.len() != 40 || !manifest.commit.bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        return Err("更新清单的提交号无效".into());
    }
    if manifest.published_at.trim().is_empty() {
        return Err("更新清单缺少发布时间".into());
    }
    let filename = &manifest.apk.filename;
    if filename.len() > 200
        || !filename.starts_with("Binhu-Android-arm64-")
        || !filename.ends_with(".apk")
        || filename.contains('/')
        || filename.contains('\\')
        || filename.contains("..")
        || !filename.contains(&manifest.version)
    {
        return Err("更新 APK 文件名无效".into());
    }
    if manifest.apk.size == 0 || manifest.apk.size > MAX_APK_BYTES {
        return Err("更新 APK 文件大小无效".into());
    }
    if !is_sha256(&normalize_digest(&manifest.apk.sha256))
        || !is_sha256(&normalize_digest(&manifest.apk.signer_sha256))
    {
        return Err("更新 APK 校验信息无效".into());
    }
    Ok(())
}

async fn sha256_file(path: PathBuf) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let mut file = std::fs::File::open(path).map_err(|error| error.to_string())?;
        let mut digest = Sha256::new();
        std::io::copy(&mut file, &mut digest).map_err(|error| error.to_string())?;
        Ok::<_, String>(format!("{:x}", digest.finalize()))
    })
    .await
    .map_err(|error| error.to_string())?
}

fn updates_dir(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_cache_dir()
        .map(|path| path.join("updates"))
        .map_err(|error| error.to_string())
}

fn policy_cache_path(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map(|path| path.join(POLICY_CACHE_NAME))
        .map_err(|error| error.to_string())
}

async fn load_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Option<T> {
    let value = fs::read(path).await.ok()?;
    serde_json::from_slice(&value).ok()
}

async fn save_json<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .await
            .map_err(|error| error.to_string())?;
    }
    let temporary = path.with_extension("json.new");
    let payload = serde_json::to_vec(value).map_err(|error| error.to_string())?;
    fs::write(&temporary, payload)
        .await
        .map_err(|error| error.to_string())?;
    if path.exists() {
        let _ = fs::remove_file(path).await;
    }
    fs::rename(temporary, path)
        .await
        .map_err(|error| error.to_string())
}

#[cfg(target_os = "android")]
async fn native_app_info(app: &AppHandle) -> Result<NativeAppInfo, String> {
    app.state::<AndroidNativeUpdater<Wry>>()
        .handle
        .run_mobile_plugin_async("getAppInfo", serde_json::json!({}))
        .await
        .map_err(|error| error.to_string())
}

#[cfg(not(target_os = "android"))]
async fn native_app_info(_app: &AppHandle) -> Result<NativeAppInfo, String> {
    Ok(NativeAppInfo {
        package_name: PACKAGE_NAME.into(),
        version_name: env!("CARGO_PKG_VERSION").into(),
        version_code: version_code(env!("CARGO_PKG_VERSION"))?,
        signer_sha256: "0".repeat(64),
    })
}

#[cfg(target_os = "android")]
async fn inspect_apk(app: &AppHandle, path: &Path) -> Result<NativeAppInfo, String> {
    app.state::<AndroidNativeUpdater<Wry>>()
        .handle
        .run_mobile_plugin_async(
            "inspectApk",
            serde_json::json!({ "path": path.to_string_lossy() }),
        )
        .await
        .map_err(|error| error.to_string())
}

#[cfg(not(target_os = "android"))]
async fn inspect_apk(_app: &AppHandle, _path: &Path) -> Result<NativeAppInfo, String> {
    Err("APK 检查只能在 Android 设备上执行".into())
}

#[cfg(target_os = "android")]
async fn can_install_packages(app: &AppHandle) -> Result<bool, String> {
    #[derive(Deserialize)]
    #[serde(rename_all = "camelCase")]
    struct PermissionResult {
        allowed: bool,
    }
    let result: PermissionResult = app
        .state::<AndroidNativeUpdater<Wry>>()
        .handle
        .run_mobile_plugin_async("canInstallPackages", serde_json::json!({}))
        .await
        .map_err(|error| error.to_string())?;
    Ok(result.allowed)
}

#[cfg(not(target_os = "android"))]
async fn can_install_packages(_app: &AppHandle) -> Result<bool, String> {
    Ok(false)
}

#[cfg(target_os = "android")]
async fn request_install_permission(app: &AppHandle) -> Result<(), String> {
    app.state::<AndroidNativeUpdater<Wry>>()
        .handle
        .run_mobile_plugin_async::<serde_json::Value>(
            "requestInstallPermission",
            serde_json::json!({}),
        )
        .await
        .map(|_| ())
        .map_err(|error| error.to_string())
}

#[cfg(not(target_os = "android"))]
async fn request_install_permission(_app: &AppHandle) -> Result<(), String> {
    Err("安装权限只能在 Android 设备上申请".into())
}

#[cfg(target_os = "android")]
async fn launch_installer(app: &AppHandle, path: &Path) -> Result<(), String> {
    app.state::<AndroidNativeUpdater<Wry>>()
        .handle
        .run_mobile_plugin_async::<serde_json::Value>(
            "installApk",
            serde_json::json!({ "path": path.to_string_lossy() }),
        )
        .await
        .map(|_| ())
        .map_err(|error| error.to_string())
}

#[cfg(not(target_os = "android"))]
async fn launch_installer(_app: &AppHandle, _path: &Path) -> Result<(), String> {
    Err("APK 安装只能在 Android 设备上执行".into())
}

async fn cached_minimum_version(app: &AppHandle) -> Option<String> {
    let path = policy_cache_path(app).ok()?;
    load_json::<UpdatePolicy>(&path).await.and_then(|policy| {
        Version::parse(&policy.minimum_version)
            .ok()
            .map(|_| policy.minimum_version)
    })
}

async fn fetch_policy(manager: &AndroidUpdateManager, app: &AppHandle) -> Option<String> {
    let base = update_base_url().ok()?;
    let response = manager
        .client
        .get(format!("{base}{POLICY_NAME}"))
        .header(header::CACHE_CONTROL, "no-cache")
        .send()
        .await
        .ok()?;
    if !response.status().is_success() {
        return None;
    }
    let policy: UpdatePolicy = response.json().await.ok()?;
    Version::parse(&policy.minimum_version).ok()?;
    if let Ok(path) = policy_cache_path(app) {
        let _ = save_json(&path, &policy).await;
    }
    Some(policy.minimum_version)
}

fn mandatory_for(current: &str, minimum: Option<String>) -> bool {
    let Ok(current) = Version::parse(current) else {
        return false;
    };
    minimum
        .and_then(|value| Version::parse(&value).ok())
        .is_some_and(|minimum| current < minimum)
}

async fn validate_downloaded_apk(
    app: &AppHandle,
    path: &Path,
    manifest: &UpdateManifest,
    current: &NativeAppInfo,
) -> Result<(), String> {
    let metadata = fs::metadata(path)
        .await
        .map_err(|error| error.to_string())?;
    if metadata.len() != manifest.apk.size {
        return Err("APK 文件长度与更新清单不一致".into());
    }
    let digest = sha256_file(path.to_path_buf()).await?;
    if digest != normalize_digest(&manifest.apk.sha256) {
        return Err("APK SHA-256 校验失败".into());
    }
    let apk = inspect_apk(app, path).await?;
    if apk.package_name != PACKAGE_NAME {
        return Err("更新 APK 的应用标识不正确".into());
    }
    if apk.version_name != manifest.version || apk.version_code != manifest.version_code {
        return Err("更新 APK 的版本信息与清单不一致".into());
    }
    if apk.version_code <= current.version_code {
        return Err("拒绝安装同版本或更低版本 APK".into());
    }
    let apk_signer = normalize_digest(&apk.signer_sha256);
    if apk_signer != normalize_digest(&manifest.apk.signer_sha256) {
        return Err("更新 APK 的签名与清单不一致".into());
    }
    if apk_signer != normalize_digest(&current.signer_sha256) {
        return Err("更新 APK 与当前应用不是同一签名".into());
    }
    Ok(())
}

async fn restore_pending(
    manager: &AndroidUpdateManager,
    app: &AppHandle,
    current: &NativeAppInfo,
) -> Option<(UpdateManifest, PathBuf)> {
    let directory = updates_dir(app).ok()?;
    let manifest_path = directory.join(PENDING_MANIFEST_NAME);
    let manifest: UpdateManifest = load_json(&manifest_path).await?;
    if validate_manifest(&manifest).is_err() || manifest.version_code <= current.version_code {
        let _ = fs::remove_file(manifest_path).await;
        return None;
    }
    let apk_path = directory.join(&manifest.apk.filename);
    if validate_downloaded_apk(app, &apk_path, &manifest, current)
        .await
        .is_err()
    {
        let _ = fs::remove_file(apk_path).await;
        let _ = fs::remove_file(manifest_path).await;
        return None;
    }
    let permission = can_install_packages(app).await.unwrap_or(false);
    manager
        .publish(app, |runtime| {
            runtime.manifest = Some(manifest.clone());
            runtime.downloaded_path = Some(apk_path.clone());
            runtime.state.state = "ready".into();
            runtime.state.available_version = Some(manifest.version.clone());
            runtime.state.progress = Some(100);
            runtime.state.requires_install_permission = !permission;
            runtime.state.error = None;
        })
        .await;
    Some((manifest, apk_path))
}

#[tauri::command]
pub async fn get_update_status(
    app: AppHandle,
    manager: State<'_, AndroidUpdateManager>,
) -> Result<UpdateState, String> {
    let current = native_app_info(&app).await?;
    let cached = cached_minimum_version(&app).await;
    let mandatory = mandatory_for(&current.version_name, cached);
    manager
        .publish(&app, |runtime| {
            runtime.state.current_version = current.version_name.clone();
            runtime.state.current_version_code = current.version_code;
            runtime.state.mandatory = mandatory;
            if runtime.state.state == "applying" {
                runtime.state.state = "ready".into();
            }
        })
        .await;
    if manager.runtime.lock().await.downloaded_path.is_none() {
        let _ = restore_pending(&manager, &app, &current).await;
    }
    Ok(manager.snapshot().await)
}

async fn perform_check(
    manager: &AndroidUpdateManager,
    app: &AppHandle,
) -> Result<UpdateState, String> {
    let current = native_app_info(app).await?;
    if current.package_name != PACKAGE_NAME {
        return Err("当前 Android 应用标识无效".into());
    }
    manager
        .publish(app, |runtime| {
            runtime.state.state = "checking".into();
            runtime.state.current_version = current.version_name.clone();
            runtime.state.current_version_code = current.version_code;
            runtime.state.progress = None;
            runtime.state.error = None;
            runtime.state.requires_install_permission = false;
        })
        .await;

    let cached_policy = cached_minimum_version(app).await;
    let minimum = fetch_policy(manager, app).await.or(cached_policy);
    let mandatory = mandatory_for(&current.version_name, minimum);
    manager
        .publish(app, |runtime| {
            runtime.state.mandatory = mandatory;
        })
        .await;
    let base = update_base_url()?;
    let response = manager
        .client
        .get(format!("{base}{MANIFEST_NAME}"))
        .header(header::CACHE_CONTROL, "no-cache")
        .send()
        .await
        .map_err(|error| format!("无法连接更新服务器：{error}"))?;
    if !response.status().is_success() {
        return Err(format!(
            "更新服务器返回 HTTP {}",
            response.status().as_u16()
        ));
    }
    let manifest: UpdateManifest = response
        .json()
        .await
        .map_err(|_| "更新清单不是有效 JSON".to_string())?;
    validate_manifest(&manifest)?;
    if manifest.version_code <= current.version_code {
        return Ok(manager
            .publish(app, |runtime| {
                runtime.manifest = None;
                runtime.downloaded_path = None;
                runtime.state.state = "idle".into();
                runtime.state.available_version = None;
                runtime.state.progress = None;
                runtime.state.mandatory = mandatory;
                runtime.state.error = None;
            })
            .await);
    }

    let directory = updates_dir(app)?;
    fs::create_dir_all(&directory)
        .await
        .map_err(|error| error.to_string())?;
    let pending_path = directory.join(&manifest.apk.filename);
    if validate_downloaded_apk(app, &pending_path, &manifest, &current)
        .await
        .is_ok()
    {
        let permission = can_install_packages(app).await.unwrap_or(false);
        save_json(&directory.join(PENDING_MANIFEST_NAME), &manifest).await?;
        return Ok(manager
            .publish(app, |runtime| {
                runtime.manifest = Some(manifest.clone());
                runtime.downloaded_path = Some(pending_path.clone());
                runtime.state.state = "ready".into();
                runtime.state.available_version = Some(manifest.version.clone());
                runtime.state.progress = Some(100);
                runtime.state.mandatory = mandatory;
                runtime.state.requires_install_permission = !permission;
                runtime.state.error = None;
            })
            .await);
    }

    Ok(manager
        .publish(app, |runtime| {
            runtime.manifest = Some(manifest.clone());
            runtime.downloaded_path = None;
            runtime.state.state = "available".into();
            runtime.state.available_version = Some(manifest.version.clone());
            runtime.state.progress = None;
            runtime.state.mandatory = mandatory;
            runtime.state.error = None;
        })
        .await)
}

#[tauri::command]
pub async fn check_for_updates(
    app: AppHandle,
    manager: State<'_, AndroidUpdateManager>,
) -> Result<UpdateState, String> {
    let _guard = manager.operation.lock().await;
    Ok(match perform_check(&manager, &app).await {
        Ok(state) => state,
        Err(error) => {
            manager
                .publish(&app, |runtime| {
                    runtime.state.state = "error".into();
                    runtime.state.progress = None;
                    runtime.state.error = Some(error);
                })
                .await
        }
    })
}

async fn download_manifest(
    manager: &AndroidUpdateManager,
    app: &AppHandle,
    manifest: &UpdateManifest,
) -> Result<PathBuf, String> {
    let directory = updates_dir(app)?;
    fs::create_dir_all(&directory)
        .await
        .map_err(|error| error.to_string())?;
    let final_path = directory.join(&manifest.apk.filename);
    let partial_path = final_path.with_extension("apk.partial");
    let mut existing = fs::metadata(&partial_path)
        .await
        .map(|metadata| metadata.len())
        .unwrap_or_default();
    if existing > manifest.apk.size {
        fs::remove_file(&partial_path)
            .await
            .map_err(|error| error.to_string())?;
        existing = 0;
    }
    let remaining = manifest.apk.size.saturating_sub(existing);
    let required = remaining
        .saturating_add(manifest.apk.size)
        .saturating_add(DOWNLOAD_RESERVE_BYTES);
    let free = available_space(&directory).map_err(|error| error.to_string())?;
    if free < required {
        return Err(format!(
            "存储空间不足，至少还需要 {} MB",
            (required - free).div_ceil(1024 * 1024)
        ));
    }

    if existing < manifest.apk.size {
        let base = update_base_url()?;
        let mut request = manager
            .client
            .get(format!("{base}{}", manifest.apk.filename));
        if existing > 0 {
            request = request.header(header::RANGE, format!("bytes={existing}-"));
        }
        let response = request
            .send()
            .await
            .map_err(|error| format!("下载更新失败：{error}"))?;
        let append = existing > 0 && response.status() == StatusCode::PARTIAL_CONTENT;
        if !response.status().is_success() {
            return Err(format!(
                "下载服务器返回 HTTP {}",
                response.status().as_u16()
            ));
        }
        if !append {
            existing = 0;
        }
        let mut output = OpenOptions::new()
            .create(true)
            .write(true)
            .append(append)
            .truncate(!append)
            .open(&partial_path)
            .await
            .map_err(|error| error.to_string())?;
        let mut downloaded = existing;
        let mut last_progress = u8::MAX;
        let mut stream = response.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|error| format!("下载更新失败：{error}"))?;
            output
                .write_all(&chunk)
                .await
                .map_err(|error| error.to_string())?;
            downloaded = downloaded.saturating_add(chunk.len() as u64);
            if downloaded > manifest.apk.size {
                return Err("下载文件超过清单声明长度".into());
            }
            let progress = ((downloaded.saturating_mul(100)) / manifest.apk.size) as u8;
            if progress != last_progress {
                last_progress = progress;
                manager
                    .publish(app, |runtime| {
                        runtime.state.state = "downloading".into();
                        runtime.state.progress = Some(progress);
                    })
                    .await;
            }
        }
        output.flush().await.map_err(|error| error.to_string())?;
    }

    let length = fs::metadata(&partial_path)
        .await
        .map_err(|error| error.to_string())?
        .len();
    if length != manifest.apk.size {
        return Err("下载未完成，可稍后继续".into());
    }
    if final_path.exists() {
        let _ = fs::remove_file(&final_path).await;
    }
    fs::rename(&partial_path, &final_path)
        .await
        .map_err(|error| error.to_string())?;
    Ok(final_path)
}

#[tauri::command]
pub async fn download_update(
    app: AppHandle,
    manager: State<'_, AndroidUpdateManager>,
) -> Result<UpdateState, String> {
    let _guard = manager.operation.lock().await;
    let mut manifest = manager.runtime.lock().await.manifest.clone();
    if manifest.is_none() {
        if let Err(error) = perform_check(&manager, &app).await {
            return Ok(manager
                .publish(&app, |runtime| {
                    runtime.state.state = "error".into();
                    runtime.state.error = Some(error);
                })
                .await);
        }
        manifest = manager.runtime.lock().await.manifest.clone();
    }
    let Some(manifest) = manifest else {
        return Ok(manager.snapshot().await);
    };
    manager
        .publish(&app, |runtime| {
            runtime.state.state = "downloading".into();
            runtime.state.progress = Some(0);
            runtime.state.error = None;
        })
        .await;
    let result = async {
        let path = download_manifest(&manager, &app, &manifest).await?;
        let current = native_app_info(&app).await?;
        if let Err(error) = validate_downloaded_apk(&app, &path, &manifest, &current).await {
            let _ = fs::remove_file(&path).await;
            return Err(error);
        }
        let directory = updates_dir(&app)?;
        save_json(&directory.join(PENDING_MANIFEST_NAME), &manifest).await?;
        let permission = can_install_packages(&app).await.unwrap_or(false);
        Ok::<_, String>((path, permission))
    }
    .await;
    Ok(match result {
        Ok((path, permission)) => {
            manager
                .publish(&app, |runtime| {
                    runtime.downloaded_path = Some(path);
                    runtime.state.state = "ready".into();
                    runtime.state.progress = Some(100);
                    runtime.state.requires_install_permission = !permission;
                    runtime.state.error = None;
                })
                .await
        }
        Err(error) => {
            manager
                .publish(&app, |runtime| {
                    runtime.state.state = "error".into();
                    runtime.state.progress = None;
                    runtime.state.error = Some(error);
                })
                .await
        }
    })
}

#[tauri::command]
pub async fn restart_and_apply(
    app: AppHandle,
    manager: State<'_, AndroidUpdateManager>,
) -> Result<UpdateState, String> {
    let _guard = manager.operation.lock().await;
    let (manifest, path) = {
        let runtime = manager.runtime.lock().await;
        (runtime.manifest.clone(), runtime.downloaded_path.clone())
    };
    let (Some(manifest), Some(path)) = (manifest, path) else {
        return Ok(manager
            .publish(&app, |runtime| {
                runtime.state.state = "error".into();
                runtime.state.error = Some("尚未下载可安装的 Android 更新".into());
            })
            .await);
    };
    let current = match native_app_info(&app).await {
        Ok(value) => value,
        Err(error) => {
            return Ok(manager
                .publish(&app, |runtime| {
                    runtime.state.state = "error".into();
                    runtime.state.error = Some(error);
                })
                .await)
        }
    };
    if let Err(error) = validate_downloaded_apk(&app, &path, &manifest, &current).await {
        let _ = fs::remove_file(&path).await;
        return Ok(manager
            .publish(&app, |runtime| {
                runtime.downloaded_path = None;
                runtime.state.state = "error".into();
                runtime.state.progress = None;
                runtime.state.error = Some(error);
            })
            .await);
    }
    Ok(match can_install_packages(&app).await {
        Ok(false) => {
            let error = request_install_permission(&app)
                .await
                .err()
                .unwrap_or_else(|| "请在系统设置中允许本应用安装未知应用，然后返回重试".into());
            manager
                .publish(&app, |runtime| {
                    runtime.state.state = "ready".into();
                    runtime.state.requires_install_permission = true;
                    runtime.state.error = Some(error);
                })
                .await
        }
        Ok(true) => match launch_installer(&app, &path).await {
            Ok(()) => {
                manager
                    .publish(&app, |runtime| {
                        runtime.state.state = "applying".into();
                        runtime.state.requires_install_permission = false;
                        runtime.state.error = None;
                    })
                    .await
            }
            Err(error) => {
                manager
                    .publish(&app, |runtime| {
                        runtime.state.state = "ready".into();
                        runtime.state.error = Some(error);
                    })
                    .await
            }
        },
        Err(error) => {
            manager
                .publish(&app, |runtime| {
                    runtime.state.state = "ready".into();
                    runtime.state.error = Some(error);
                })
                .await
        }
    })
}

pub fn configure(builder: tauri::Builder<Wry>) -> tauri::Builder<Wry> {
    builder
        .plugin(native_plugin())
        .manage(AndroidUpdateManager::new())
        .invoke_handler(tauri::generate_handler![
            get_update_status,
            check_for_updates,
            download_update,
            restart_and_apply,
        ])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest() -> UpdateManifest {
        UpdateManifest {
            schema_version: 1,
            channel: "stable".into(),
            version: "0.25.28".into(),
            version_code: 25_028,
            commit: "a".repeat(40),
            published_at: "2026-08-24T12:00:00Z".into(),
            apk: ApkAsset {
                filename: "Binhu-Android-arm64-0.25.28.apk".into(),
                size: 1024,
                sha256: "b".repeat(64),
                signer_sha256: "c".repeat(64),
            },
        }
    }

    #[test]
    fn maps_semver_to_monotonic_android_version_code() {
        assert_eq!(version_code("0.25.27").unwrap(), 25_027);
        assert_eq!(version_code("1.2.3").unwrap(), 1_002_003);
        assert!(version_code("1.1000.0").is_err());
        assert!(version_code("1.2.3-beta.1").is_err());
    }

    #[test]
    fn validates_stable_android_manifest() {
        assert!(validate_manifest(&manifest()).is_ok());
        let mut invalid = manifest();
        invalid.apk.filename = "../update.apk".into();
        assert!(validate_manifest(&invalid).is_err());

        let mut invalid_code = manifest();
        invalid_code.version_code = 25_029;
        assert!(validate_manifest(&invalid_code).is_err());

        let mut invalid_signer = manifest();
        invalid_signer.apk.signer_sha256 = "not-a-certificate".into();
        assert!(validate_manifest(&invalid_signer).is_err());
    }

    #[test]
    fn normalizes_certificate_fingerprints() {
        let value = "AA:bb:01";
        assert_eq!(normalize_digest(value), "aabb01");
    }

    #[test]
    fn mandatory_policy_only_blocks_versions_below_the_minimum() {
        assert!(mandatory_for("0.25.27", Some("0.25.28".into())));
        assert!(!mandatory_for("0.25.28", Some("0.25.28".into())));
        assert!(!mandatory_for("0.25.29", Some("0.25.28".into())));
        assert!(!mandatory_for("0.25.27", None));
    }
}
