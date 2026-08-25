mod updater;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    updater::configure(tauri::Builder::default())
        .plugin(tauri_plugin_opener::init())
        .run(tauri::generate_context!())
        .expect("error while running Binhu Android application");
}
