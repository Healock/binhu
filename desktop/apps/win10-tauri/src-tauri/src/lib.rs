use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    path::PathBuf,
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    thread,
    time::Duration,
};

use serde::{Deserialize, Serialize};
use tauri::{Emitter, Manager, State};
use velopack::{
    sources::HttpSource, UpdateCheck, UpdateInfo, UpdateManager, UpdateOptions, VelopackAsset,
};

const DESKTOP_CONFIG: &str = include_str!("../../../../config/desktop.config.json");
const UPDATE_URL: &str = "https://47.100.44.36/updates/win10-x64/";
const POLICY_URL: &str = "https://47.100.44.36/updates/win10-x64/policy.stable.json";
const CHECK_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);

#[cfg(windows)]
pub fn ensure_windows_shortcuts() {
    use std::os::windows::ffi::{OsStrExt, OsStringExt};
    use windows::core::{Interface, PCWSTR};
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CoUninitialize, IPersistFile, CLSCTX_INPROC_SERVER,
        COINIT_APARTMENTTHREADED,
    };
    use windows::Win32::UI::Shell::{
        IShellLinkW, SHGetFolderPathW, ShellLink, CSIDL_DESKTOPDIRECTORY, CSIDL_PROGRAMS,
    };

    fn wide(value: &std::path::Path) -> Vec<u16> {
        value
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect()
    }

    fn write_shortcut(
        shortcut: &std::path::Path,
        target: &std::path::Path,
        working_directory: &std::path::Path,
        icon: &std::path::Path,
    ) -> bool {
        let shortcut_w = wide(shortcut);
        let target_w = wide(target);
        let working_w = wide(working_directory);
        let icon_w = wide(icon);
        let product_w: Vec<u16> = "滨湖智慧平台"
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect();
        unsafe {
            let shell_link: IShellLinkW =
                match CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER) {
                    Ok(value) => value,
                    Err(_) => return false,
                };
            if shell_link.SetPath(PCWSTR(target_w.as_ptr())).is_err()
                || shell_link
                    .SetWorkingDirectory(PCWSTR(working_w.as_ptr()))
                    .is_err()
                || shell_link
                    .SetDescription(PCWSTR(product_w.as_ptr()))
                    .is_err()
                || shell_link
                    .SetIconLocation(PCWSTR(icon_w.as_ptr()), 0)
                    .is_err()
            {
                return false;
            }
            let persist_file: IPersistFile = match shell_link.cast() {
                Ok(value) => value,
                Err(_) => return false,
            };
            persist_file.Save(PCWSTR(shortcut_w.as_ptr()), true).is_ok()
        }
    }

    fn shell_folder(csidl: u32) -> Option<std::path::PathBuf> {
        let mut path = [0u16; 260];
        unsafe { SHGetFolderPathW(None, csidl as i32, None, 0, &mut path) }.ok()?;
        let length = path.iter().position(|value| *value == 0)?;
        Some(std::path::PathBuf::from(std::ffi::OsString::from_wide(
            &path[..length],
        )))
    }

    let Some(desktop) = shell_folder(CSIDL_DESKTOPDIRECTORY) else {
        return;
    };
    let Some(start_menu) = shell_folder(CSIDL_PROGRAMS) else {
        return;
    };
    let Ok(target) = std::env::current_exe() else {
        return;
    };
    let Some(working_directory) = target.parent() else {
        return;
    };
    let icon = working_directory.join("BinhuWin10.ico");
    let icon = if icon.is_file() { icon } else { target.clone() };
    if unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED) }.is_err() {
        return;
    }
    let desktop_written = write_shortcut(
        &desktop.join("滨湖智慧平台.lnk"),
        &target,
        working_directory,
        &icon,
    );
    let start_menu_written = write_shortcut(
        &start_menu.join("滨湖智慧平台.lnk"),
        &target,
        working_directory,
        &icon,
    );
    if desktop_written {
        let _ = fs::remove_file(desktop.join("BinhuDesktop.lnk"));
    }
    if start_menu_written {
        let _ = fs::remove_file(start_menu.join("BinhuDesktop.lnk"));
    }
    unsafe { CoUninitialize() };
}

#[cfg(not(windows))]
pub fn ensure_windows_shortcuts() {}

#[cfg(windows)]
pub fn remove_windows_shortcuts() {
    use std::os::windows::ffi::OsStringExt;
    use windows::Win32::UI::Shell::{SHGetFolderPathW, CSIDL_DESKTOPDIRECTORY, CSIDL_PROGRAMS};

    let shell_folder = |csidl: u32| {
        let mut path = [0u16; 260];
        unsafe { SHGetFolderPathW(None, csidl as i32, None, 0, &mut path) }.ok()?;
        let length = path.iter().position(|value| *value == 0)?;
        Some(std::path::PathBuf::from(std::ffi::OsString::from_wide(
            &path[..length],
        )))
    };
    let locations = [
        shell_folder(CSIDL_DESKTOPDIRECTORY),
        shell_folder(CSIDL_PROGRAMS),
    ];
    for location in locations {
        let Some(location) = location else { continue };
        let _ = std::fs::remove_file(location.join("滨湖智慧平台.lnk"));
        let _ = std::fs::remove_file(location.join("BinhuDesktop.lnk"));
    }
}

#[cfg(not(windows))]
pub fn remove_windows_shortcuts() {}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopUpdateState {
    state: String,
    current_version: String,
    available_version: Option<String>,
    progress: Option<i16>,
    mandatory: bool,
    error: Option<String>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopUpgradeInfo {
    current_version: String,
    upgraded_from: Option<String>,
    upgrade_detected: bool,
}

#[derive(Default, serde::Deserialize, serde::Serialize)]
struct UpgradeStateFile {
    last_started_version: Option<String>,
    pending_from: Option<String>,
}

impl Default for DesktopUpdateState {
    fn default() -> Self {
        Self {
            state: "idle".into(),
            current_version: env!("CARGO_PKG_VERSION").into(),
            available_version: None,
            progress: None,
            mandatory: false,
            error: None,
        }
    }
}

#[derive(Clone)]
enum PendingUpdate {
    Remote(UpdateInfo),
    Downloaded(VelopackAsset),
}

struct UpdateRuntime {
    state: DesktopUpdateState,
    manager: Option<UpdateManager>,
    pending: Option<PendingUpdate>,
    upgrade_info: DesktopUpgradeInfo,
}

struct UpdateRuntimeState(Mutex<UpdateRuntime>);

const DEFAULT_LOCAL_MAC: &str = "02:00:00:00:00:01";

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ResidenceProbeRequest {
    base_url: String,
    username: String,
    password: String,
    mac: String,
    timeout_seconds: u64,
    community_code: Option<String>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct ResidenceProbeResult {
    status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    organization_code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error_code: Option<String>,
}

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ResidenceApiRequest {
    base_url: String,
    path: String,
    method: String,
    headers: Option<std::collections::HashMap<String, String>>,
    body: Option<String>,
    timeout_seconds: u64,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct ResidenceApiResponse {
    status_code: u16,
    payload: serde_json::Value,
}

fn residence_api_path_allowed(method: &str, path: &str) -> bool {
    (method == "GET"
        && path.starts_with("/sys/randomImage/")
        && !path[17..].is_empty()
        && path[17..].chars().all(|c| c.is_ascii_digit()))
        || (method == "POST"
            && matches!(
                path,
                "/sys/login" | "/szjzz/searchIsck" | "/szjzz/searchzzrk"
            ))
}

fn request_residence_api_sync(
    request: ResidenceApiRequest,
) -> Result<ResidenceApiResponse, String> {
    let method = request.method.trim().to_ascii_uppercase();
    if !matches!(method.as_str(), "GET" | "POST")
        || !request.path.starts_with('/')
        || request.path.contains("..")
        || request.path.contains('?')
        || request.path.contains('#')
        || !residence_api_path_allowed(&method, &request.path)
    {
        return Err("config_error".into());
    }
    let base = request.base_url.trim().trim_end_matches('/');
    let parsed = match base.parse::<ureq::http::Uri>() {
        Ok(value)
            if matches!(value.scheme_str(), Some("http") | Some("https"))
                && value.authority().is_some()
                && !value.path().contains("..")
                && value.query().is_none() =>
        {
            value
        }
        _ => return Err("config_error".into()),
    };
    if request
        .body
        .as_ref()
        .map(|body| body.len() > 256 * 1024)
        .unwrap_or(false)
    {
        return Err("config_error".into());
    }
    let origin = format!(
        "{}://{}",
        parsed.scheme_str().unwrap(),
        parsed.authority().unwrap()
    );
    let prefix = parsed.path().trim_end_matches('/');
    let url = format!("{}{}{}", origin, prefix, request.path);
    let timeout = Duration::from_secs(request.timeout_seconds.clamp(1, 120));
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .tls_config(
            ureq::tls::TlsConfig::builder()
                .disable_verification(true)
                .build(),
        )
        .build()
        .into();
    let response = if method == "GET" {
        let mut call = agent.get(&url);
        if let Some(headers) = request.headers.as_ref() {
            for (key, value) in headers {
                let normalized = key.to_ascii_lowercase();
                if !matches!(
                    normalized.as_str(),
                    "content-type" | "x-access-token" | "tenant_id" | "accept"
                ) || value.len() > 512
                {
                    return Err("config_error".into());
                }
                call = call.header(key, value);
            }
        }
        call.call()
    } else {
        let mut call = agent.post(&url);
        if let Some(headers) = request.headers.as_ref() {
            for (key, value) in headers {
                let normalized = key.to_ascii_lowercase();
                if !matches!(
                    normalized.as_str(),
                    "content-type" | "x-access-token" | "tenant_id" | "accept"
                ) || value.len() > 512
                {
                    return Err("config_error".into());
                }
                call = call.header(key, value);
            }
        }
        call.send(request.body.unwrap_or_default())
    }
    .map_err(|_| "network_error".to_string())?;
    let status = response.status().as_u16();
    let body = response
        .into_body()
        .with_config()
        .limit(1024 * 1024)
        .read_to_string()
        .map_err(|_| "network_error".to_string())?;
    let payload = serde_json::from_str(&body).map_err(|_| "invalid_response".to_string())?;
    Ok(ResidenceApiResponse {
        status_code: status,
        payload,
    })
}

#[tauri::command]
async fn request_residence_api(
    request: ResidenceApiRequest,
) -> Result<ResidenceApiResponse, String> {
    tauri::async_runtime::spawn_blocking(move || request_residence_api_sync(request))
        .await
        .map_err(|_| "居住证请求任务异常结束".to_string())?
}

fn probe_residence_result(
    status: &str,
    error_code: Option<&str>,
    organization_code: Option<String>,
) -> ResidenceProbeResult {
    ResidenceProbeResult {
        status: status.to_string(),
        organization_code,
        error_code: error_code.map(str::to_string),
    }
}

fn residence_org_matches(expected: &str, actual: &str) -> bool {
    let left = expected.trim().to_ascii_uppercase();
    let right = actual.trim().to_ascii_uppercase();
    left.is_empty()
        || right.is_empty()
        || left == right
        || (left.len() >= 6 && right.starts_with(&left))
        || (right.len() >= 6 && left.starts_with(&right))
}

fn probe_residence_login_sync(request: ResidenceProbeRequest) -> ResidenceProbeResult {
    let base = request.base_url.trim().trim_end_matches('/');
    let parsed = match base.parse::<ureq::http::Uri>() {
        Ok(value)
            if matches!(value.scheme_str(), Some("http") | Some("https"))
                && value.authority().is_some()
                && (value.path().is_empty() || value.path().starts_with('/'))
                && !value.path().contains("..")
                && value.query().is_none() =>
        {
            value
        }
        _ => return probe_residence_result("config_error", Some("invalid_base_url"), None),
    };
    if request.username.trim().is_empty() || request.password.is_empty() {
        return probe_residence_result("config_error", Some("missing_credentials"), None);
    }
    let mac = match normalize_local_mac(&request.mac) {
        Ok(value) => value,
        Err(_) => return probe_residence_result("config_error", Some("invalid_mac"), None),
    };
    let timeout = Duration::from_secs(request.timeout_seconds.clamp(1, 120));
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .tls_config(
            ureq::tls::TlsConfig::builder()
                .disable_verification(true)
                .build(),
        )
        .build()
        .into();
    let origin = format!(
        "{}://{}",
        parsed.scheme_str().unwrap(),
        parsed.authority().unwrap()
    );
    let prefix = parsed.path().trim_end_matches('/');
    let check_key = format!(
        "{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
    );
    let captcha_url = format!("{}{}/sys/randomImage/{}", origin, prefix, check_key);
    let mut captcha = match agent.get(&captcha_url).call() {
        Ok(value) => value,
        Err(_) => {
            return probe_residence_result("network_error", Some("captcha_request_failed"), None)
        }
    };
    if !captcha.status().is_success() {
        return probe_residence_result("rejected", Some("captcha_http_error"), None);
    }
    let captcha_body = match captcha
        .body_mut()
        .with_config()
        .limit(1024 * 1024)
        .read_to_string()
    {
        Ok(value) => value,
        Err(_) => {
            return probe_residence_result("network_error", Some("captcha_response_failed"), None)
        }
    };
    let captcha_payload: serde_json::Value = match serde_json::from_str(&captcha_body) {
        Ok(value) => value,
        Err(_) => return probe_residence_result("network_error", Some("invalid_response"), None),
    };
    if captcha_payload.get("success") != Some(&serde_json::Value::Bool(true)) {
        return probe_residence_result("rejected", Some("captcha_rejected"), None);
    }
    let login_url = format!("{}{}/sys/login", origin, prefix);
    let login_body = serde_json::json!({
        "username": request.username.trim(), "password": request.password, "mac": mac,
        "remember_me": true, "captcha": "", "checkKey": check_key, "terminalType": 1,
    })
    .to_string();
    let mut login = match agent
        .post(&login_url)
        .header("Content-Type", "application/json;charset=UTF-8")
        .send(login_body)
    {
        Ok(value) => value,
        Err(_) => {
            return probe_residence_result("network_error", Some("login_request_failed"), None)
        }
    };
    if !login.status().is_success() {
        return probe_residence_result("rejected", Some("login_http_error"), None);
    }
    let login_body = match login
        .body_mut()
        .with_config()
        .limit(1024 * 1024)
        .read_to_string()
    {
        Ok(value) => value,
        Err(_) => {
            return probe_residence_result("network_error", Some("login_response_failed"), None)
        }
    };
    let payload: serde_json::Value = match serde_json::from_str(&login_body) {
        Ok(value) => value,
        Err(_) => return probe_residence_result("network_error", Some("invalid_response"), None),
    };
    let result = payload.get("result");
    let token = result
        .and_then(|value| value.get("token"))
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    if payload.get("success") != Some(&serde_json::Value::Bool(true)) || token.is_empty() {
        return probe_residence_result("rejected", Some("login_rejected"), None);
    }
    let organization_code = result
        .and_then(|value| value.get("orgCode").or_else(|| value.get("org_code")))
        .and_then(serde_json::Value::as_str)
        .or_else(|| {
            result
                .and_then(|value| value.get("userInfo"))
                .and_then(|value| value.get("orgCode"))
                .and_then(serde_json::Value::as_str)
        })
        .unwrap_or("")
        .trim()
        .to_string();
    if !residence_org_matches(
        request.community_code.as_deref().unwrap_or(""),
        &organization_code,
    ) {
        return probe_residence_result(
            "rejected",
            Some("organization_mismatch"),
            Some(organization_code),
        );
    }
    probe_residence_result("allowed", None, Some(organization_code))
}

#[tauri::command]
async fn probe_residence_login(
    request: ResidenceProbeRequest,
) -> Result<ResidenceProbeResult, String> {
    tauri::async_runtime::spawn_blocking(move || probe_residence_login_sync(request))
        .await
        .map_err(|_| "居住证探测任务异常结束".to_string())
}

#[derive(Clone)]
struct LocalMacRuntimeState {
    mac: Arc<Mutex<String>>,
    state_path: Arc<Mutex<Option<PathBuf>>>,
    server_ready: Arc<AtomicBool>,
}

impl Default for LocalMacRuntimeState {
    fn default() -> Self {
        Self {
            mac: Arc::new(Mutex::new(DEFAULT_LOCAL_MAC.into())),
            state_path: Arc::new(Mutex::new(None)),
            server_ready: Arc::new(AtomicBool::new(false)),
        }
    }
}

fn normalize_local_mac(value: &str) -> Result<String, String> {
    let compact: String = value
        .chars()
        .filter(|character| !matches!(character, '.' | '-' | ':' | ' ' | '\t' | '\r' | '\n'))
        .flat_map(char::to_uppercase)
        .collect();
    if compact.len() != 12
        || !compact
            .chars()
            .all(|character| character.is_ascii_hexdigit())
    {
        return Err("MAC 地址必须是 12 位十六进制字符".into());
    }
    let first = u8::from_str_radix(&compact[..2], 16).map_err(|_| "MAC 地址格式无效")?;
    if compact == "000000000000" || first & 1 == 1 {
        return Err("MAC 地址必须是有效的单播地址".into());
    }
    Ok((0..6)
        .map(|index| compact[index * 2..index * 2 + 2].to_string())
        .collect::<Vec<_>>()
        .join(":"))
}

fn detect_hardware_mac() -> String {
    let output = Command::new("getmac").args(["/fo", "csv", "/nh"]).output();
    if let Ok(output) = output {
        let text = String::from_utf8_lossy(&output.stdout);
        for token in text.split(|character: char| {
            character == '"' || character == ',' || character.is_whitespace()
        }) {
            if let Ok(mac) = normalize_local_mac(token) {
                return mac;
            }
        }
    }
    DEFAULT_LOCAL_MAC.into()
}

fn persist_local_mac(path: &PathBuf, mac: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|_| "无法创建本机 MAC 配置目录")?;
    }
    let temporary = path.with_extension("partial");
    fs::write(&temporary, format!("{}\n", mac)).map_err(|_| "无法保存本机 MAC")?;
    if path.exists() {
        fs::remove_file(path).map_err(|_| "无法替换本机 MAC")?;
    }
    fs::rename(&temporary, path).map_err(|_| "无法保存本机 MAC".to_string())
}

impl LocalMacRuntimeState {
    fn initialize(&self, path: PathBuf) {
        let stored = fs::read_to_string(&path)
            .ok()
            .and_then(|value| normalize_local_mac(&value).ok());
        let needs_persist = stored.is_none();
        let loaded = stored.unwrap_or_else(detect_hardware_mac);
        if needs_persist {
            let _ = persist_local_mac(&path, &loaded);
        }
        *self
            .mac
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = loaded;
        *self
            .state_path
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(path);
    }

    fn start(&self) {
        let mac = self.mac.clone();
        let ready = self.server_ready.clone();
        thread::spawn(move || {
            let Ok(listener) = TcpListener::bind(("127.0.0.1", 23333)) else {
                return;
            };
            ready.store(true, Ordering::Release);
            for incoming in listener.incoming() {
                let Ok(mut stream) = incoming else { continue };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
                let mut buffer = [0u8; 8192];
                let Ok(length) = stream.read(&mut buffer) else {
                    continue;
                };
                let request = String::from_utf8_lossy(&buffer[..length]);
                let first_line = request.lines().next().unwrap_or("");
                let parts: Vec<_> = first_line.split_whitespace().collect();
                let (method, path) = if parts.len() == 3 {
                    (parts[0], parts[1])
                } else {
                    ("", "")
                };
                let (status, body) = match (method, path) {
                    ("OPTIONS", "/") | ("OPTIONS", "/health") => ("204 No Content", String::new()),
                    ("GET", "/") => {
                        let current = mac
                            .lock()
                            .unwrap_or_else(|poisoned| poisoned.into_inner())
                            .clone();
                        ("200 OK", format!("{{\"mac\":\"{}\"}}", current))
                    }
                    ("GET", "/health") => ("200 OK", "{\"status\":\"ok\"}".into()),
                    (_, "/") | (_, "/health") => (
                        "405 Method Not Allowed",
                        "{\"error\":\"method_not_allowed\"}".into(),
                    ),
                    _ => ("404 Not Found", "{\"error\":\"not_found\"}".into()),
                };
                let response = format!(
                    "HTTP/1.1 {}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, OPTIONS\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n{}",
                    status,
                    body.as_bytes().len(),
                    body,
                );
                let _ = stream.write_all(response.as_bytes());
            }
            ready.store(false, Ordering::Release);
        });
    }
}

#[tauri::command]
fn get_local_mac(state: State<'_, LocalMacRuntimeState>) -> Result<String, String> {
    if !state.server_ready.load(Ordering::Acquire) {
        return Err("本机 23333 端口未能启动，请检查端口占用".into());
    }
    Ok(state
        .mac
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone())
}

#[tauri::command]
fn set_local_mac(mac: String, state: State<'_, LocalMacRuntimeState>) -> Result<String, String> {
    if !state.server_ready.load(Ordering::Acquire) {
        return Err("本机 23333 端口未能启动，请检查端口占用".into());
    }
    let normalized = normalize_local_mac(&mac)?;
    let path = state
        .state_path
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone()
        .ok_or_else(|| "本机 MAC 配置目录尚未就绪".to_string())?;
    persist_local_mac(&path, &normalized)?;
    *state
        .mac
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner()) = normalized.clone();
    Ok(normalized)
}

impl Default for UpdateRuntimeState {
    fn default() -> Self {
        Self(Mutex::new(UpdateRuntime {
            state: DesktopUpdateState::default(),
            manager: None,
            pending: None,
            upgrade_info: DesktopUpgradeInfo {
                current_version: env!("CARGO_PKG_VERSION").into(),
                upgraded_from: None,
                upgrade_detected: false,
            },
        }))
    }
}

fn upgrade_state_path(app: &tauri::AppHandle) -> Option<PathBuf> {
    app.path()
        .app_data_dir()
        .ok()
        .map(|root| root.join("upgrade-state.json"))
}

fn write_upgrade_state(app: &tauri::AppHandle, state: &UpgradeStateFile) {
    let Some(path) = upgrade_state_path(app) else {
        return;
    };
    let temporary = path.with_extension("json.partial");
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(content) = serde_json::to_vec(state) {
        if fs::write(&temporary, content).is_ok() {
            let _ = fs::rename(temporary, path);
        }
    }
}

fn read_upgrade_state(app: &tauri::AppHandle) -> UpgradeStateFile {
    upgrade_state_path(app)
        .and_then(|path| fs::read_to_string(path).ok())
        .and_then(|content| serde_json::from_str(&content).ok())
        .unwrap_or_default()
}

fn initialize_upgrade_info(app: &tauri::AppHandle, restarted: bool) {
    let current = env!("CARGO_PKG_VERSION").to_string();
    let state = read_upgrade_state(app);
    let restarted_marker =
        restarted || state.pending_from.as_deref() == Some("__velopack_restarted__");
    let upgraded_from = state
        .pending_from
        .filter(|value| value != &current && value != "__velopack_restarted__")
        .or_else(|| state.last_started_version.filter(|value| value != &current));
    let upgrade_detected = upgraded_from.is_some() || restarted_marker;
    write_upgrade_state(
        app,
        &UpgradeStateFile {
            last_started_version: Some(current.clone()),
            pending_from: upgraded_from
                .clone()
                .or_else(|| restarted_marker.then(|| "__velopack_restarted__".to_string())),
        },
    );
    let managed = app.state::<UpdateRuntimeState>();
    if let Ok(mut runtime) = managed.0.lock() {
        runtime.upgrade_info = DesktopUpgradeInfo {
            current_version: current,
            upgraded_from,
            upgrade_detected,
        };
    };
}

fn mark_pending_upgrade(app: &tauri::AppHandle) {
    let current = env!("CARGO_PKG_VERSION").to_string();
    write_upgrade_state(
        app,
        &UpgradeStateFile {
            last_started_version: Some(current.clone()),
            pending_from: Some(current),
        },
    );
}

fn create_update_manager() -> Result<UpdateManager, String> {
    let source = HttpSource::new(UPDATE_URL);
    let options = UpdateOptions {
        AllowVersionDowngrade: false,
        ExplicitChannel: Some("stable".into()),
        MaximumDeltasBeforeFallback: 10,
    };
    UpdateManager::new(source, Some(options), None).map_err(|error| error.to_string())
}

fn version_parts(value: &str) -> Option<[u64; 3]> {
    let parts = value
        .split('.')
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    (parts.len() == 3).then(|| [parts[0], parts[1], parts[2]])
}

fn mandatory_update_required() -> bool {
    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase")]
    struct UpdatePolicy {
        minimum_version: String,
    }

    let response = match ureq::get(POLICY_URL).call() {
        Ok(response) => response,
        Err(_) => return false,
    };
    let mut body = response.into_body();
    let text = match body.read_to_string() {
        Ok(text) => text,
        Err(_) => return false,
    };
    let policy: UpdatePolicy = match serde_json::from_str(&text) {
        Ok(policy) => policy,
        Err(_) => return false,
    };
    match (
        version_parts(env!("CARGO_PKG_VERSION")),
        version_parts(&policy.minimum_version),
    ) {
        (Some(current), Some(minimum)) => current < minimum,
        _ => false,
    }
}

fn update_state<F>(app: &tauri::AppHandle, change: F) -> DesktopUpdateState
where
    F: FnOnce(&mut UpdateRuntime),
{
    let managed = app.state::<UpdateRuntimeState>();
    let snapshot = match managed.0.lock() {
        Ok(mut runtime) => {
            change(&mut runtime);
            runtime.state.clone()
        }
        Err(poisoned) => {
            let mut runtime = poisoned.into_inner();
            runtime.state.state = "error".into();
            runtime.state.error = Some("更新状态存储不可用。".into());
            runtime.state.clone()
        }
    };
    app.emit("desktop:update-state", &snapshot).ok();
    snapshot
}

fn current_update_state(app: &tauri::AppHandle) -> DesktopUpdateState {
    let managed = app.state::<UpdateRuntimeState>();
    let snapshot = match managed.0.lock() {
        Ok(runtime) => runtime.state.clone(),
        Err(poisoned) => poisoned.into_inner().state.clone(),
    };
    snapshot
}

async fn perform_update_check(app: tauri::AppHandle) -> DesktopUpdateState {
    update_state(&app, |runtime| {
        runtime.state.state = "checking".into();
        runtime.state.progress = None;
        runtime.state.error = None;
    });

    let result = tauri::async_runtime::spawn_blocking(|| {
        let mandatory = mandatory_update_required();
        let update_result: Result<
            (UpdateManager, Option<UpdateInfo>, Option<VelopackAsset>),
            String,
        > = (|| {
            let manager = create_update_manager()?;
            if let Some(pending) = manager.get_update_pending_restart() {
                return Ok((manager, None, Some(pending)));
            }
            match manager
                .check_for_updates()
                .map_err(|error| error.to_string())?
            {
                UpdateCheck::UpdateAvailable(update) => Ok((manager, Some(*update), None)),
                UpdateCheck::NoUpdateAvailable | UpdateCheck::RemoteIsEmpty => {
                    Ok((manager, None, None))
                }
            }
        })();
        (mandatory, update_result)
    })
    .await;

    match result {
        Ok((mandatory, Ok((manager, update, downloaded)))) => update_state(&app, |runtime| {
            runtime.manager = Some(manager);
            runtime.state.mandatory = mandatory;
            if let Some(asset) = downloaded {
                runtime.state.state = "ready".into();
                runtime.state.available_version = Some(asset.Version.clone());
                runtime.state.progress = Some(100);
                runtime.pending = Some(PendingUpdate::Downloaded(asset));
            } else if let Some(update) = update {
                runtime.state.state = "available".into();
                runtime.state.available_version = Some(update.TargetFullRelease.Version.clone());
                runtime.state.progress = None;
                runtime.pending = Some(PendingUpdate::Remote(update));
            } else {
                runtime.state.state = "idle".into();
                runtime.state.available_version = None;
                runtime.state.progress = None;
                runtime.pending = None;
            }
        }),
        Ok((mandatory, Err(error))) => update_state(&app, |runtime| {
            runtime.state.state = "error".into();
            runtime.state.mandatory = mandatory;
            runtime.state.error = Some(error);
        }),
        Err(error) => update_state(&app, |runtime| {
            runtime.state.state = "error".into();
            runtime.state.error = Some(error.to_string());
        }),
    }
}

#[tauri::command]
fn desktop_config() -> Result<serde_json::Value, String> {
    serde_json::from_str(DESKTOP_CONFIG).map_err(|error| error.to_string())
}

fn navigate_main(app: &tauri::AppHandle, route: &str) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "主窗口尚未创建".to_string())?;
    let script = format!(
        "window.history.pushState({{}}, '', '{}'); window.dispatchEvent(new PopStateEvent('popstate'));",
        route
    );
    window.eval(script).map_err(|error| error.to_string())?;
    window.show().map_err(|error| error.to_string())?;
    window.set_focus().map_err(|error| error.to_string())?;
    Ok(())
}

#[tauri::command]
fn open_online(app: tauri::AppHandle) -> Result<(), String> {
    navigate_main(&app, "/login")
}

#[tauri::command]
fn open_offline(app: tauri::AppHandle) -> Result<(), String> {
    navigate_main(&app, "/offline")
}

#[tauri::command]
fn window_minimize(window: tauri::WebviewWindow) -> Result<(), String> {
    window.minimize().map_err(|error| error.to_string())
}

#[tauri::command]
fn window_toggle_maximize(window: tauri::WebviewWindow) -> Result<bool, String> {
    if window.is_maximized().map_err(|error| error.to_string())? {
        window.unmaximize().map_err(|error| error.to_string())?;
    } else {
        window.maximize().map_err(|error| error.to_string())?;
    }
    window.is_maximized().map_err(|error| error.to_string())
}

#[tauri::command]
fn window_is_maximized(window: tauri::WebviewWindow) -> Result<bool, String> {
    window.is_maximized().map_err(|error| error.to_string())
}

#[tauri::command]
fn window_close(window: tauri::WebviewWindow) {
    window.close().ok();
}

#[tauri::command]
fn save_file(
    app: tauri::AppHandle,
    window: tauri::WebviewWindow,
    filename: String,
    data: Vec<u8>,
) -> Result<bool, String> {
    let safe_name = PathBuf::from(filename)
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("下载文件")
        .replace(['\\', '/', ':', '*', '?', '"', '<', '>', '|'], "_");
    let safe_name = if safe_name.is_empty() {
        "下载文件".to_string()
    } else {
        safe_name
    };
    let extension = PathBuf::from(&safe_name)
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_string);
    let mut dialog = rfd::FileDialog::new()
        .set_title("保存导出文件")
        .set_file_name(&safe_name)
        .set_parent(&window);
    if let Ok(directory) = app.path().download_dir() {
        dialog = dialog.set_directory(directory);
    }
    if let Some(extension) = extension.as_deref() {
        dialog = dialog.add_filter(extension.to_uppercase(), &[extension]);
    }
    let Some(target) = dialog.save_file() else {
        return Ok(false);
    };
    fs::write(target, data).map_err(|error| error.to_string())?;
    Ok(true)
}

#[tauri::command]
fn get_update_status(app: tauri::AppHandle) -> DesktopUpdateState {
    current_update_state(&app)
}

#[tauri::command]
async fn check_for_updates(app: tauri::AppHandle) -> DesktopUpdateState {
    perform_update_check(app).await
}

#[tauri::command]
async fn download_update(app: tauri::AppHandle) -> DesktopUpdateState {
    fn select_remote_update(app: &tauri::AppHandle) -> Option<(UpdateManager, UpdateInfo)> {
        let managed = app.state::<UpdateRuntimeState>();
        let runtime = match managed.0.lock() {
            Ok(runtime) => runtime,
            Err(poisoned) => poisoned.into_inner(),
        };
        match (runtime.manager.clone(), runtime.pending.clone()) {
            (Some(manager), Some(PendingUpdate::Remote(update))) => Some((manager, update)),
            _ => None,
        }
    }

    let mut selection = select_remote_update(&app);
    if selection.is_none() {
        let checked = perform_update_check(app.clone()).await;
        if checked.state == "ready" || checked.state == "error" || checked.state == "idle" {
            return checked;
        }
        selection = select_remote_update(&app);
    }
    let Some((manager, update)) = selection else {
        return current_update_state(&app);
    };

    update_state(&app, |runtime| {
        runtime.state.state = "downloading".into();
        runtime.state.progress = Some(0);
        runtime.state.error = None;
    });

    let progress_app = app.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        let (sender, receiver) = mpsc::channel();
        let progress_thread = thread::spawn(move || {
            for progress in receiver {
                update_state(&progress_app, |runtime| {
                    runtime.state.state = "downloading".into();
                    runtime.state.progress = Some(progress);
                });
            }
        });
        let download_result = manager
            .download_updates(&update, Some(sender))
            .map_err(|error| error.to_string());
        progress_thread.join().ok();
        download_result
    })
    .await;

    match result {
        Ok(Ok(())) => update_state(&app, |runtime| {
            runtime.state.state = "ready".into();
            runtime.state.progress = Some(100);
        }),
        Ok(Err(error)) => update_state(&app, |runtime| {
            runtime.state.state = "error".into();
            runtime.state.error = Some(error);
        }),
        Err(error) => update_state(&app, |runtime| {
            runtime.state.state = "error".into();
            runtime.state.error = Some(error.to_string());
        }),
    }
}

#[tauri::command]
async fn restart_and_apply(app: tauri::AppHandle) -> DesktopUpdateState {
    let (manager, pending) = {
        let managed = app.state::<UpdateRuntimeState>();
        let runtime = match managed.0.lock() {
            Ok(runtime) => runtime,
            Err(poisoned) => poisoned.into_inner(),
        };
        match (runtime.manager.clone(), runtime.pending.clone()) {
            (Some(manager), Some(pending)) if runtime.state.state == "ready" => (manager, pending),
            _ => {
                drop(runtime);
                return update_state(&app, |runtime| {
                    runtime.state.state = "error".into();
                    runtime.state.error = Some("更新尚未下载完成。".into());
                });
            }
        }
    };

    update_state(&app, |runtime| {
        runtime.state.state = "applying".into();
        runtime.state.error = None;
    });
    mark_pending_upgrade(&app);
    let result = tauri::async_runtime::spawn_blocking(move || match pending {
        PendingUpdate::Remote(update) => {
            manager.wait_exit_then_apply_updates(update, false, true, Vec::<String>::new())
        }
        PendingUpdate::Downloaded(asset) => {
            manager.wait_exit_then_apply_updates(asset, false, true, Vec::<String>::new())
        }
    })
    .await;

    match result {
        Ok(Ok(())) => app.exit(0),
        Ok(Err(error)) => {
            return update_state(&app, |runtime| {
                runtime.state.state = "error".into();
                runtime.state.error = Some(error.to_string());
            });
        }
        Err(error) => {
            return update_state(&app, |runtime| {
                runtime.state.state = "error".into();
                runtime.state.error = Some(error.to_string());
            });
        }
    }
    current_update_state(&app)
}

#[tauri::command]
fn get_upgrade_info(app: tauri::AppHandle) -> DesktopUpgradeInfo {
    let managed = app.state::<UpdateRuntimeState>();
    let info = match managed.0.lock() {
        Ok(runtime) => runtime.upgrade_info.clone(),
        Err(poisoned) => poisoned.into_inner().upgrade_info.clone(),
    };
    info
}

#[tauri::command]
fn acknowledge_upgrade(app: tauri::AppHandle) -> DesktopUpgradeInfo {
    let current = env!("CARGO_PKG_VERSION").to_string();
    write_upgrade_state(
        &app,
        &UpgradeStateFile {
            last_started_version: Some(current.clone()),
            pending_from: None,
        },
    );
    let managed = app.state::<UpdateRuntimeState>();
    let info = match managed.0.lock() {
        Ok(mut runtime) => {
            runtime.upgrade_info = DesktopUpgradeInfo {
                current_version: current,
                upgraded_from: None,
                upgrade_detected: false,
            };
            runtime.upgrade_info.clone()
        }
        Err(poisoned) => poisoned.into_inner().upgrade_info.clone(),
    };
    info
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run(restarted: bool) {
    tauri::Builder::default()
        .manage(UpdateRuntimeState::default())
        .manage(LocalMacRuntimeState::default())
        .setup(move |app| {
            let handle = app.handle().clone();
            initialize_upgrade_info(&handle, restarted);
            let local_mac = app.state::<LocalMacRuntimeState>().inner().clone();
            if let Ok(directory) = app.path().app_data_dir() {
                local_mac.initialize(directory.join("mac-address"));
                local_mac.start();
            }
            thread::spawn(move || loop {
                tauri::async_runtime::block_on(perform_update_check(handle.clone()));
                thread::sleep(CHECK_INTERVAL);
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            desktop_config,
            open_online,
            open_offline,
            window_minimize,
            window_toggle_maximize,
            window_is_maximized,
            window_close,
            save_file,
            get_update_status,
            get_upgrade_info,
            acknowledge_upgrade,
            check_for_updates,
            download_update,
            restart_and_apply,
            get_local_mac,
            set_local_mac,
            probe_residence_login,
            request_residence_api
        ])
        .run(tauri::generate_context!())
        .expect("error while running Binhu Tauri application");
}
