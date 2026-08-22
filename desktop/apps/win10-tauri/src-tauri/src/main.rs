#![windows_subsystem = "windows"]

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

fn main() {
    let restarted = Arc::new(AtomicBool::new(false));
    let restarted_hook = restarted.clone();
    velopack::VelopackApp::build()
        .set_auto_apply_on_startup(false)
        .on_restarted(move |_| {
            restarted_hook.store(true, Ordering::Relaxed);
        })
        .run();
    binhu_win10_tauri_lib::run(restarted.load(Ordering::Relaxed));
}
