mod engine;
mod mic;
mod recorder;

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use engine::{Engine, Info};
use mic::{Backend, Capture, Choice, Source};
use recorder::Recorder;
use serde::Serialize;
use serde_json::{json, Value};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, RunEvent, State, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

/// Popup sizes from the UX plan, in logical pixels: the bar, the bar folded to its top line, the picker card.
const POPUP_WIDTH: f64 = 360.0;
const POPUP_BAR: f64 = 64.0;
const POPUP_EDGE: i32 = 16;

/// Everything the shell itself holds: the recorder, the Settings meters, the shortcuts the engine asked for.
struct Shell {
    backend: Arc<dyn Backend>,
    recorder: Mutex<Option<Recorder>>,
    preview: Mutex<Option<[Capture; 2]>>,
    shortcuts: Mutex<HashMap<u32, String>>,
    mic: Mutex<Choice>,
}

impl Default for Shell {
    fn default() -> Self {
        Shell { backend: Arc::new(mic::Cpal::default()), recorder: Mutex::default(), preview: Mutex::default(),
            shortcuts: Mutex::default(), mic: Mutex::default() }
    }
}

/// Port and token of the engine, waiting while it starts. The one way the UI learns how to reach it.
#[tauri::command]
async fn engine_info(engine: State<'_, Engine>) -> Result<Info, ()> {
    let engine = engine.inner().clone();
    Ok(tauri::async_runtime::spawn_blocking(move || engine.info()).await.unwrap_or(Info::Stopped {
        error: "The shell could not wait for the engine.".into(),
    }))
}

#[tauri::command]
async fn restart_engine(app: AppHandle, engine: State<'_, Engine>) -> Result<Info, ()> {
    let engine = engine.inner().clone();
    Ok(tauri::async_runtime::spawn_blocking(move || {
        engine.stop();
        start_engine(&app, &engine);
        engine.info()
    })
    .await
    .unwrap_or(Info::Stopped { error: "The shell could not restart the engine.".into() }))
}

/// The recording in progress: time, marks, and both tracks' devices and levels. Polled by the popup.
#[tauri::command]
fn recorder_live(shell: State<'_, Shell>) -> Option<recorder::Live> {
    shell.recorder.lock().unwrap().as_ref().map(Recorder::live)
}

#[derive(Serialize)]
struct Devices {
    devices: Vec<mic::Device>,
    communications: bool,
}

#[tauri::command]
async fn mic_devices(shell: State<'_, Shell>) -> Result<Devices, ()> {
    let backend = shell.backend.clone();
    let devices = tauri::async_runtime::spawn_blocking(move || backend.microphones()).await.unwrap_or_default();
    Ok(Devices { devices, communications: mic::HAS_COMMUNICATIONS })
}

#[derive(Serialize)]
struct Meter {
    level: f32,
    status: mic::Status,
}

/// The Settings meters. A running recording is measured where it is; otherwise `mic_preview` opens streams.
#[tauri::command]
fn mic_levels(shell: State<'_, Shell>) -> Option<[Meter; 2]> {
    if let Some(live) = shell.recorder.lock().unwrap().as_ref().map(Recorder::live) {
        return Some([Meter { level: live.mic_level, status: live.mic }, Meter { level: live.system_level, status: live.system }]);
    }
    shell.preview.lock().unwrap().as_ref().map(|p| p.each_ref().map(|c| Meter { level: c.level(), status: c.status() }))
}

/// Open the meters for Settings (`Some`), or close them (`None`). Does nothing to a running recording.
#[tauri::command]
fn mic_preview(shell: State<'_, Shell>, choice: Option<Choice>) {
    let mut preview = shell.preview.lock().unwrap();
    match (choice, preview.as_ref()) {
        (None, _) => {
            if let Some(old) = preview.take() {
                std::thread::spawn(move || old.map(Capture::stop));
            }
        }
        (Some(choice), Some(running)) => running[0].choose(choice),
        (Some(choice), None) => {
            let start = Instant::now();
            *preview = Some([Capture::start(shell.backend.clone(), Source::Microphone(choice), start, None),
                Capture::start(shell.backend.clone(), Source::System, start, None)]);
        }
    }
}

/// A new pick in Settings: the meters and a running recording switch at once; the engine stores it.
#[tauri::command]
fn mic_select(shell: State<'_, Shell>, choice: Choice) {
    if let Some(rec) = shell.recorder.lock().unwrap().as_ref() {
        rec.choose_mic(choice.clone());
    }
    if let Some(p) = shell.preview.lock().unwrap().as_ref() {
        p[0].choose(choice.clone());
    }
    *shell.mic.lock().unwrap() = choice;
}

/// The popup's own size and whether it may take the keyboard: only the picker card does, and only
/// because the user opened it.
#[tauri::command]
fn popup_layout(app: AppHandle, height: f64, keyboard: bool) {
    let Some(popup) = app.get_webview_window("popup") else { return };
    let size = tauri::LogicalSize::new(POPUP_WIDTH, height);
    // GTK keeps a non-resizable window at least as tall as it was: resizable only for the change, so the bar can fold
    let _ = popup.set_resizable(true);
    let _ = popup.set_size(size);
    let _ = popup.set_resizable(false);
    keep_on_screen(&popup, height);
    let _ = popup.set_focusable(keyboard);
    if keyboard {
        let _ = popup.set_focus();
    }
}

#[tauri::command]
fn popup_hide(app: AppHandle) {
    if let Some(popup) = app.get_webview_window("popup") {
        let _ = popup.set_focusable(false);
        let _ = popup.hide();
    }
}

/// Bring the main window forward, on the meeting the popup was about.
#[tauri::command]
fn open_main(app: AppHandle, meeting: Option<String>) {
    show_main(&app);
    if let Some(meeting) = meeting {
        let _ = app.emit_to("main", "open-meeting", meeting);
    }
}

#[derive(Serialize)]
struct Platform {
    os: &'static str,
    content_protection: bool,
    shortcuts: bool,
}

#[tauri::command]
fn platform() -> Platform {
    let wayland = cfg!(target_os = "linux") && (std::env::var_os("WAYLAND_DISPLAY").is_some()
        || std::env::var("XDG_SESSION_TYPE").is_ok_and(|t| t == "wayland"));
    Platform { os: std::env::consts::OS, content_protection: !cfg!(target_os = "linux"), shortcuts: !wayland }
}

fn start_engine(app: &AppHandle, engine: &Engine) {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."); // development: this checkout
    let resources = app.path().resource_dir().ok();
    let launch = engine::launch(std::env::var_os(engine::ENGINE_ENV), resources.as_deref(), &repo, std::process::id());
    if launch.bundled {
        // an installed app cannot write next to its program files: settings live in the per-user app folder
        let home = app.path().app_local_data_dir().ok();
        if let Some(dir) = &home {
            let _ = std::fs::create_dir_all(dir);
        }
        engine.start(launch, home.clone(), home);
    } else {
        engine.start(launch, repo.canonicalize().ok(), None);
    }
}

fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Right edge of the screen, vertically centred, 16 px in: clear of the call buttons Meet and Zoom keep
/// along the bottom and Teams along the top.
fn place_popup(popup: &tauri::WebviewWindow, height: f64) {
    let Ok(Some(monitor)) = popup.current_monitor().or_else(|_| popup.primary_monitor()) else { return };
    let area = monitor.work_area();
    let scale = monitor.scale_factor();
    let (w, h) = ((POPUP_WIDTH * scale) as i32, (height * scale) as i32);
    let x = area.position.x + area.size.width as i32 - w - (POPUP_EDGE as f64 * scale) as i32;
    let y = area.position.y + (area.size.height as i32 - h) / 2;
    let _ = popup.set_position(PhysicalPosition::new(x, y));
}

/// The popup keeps its top edge when it grows or folds (it must not move under the pointer); it moves up only
/// as far as needed to stay on the screen.
fn keep_on_screen(popup: &tauri::WebviewWindow, height: f64) {
    let (Ok(Some(monitor)), Ok(pos)) = (popup.current_monitor(), popup.outer_position()) else { return };
    let area = monitor.work_area();
    let bottom = area.position.y + area.size.height as i32 - (POPUP_EDGE as f64 * monitor.scale_factor()) as i32;
    let h = (height * monitor.scale_factor()) as i32;
    if pos.y + h > bottom {
        let _ = popup.set_position(PhysicalPosition::new(pos.x, (bottom - h).max(area.position.y)));
    }
}

fn quit(app: &AppHandle) {
    app.state::<Engine>().stop();
    app.exit(0);
}

/// A recorder event for the engine (it decides what follows) and the windows (they redraw).
fn report(app: &AppHandle, state: &str, data: Value) {
    app.state::<Engine>().send(&format!("recorder {state} {data}"));
    let _ = app.emit("recorder", json!({ "state": state, "data": data }));
}

/// The engine's `@@` lines. It decides; the shell carries out.
fn on_engine(app: &AppHandle, name: &str, data: Value) {
    let shell = app.state::<Shell>();
    match name {
        "ready" => {
            // a recording outlives an engine restart: tell the new engine about it
            if let Some(live) = shell.recorder.lock().unwrap().as_ref().map(Recorder::live) {
                report(app, "recording", json!({ "folder": live.folder, "seconds": live.seconds }));
            }
        }
        "config" => apply_config(app, &data),
        "popup show" => {
            let Some(popup) = app.get_webview_window("popup") else { return };
            if !popup.is_visible().unwrap_or(false) {
                let height = popup.inner_size().ok().and_then(|s| popup.scale_factor().ok().map(|f| s.height as f64 / f))
                    .unwrap_or(POPUP_BAR);
                place_popup(&popup, height);
            }
            let activate = data["activate"].as_bool().unwrap_or(false);
            let _ = popup.set_focusable(activate);
            let _ = popup.show();
            if activate {
                let _ = popup.set_focus();
            }
            let _ = app.emit_to("popup", "popup-show", data);
        }
        "popup hide" => popup_hide(app.clone()),
        "record start" => {
            let folder = PathBuf::from(data["folder"].as_str().unwrap_or_default());
            let choice = serde_json::from_value(data["mic"].clone()).unwrap_or_else(|_| shell.mic.lock().unwrap().clone());
            let mut slot = shell.recorder.lock().unwrap();
            if slot.is_some() || folder.as_os_str().is_empty() {
                drop(slot);
                return report(app, "error", json!({ "folder": folder, "message": "A recording is already running." }));
            }
            match Recorder::start(shell.backend.clone(), &folder, choice) {
                Ok(rec) => {
                    *slot = Some(rec);
                    drop(slot);
                    report(app, "recording", json!({ "folder": folder, "seconds": 0.0 }));
                }
                Err(e) => {
                    drop(slot);
                    report(app, "error", json!({ "folder": folder, "message": e.to_string() }));
                }
            }
        }
        "record stop" => {
            let Some(rec) = shell.recorder.lock().unwrap().take() else { return };
            let folder = rec.folder().to_path_buf();
            match rec.stop() {
                Ok(summary) => report(app, "stopped", serde_json::to_value(summary).unwrap_or_default()),
                Err(e) => report(app, "error", json!({ "folder": folder, "message": e.to_string() })),
            }
        }
        "record mark" => {
            let marked = shell.recorder.lock().unwrap().as_ref().map(|r| (r.folder().to_path_buf(), r.mark()));
            if let Some((folder, at)) = marked {
                report(app, "mark", json!({ "folder": folder, "at": at }));
            }
        }
        _ => {}
    }
}

/// `@@ config {"hotkeys": {action: combo}, "content_protected": bool, "mic": choice}`. Shortcuts that cannot
/// be registered go back to the engine, which shows them in Settings.
fn apply_config(app: &AppHandle, data: &Value) {
    let shell = app.state::<Shell>();
    if let Ok(choice) = serde_json::from_value::<Choice>(data["mic"].clone()) {
        *shell.mic.lock().unwrap() = choice;
    }
    if let Some(popup) = app.get_webview_window("popup") {
        let _ = popup.set_content_protected(data["content_protected"].as_bool().unwrap_or(true));
    }
    let keys = app.global_shortcut();
    let _ = keys.unregister_all();
    let mut map = shell.shortcuts.lock().unwrap();
    map.clear();
    let mut failed = serde_json::Map::new();
    for (action, combo) in data["hotkeys"].as_object().into_iter().flatten() {
        let Some(combo) = combo.as_str().filter(|c| !c.trim().is_empty()) else { continue };
        let result = combo.parse::<Shortcut>().map_err(|e| e.to_string())
            .and_then(|s| keys.register(s).map(|_| s).map_err(|e| e.to_string()));
        match result {
            Ok(shortcut) => {
                map.insert(shortcut.id(), action.clone());
            }
            Err(e) => {
                failed.insert(action.clone(), e.into());
            }
        }
    }
    drop(map);
    app.state::<Engine>().send(&format!("shortcuts {}", json!({ "failed": failed })));
}

pub fn run() {
    tauri::Builder::default()
        // first: a second launch only brings the running window forward
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new()
            .with_handler(|app, shortcut, event| {
                if event.state() == ShortcutState::Pressed {
                    let action = app.state::<Shell>().shortcuts.lock().unwrap().get(&shortcut.id()).cloned();
                    if let Some(action) = action {
                        app.state::<Engine>().send(&format!("hotkey {action}"));
                    }
                }
            })
            .build())
        .manage(Engine::default())
        .manage(Shell::default())
        .invoke_handler(tauri::generate_handler![engine_info, restart_engine, recorder_live, mic_devices, mic_levels,
            mic_preview, mic_select, popup_layout, popup_hide, open_main, platform])
        .setup(|app| {
            let handle = app.handle();
            // Created hidden, never focused on its own; it may take the keyboard only when the user opens the picker.
            WebviewWindowBuilder::new(handle, "popup", WebviewUrl::App("index.html".into()))
                .title("vault-ingest call")
                .inner_size(POPUP_WIDTH, POPUP_BAR)
                .resizable(false)
                .decorations(false)
                .always_on_top(true)
                .skip_taskbar(true)
                .visible(false)
                .focused(false)
                .focusable(false)
                .content_protected(true)
                .background_color(tauri::window::Color(0x21, 0x1c, 0x19, 0xff))
                .build()?;

            let link = handle.clone();
            app.state::<Engine>().set_handler(Arc::new(move |name, data| on_engine(&link, name, data)));
            start_engine(handle, &app.state::<Engine>());

            let open = MenuItem::with_id(handle, "open", "Open vault-ingest", true, None::<&str>)?;
            let popup = MenuItem::with_id(handle, "popup", "Show call popup", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(handle, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(handle, &[&open, &popup, &PredefinedMenuItem::separator(handle)?, &quit_item])?;
            let mut tray = TrayIconBuilder::with_id("main")
                .tooltip("vault-ingest")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" => show_main(app),
                    "popup" => app.state::<Engine>().send("tray click popup"),
                    "quit" => quit(app),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                        show_main(tray.app_handle());
                    }
                });
            if let Some(icon) = handle.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(handle)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // closing the window keeps the app in the tray, so it can keep noticing calls; Quit is in the tray menu
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                // a recording in progress is finished cleanly before the app goes
                if let Some(rec) = app.state::<Shell>().recorder.lock().unwrap().take() {
                    let _ = rec.stop();
                }
                app.state::<Engine>().stop(); // also on log-off or when the OS ends the app
            }
        });
}
