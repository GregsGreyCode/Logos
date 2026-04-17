// Logos desktop client — thin Tauri wrapper around the Logos web UI.
//
// The window's `url` is baked into tauri.conf.json as a default (pointing
// at http://localhost:8091/login, which is the local dev gateway). The
// `set_gateway_url` command lets the user override it at runtime without
// rebuilding — the new URL is persisted to Tauri's app-data dir and
// restored on the next launch.

use std::fs;
use std::path::PathBuf;

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

const SETTINGS_FILE: &str = "settings.json";

#[derive(serde::Serialize, serde::Deserialize, Clone)]
struct Settings {
    gateway_url: String,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            gateway_url: "http://localhost:8091/login".to_string(),
        }
    }
}

fn settings_path(app: &tauri::AppHandle) -> Option<PathBuf> {
    app.path()
        .app_data_dir()
        .ok()
        .map(|d| d.join(SETTINGS_FILE))
}

fn load_settings(app: &tauri::AppHandle) -> Settings {
    let path = match settings_path(app) {
        Some(p) => p,
        None => return Settings::default(),
    };
    match fs::read_to_string(&path) {
        Ok(body) => serde_json::from_str(&body).unwrap_or_default(),
        Err(_) => Settings::default(),
    }
}

fn save_settings(app: &tauri::AppHandle, settings: &Settings) -> Result<(), String> {
    let path = settings_path(app).ok_or_else(|| "no app_data_dir".to_string())?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let body = serde_json::to_string_pretty(settings).map_err(|e| e.to_string())?;
    fs::write(&path, body).map_err(|e| e.to_string())
}

#[tauri::command]
fn get_gateway_url(app: tauri::AppHandle) -> String {
    load_settings(&app).gateway_url
}

#[tauri::command]
fn set_gateway_url(app: tauri::AppHandle, url: String) -> Result<(), String> {
    let mut s = load_settings(&app);
    s.gateway_url = url.clone();
    save_settings(&app, &s)?;
    // Re-navigate the existing window to the new URL instead of
    // requiring a restart — more responsive for users fixing a typo.
    if let Some(window) = app.get_webview_window("main") {
        let parsed = url.parse().map_err(|e: url::ParseError| e.to_string())?;
        window.navigate(parsed).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // Override the default config URL with the persisted
            // setting if the user has customised it. This runs once at
            // startup before the initial window becomes visible.
            let persisted = load_settings(app.handle()).gateway_url;
            let default_url = "http://localhost:8091/login";
            if persisted != default_url {
                let url: tauri::Url = persisted.parse()?;
                // Re-create the main window with the persisted URL.
                // Using `get_webview_window` + `navigate` would work
                // too, but Tauri may have already loaded the default
                // URL by this point — rebuilding before show is
                // cleaner.
                if let Some(existing) = app.get_webview_window("main") {
                    existing.navigate(url)?;
                } else {
                    WebviewWindowBuilder::new(
                        app,
                        "main",
                        WebviewUrl::External(url),
                    )
                    .title("Logos")
                    .inner_size(1400.0, 900.0)
                    .min_inner_size(800.0, 600.0)
                    .center()
                    .build()?;
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_gateway_url,
            set_gateway_url,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Logos desktop app");
}
