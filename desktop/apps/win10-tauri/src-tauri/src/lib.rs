use std::{
    fs,
    path::PathBuf,
    sync::{mpsc, Mutex},
    thread,
    time::Duration,
};

use serde::Serialize;
use tauri::{Emitter, Manager};
use velopack::{
    sources::HttpSource, UpdateCheck, UpdateInfo, UpdateManager, UpdateOptions, VelopackAsset,
};

const DESKTOP_CONFIG: &str = include_str!("../../../../config/desktop.config.json");
const UPDATE_URL: &str = "https://47.100.44.36/updates/win10-x64/";
const POLICY_URL: &str = "https://47.100.44.36/updates/win10-x64/policy.stable.json";
const CHECK_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);

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
        .setup(move |app| {
            let handle = app.handle().clone();
            initialize_upgrade_info(&handle, restarted);
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
            get_update_status,
            get_upgrade_info,
            acknowledge_upgrade,
            check_for_updates,
            download_update,
            restart_and_apply
        ])
        .run(tauri::generate_context!())
        .expect("error while running Binhu Tauri application");
}
