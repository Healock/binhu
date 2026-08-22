#![windows_subsystem = "windows"]

fn main() {
    velopack::VelopackApp::build()
        .set_auto_apply_on_startup(false)
        .run();
    binhu_win10_tauri_lib::run();
}
