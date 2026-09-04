#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    net::TcpListener,
    path::Path,
    sync::Mutex,
    time::{Duration, Instant},
};

use tauri::{
    menu::{Menu, MenuItem},
    path::BaseDirectory,
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, RunEvent, WindowEvent,
};
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};
use uuid::Uuid;

#[cfg(target_os = "macos")]
mod macos_notifications;
#[cfg(target_os = "windows")]
mod windows_notifications;
mod updater;

struct BackendProcess(Mutex<Option<CommandChild>>);

fn show_notification(
    app: &tauri::AppHandle,
    title: &str,
    body: &str,
    sound: bool,
    target: &str,
) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let _ = app;
        macos_notifications::show(title, body, sound, target).map_err(|error| error.to_string())
    }
    #[cfg(target_os = "windows")]
    {
        let _ = sound;
        windows_notifications::show(&app.config().identifier, title, body, target)
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        // Sound preference and click routing are currently native on macOS/Windows.
        let _ = (sound, target);
        app.notification()
            .builder()
            .title(title)
            .body(body)
            .show()
            .map_err(|error| error.to_string())
    }
}

#[derive(serde::Deserialize)]
struct NotificationBatch {
    cursor: u64,
    count: u32,
    test_count: u32,
    #[serde(default)]
    handoff_count: u32,
    #[serde(default)]
    sound: bool,
    #[serde(default)]
    target: String,
}

impl NotificationBatch {
    fn presentation(&self) -> Option<(&'static str, String)> {
        if self.handoff_count > 0 {
            Some(("闲鱼工作台 · 待人工处理", format!(
                "有 {} 个会话需要你接手，自动回复已暂停。请打开消息中心处理。", self.handoff_count)))
        } else if self.count > 0 {
            Some(("闲鱼工作台 · 新消息", format!("收到 {} 条新消息，请打开消息中心查看。", self.count)))
        } else if self.test_count > 0 {
            Some(("闲鱼工作台 · 测试提醒", "这是一条测试提醒，没有向买家发送消息。".to_owned()))
        } else {
            None
        }
    }
}

#[cfg(test)]
mod notification_batch_tests {
    use super::NotificationBatch;

    #[test]
    fn takeover_is_distinct_and_prioritized_over_message_alerts() {
        let batch = NotificationBatch { cursor: 3, count: 1, test_count: 1, handoff_count: 2, sound: true, target: String::new() };
        let (title, body) = batch.presentation().unwrap();
        assert_eq!(title, "闲鱼工作台 · 待人工处理");
        assert!(body.contains("2 个会话"));
        assert!(body.contains("自动回复已暂停"));
        assert!(batch.sound);
    }

    #[test]
    fn empty_batches_are_silent_and_messages_keep_their_title() {
        let batch = NotificationBatch { cursor: 0, count: 0, test_count: 0, handoff_count: 0, sound: false, target: String::new() };
        assert_eq!(batch.handoff_count, 0);
        assert!(batch.presentation().is_none());
        let message = NotificationBatch { count: 1, ..batch };
        assert_eq!(message.presentation().unwrap().0, "闲鱼工作台 · 新消息");
    }
}

async fn watch_notifications(
    app: tauri::AppHandle,
    client: reqwest::Client,
    base_url: String,
    token: String,
) {
    let mut cursor = 0;
    loop {
        #[cfg(target_os = "macos")]
        let clicked_target = macos_notifications::take_click();
        #[cfg(target_os = "windows")]
        let clicked_target = windows_notifications::take_click();
        #[cfg(not(any(target_os = "macos", target_os = "windows")))]
        let clicked_target: Option<String> = None;
        if let Some(target) = clicked_target {
            show_main_window(&app);
            let _ = client.post(format!("{base_url}/desktop/notifications/activate"))
                .header("Cookie", format!("xianyu_desktop_access={token}"))
                .header("X-Xianyu-Desktop-Token", &token)
                .json(&std::collections::HashMap::from([("target", target)])).send().await;
        }
        // Loopback only: no extra requests to Xianyu, and no webview IPC grants.
        let response = client
            .get(format!(
                "{base_url}/desktop/notifications/poll?after={cursor}"
            ))
            .header("Cookie", format!("xianyu_desktop_access={token}"))
            .header("X-Xianyu-Desktop-Token", &token)
            .send()
            .await;
        if let Ok(response) = response {
            if response.status().is_success() {
                if let Ok(batch) = response.json::<NotificationBatch>().await {
                    if let Some((title, body)) = batch.presentation() {
                        // Submitted is not a delivery receipt: macOS permission/DND decides visibility.
                        if show_notification(&app, title, &body, batch.sound, &batch.target).is_err() {
                            tokio::time::sleep(Duration::from_secs(5)).await;
                            continue;
                        }
                    }
                    cursor = batch.cursor;
                }
            }
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

fn append_launcher_log(data_dir: &Path, message: &str) {
    let log_dir = data_dir.join("logs");
    let _ = fs::create_dir_all(&log_dir);
    if let Ok(mut file) = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("desktop-launcher.log"))
    {
        let _ = writeln!(file, "{message}");
    }
}

fn select_free_port() -> io::Result<u16> {
    TcpListener::bind(("127.0.0.1", 0))?
        .local_addr()
        .map(|address| address.port())
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn setup_tray(app: &tauri::App) -> tauri::Result<()> {
    let open_item = MenuItem::with_id(app, "open", "打开闲鱼工作台", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open_item, &quit_item])?;

    let mut builder = TrayIconBuilder::new()
        .menu(&menu)
        .show_menu_on_left_click(false)
        .tooltip("闲鱼工作台")
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });

    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

fn set_splash_status(window: &tauri::WebviewWindow, message: &str) {
    let escaped = message
        .replace('\\', "\\\\")
        .replace('`', "\\`")
        .replace("${", "\\${")
        .replace('\n', "\\n")
        .replace('\r', "");
    let _ = window.eval(format!(
        "window.__setStartupStatus && window.__setStartupStatus(`{escaped}`);"
    ));
}

fn set_splash_error(window: &tauri::WebviewWindow, message: &str) {
    let escaped = message
        .replace('\\', "\\\\")
        .replace('`', "\\`")
        .replace("${", "\\${")
        .replace('\n', "\\n")
        .replace('\r', "");
    let _ = window.eval(format!(
        "window.__setStartupError && window.__setStartupError(`{escaped}`);"
    ));
}

fn start_backend(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let started = Instant::now();
    #[cfg(target_os = "macos")]
    macos_notifications::initialize();
    setup_tray(app)?;

    let window = app
        .get_webview_window("main")
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "找不到主窗口"))?;
    // Packaging acceptance gets a fresh empty profile, never real seller data.
    let smoke = std::env::args().any(|arg| arg == "--desktop-smoke");
    let data_dir = if smoke {
        std::env::temp_dir().join(format!("xianyu-desktop-smoke-{}", Uuid::new_v4()))
    } else {
        app.path().app_local_data_dir()?
    };
    fs::create_dir_all(&data_dir)?;

    let playwright_dir = app.path().resolve("playwright", BaseDirectory::Resource)?;
    if !playwright_dir.is_dir() {
        let message = format!("安装包缺少内置 Chromium：{}", playwright_dir.display());
        append_launcher_log(&data_dir, &message);
        set_splash_error(&window, &message);
        return Err(io::Error::new(io::ErrorKind::NotFound, message).into());
    }

    let port = select_free_port()?;
    let desktop_token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let base_url = format!("http://127.0.0.1:{port}");

    append_launcher_log(&data_dir, &format!("starting backend on 127.0.0.1:{port}"));
    set_splash_status(&window, "正在启动本地服务…");

    // macOS and Windows ship the complete runtime as read-only bundle resources,
    // avoiding onefile extraction on every launch. Keep a legacy/Linux fallback.
    #[cfg(target_os = "windows")]
    let backend_relative = "backend/xianyu-backend.exe";
    #[cfg(not(target_os = "windows"))]
    let backend_relative = "backend/xianyu-backend";
    let backend_path = app
        .path()
        .resolve(backend_relative, BaseDirectory::Resource)?;
    let command = if cfg!(any(target_os = "macos", target_os = "windows")) && backend_path.is_file() {
        app.shell().command(backend_path)
    } else {
        app.shell().sidecar("xianyu-backend")?
    };
    let sidecar = command
        .current_dir(&data_dir)
        .env("XIANYU_DESKTOP", "1")
        .env("XIANYU_DESKTOP_SMOKE", if smoke { "1" } else { "0" })
        .env("XIANYU_DATA_DIR", &data_dir)
        .env("XIANYU_DESKTOP_TOKEN", &desktop_token)
        .env("PLAYWRIGHT_BROWSERS_PATH", &playwright_dir)
        .env("API_HOST", "127.0.0.1")
        .env("API_PORT", port.to_string())
        .env("PYTHONUTF8", "1")
        .env("PYTHONUNBUFFERED", "1");

    let (mut events, child) = sidecar.spawn()?;
    app.manage(BackendProcess(Mutex::new(Some(child))));

    let event_data_dir = data_dir.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    let line = String::from_utf8_lossy(&bytes);
                    append_launcher_log(&event_data_dir, &format!("backend stdout: {line}"));
                }
                CommandEvent::Stderr(bytes) => {
                    let line = String::from_utf8_lossy(&bytes);
                    append_launcher_log(&event_data_dir, &format!("backend stderr: {line}"));
                }
                CommandEvent::Error(error) => {
                    append_launcher_log(&event_data_dir, &format!("backend event error: {error}"));
                }
                CommandEvent::Terminated(payload) => {
                    append_launcher_log(
                        &event_data_dir,
                        &format!(
                            "backend terminated: code={:?}, signal={:?}",
                            payload.code, payload.signal
                        ),
                    );
                    break;
                }
                _ => {}
            }
        }
    });

    let health_window = window.clone();
    let health_data_dir = data_dir.clone();
    let health_token = desktop_token.clone();
    let notification_app = app.handle().clone();
    tauri::async_runtime::spawn(async move {
        let client = match reqwest::Client::builder()
            .no_proxy() // The launch secret must never leave the loopback interface.
            .timeout(Duration::from_secs(3))
            .build()
        {
            Ok(client) => client,
            Err(error) => {
                let message = format!("初始化本地健康检查失败: {error}");
                append_launcher_log(&health_data_dir, &message);
                set_splash_error(&health_window, &message);
                return;
            }
        };

        for attempt in 1..=120 {
            if attempt == 20 {
                set_splash_status(&health_window, "正在加载后端组件…");
            } else if attempt == 60 {
                set_splash_status(&health_window, "正在初始化数据库和浏览器组件…");
            }

            let healthy = client
                .get(format!("{base_url}/health"))
                .send()
                .await
                .map(|response| response.status().is_success())
                .unwrap_or(false);

            if healthy {
                append_launcher_log(
                    &health_data_dir,
                    &format!(
                        "backend health check passed in {:.2}s",
                        started.elapsed().as_secs_f64()
                    ),
                );
                let bootstrap_url = format!("{base_url}/desktop/bootstrap?token={health_token}");
                match tauri::Url::parse(&bootstrap_url) {
                    Ok(url) => {
                        if let Err(error) = health_window.navigate(url) {
                            let message = format!("打开本地工作台失败: {error}");
                            append_launcher_log(&health_data_dir, &message);
                            set_splash_error(&health_window, &message);
                        }
                    }
                    Err(error) => {
                        let message = format!("本地工作台地址无效: {error}");
                        append_launcher_log(&health_data_dir, &message);
                        set_splash_error(&health_window, &message);
                    }
                }
                tauri::async_runtime::spawn(updater::watch(notification_app.clone(), base_url.clone(), health_token.clone()));
                watch_notifications(notification_app, client, base_url, health_token).await;
                return;
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
        }

        let message = format!(
            "本地后端在 120 秒内没有就绪。请查看日志：{}",
            health_data_dir.join("logs/desktop-launcher.log").display()
        );
        append_launcher_log(&health_data_dir, &message);
        set_splash_error(&health_window, &message);
    });

    Ok(())
}

fn stop_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<BackendProcess>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(child) = guard.take() {
                #[cfg(unix)]
                {
                    // CommandChild.kill() sends SIGKILL, preventing the onefile
                    // bootloader from forwarding shutdown and cleaning its temp
                    // directory. The backend owns bounded descendant cleanup.
                    if std::process::Command::new("/bin/kill")
                        .args(["-TERM", &child.pid().to_string()])
                        .status()
                        .map(|status| status.success())
                        .unwrap_or(false)
                    {
                        return;
                    }
                }
                let _ = child.kill();
            }
        }
    }
}

async fn stop_backend_for_update(app: &tauri::AppHandle) -> bool {
    let pid = app.try_state::<BackendProcess>().and_then(|s| s.0.lock().ok().and_then(|g| g.as_ref().map(|c| c.pid())));
    stop_backend(app);
    let Some(pid) = pid else { return true; };
    #[cfg(unix)]
    for _ in 0..60 {
        if !std::process::Command::new("/bin/kill").args(["-0", &pid.to_string()])
            .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null())
            .status().map(|s| s.success()).unwrap_or(true) { return true; }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    false
}

fn main() {
    if std::env::args().any(|arg| arg == "--version") {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let context = tauri::generate_context!();
    #[cfg(target_os = "windows")]
    let mut context = context;
    // Isolate the native WebView profile as well as the backend smoke data.
    #[cfg(target_os = "windows")]
    if std::env::args().any(|arg| arg == "--desktop-smoke") {
        for window in &mut context.config_mut().app.windows {
            window.create = false;
        }
    }
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            #[cfg(target_os = "windows")]
            if std::env::args().any(|arg| arg == "--desktop-smoke") {
                for config in app.config().app.windows.clone() {
                    tauri::WebviewWindowBuilder::from_config(app, &config)?
                        .data_directory(std::env::temp_dir().join(format!(
                            "xianyu-webview-smoke-{}", Uuid::new_v4()
                        )))
                        .build()?;
                }
            }
            start_backend(app)
        })
        .build(context)
        .expect("failed to build desktop application");

    app.run(|app_handle, event| match event {
        RunEvent::WindowEvent {
            label,
            event: WindowEvent::CloseRequested { api, .. },
            ..
        } if label == "main" => {
            api.prevent_close();
            if let Some(window) = app_handle.get_webview_window("main") {
                let _ = window.hide();
            }
        }
        RunEvent::ExitRequested { .. } | RunEvent::Exit => stop_backend(app_handle),
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show_main_window(app_handle),
        _ => {}
    });
}
