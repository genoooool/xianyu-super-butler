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
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};
use uuid::Uuid;

struct BackendProcess(Mutex<Option<CommandChild>>);

#[derive(serde::Deserialize)]
struct NotificationBatch {
    cursor: u64,
    count: u32,
    test_count: u32,
}

async fn watch_notifications(
    app: tauri::AppHandle,
    client: reqwest::Client,
    base_url: String,
    token: String,
) {
    let mut cursor = 0;
    loop {
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
                    if batch.count > 0 || batch.test_count > 0 {
                        let body = if batch.count > 0 {
                            format!("收到 {} 条新消息，请打开消息中心查看。", batch.count)
                        } else {
                            "这是一条测试提醒，没有向买家发送消息。".to_owned()
                        };
                        let title = if batch.count > 0 {
                            "闲鱼工作台 · 新消息"
                        } else {
                            "闲鱼工作台 · 测试提醒"
                        };
                        // Submitted is not a delivery receipt: macOS permission/DND decides visibility.
                        if app
                            .notification()
                            .builder()
                            .title(title)
                            .body(body)
                            .show()
                            .is_err()
                        {
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
    setup_tray(app)?;

    let window = app
        .get_webview_window("main")
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "找不到主窗口"))?;
    // Packaging acceptance gets a fresh empty profile, never real seller data.
    let data_dir = if std::env::args().any(|arg| arg == "--desktop-smoke") {
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

    let sidecar = app
        .shell()
        .sidecar("xianyu-backend")?
        .current_dir(&data_dir)
        .env("XIANYU_DESKTOP", "1")
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

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .setup(start_backend)
        .build(tauri::generate_context!())
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
