//! No webview updater permissions. The authenticated backend queues only actions;
//! release URLs, signatures, archive paths and installation stay native-owned.
use std::{io::Read, path::Component, time::Duration};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Manager};
use tauri_plugin_updater::{Update, UpdaterExt};

const ENDPOINT: &str = "https://github.com/genoooool/xianyu-super-butler/releases/latest/download/latest.json";
const IDENTIFIER: &str = "com.genoooool.xianyuworkbench";
const COMPATIBILITY: u64 = 1;

#[derive(Clone, Deserialize)]
struct Command { id: String, action: String, version: String }
#[derive(Deserialize)]
struct Poll { command: Option<Command> }
#[derive(Clone, Serialize)]
struct Report {
    id: String, phase: String, version: String, latest_version: String,
    notes: String, error: String, progress: u32,
}
impl Report {
    fn new(command: &Command, phase: &str) -> Self {
        Self { id: command.id.clone(), phase: phase.into(), version: env!("CARGO_PKG_VERSION").into(),
            latest_version: String::new(), notes: String::new(), error: String::new(), progress: 0 }
    }
}

fn validate_release(update: &Update) -> Result<(), &'static str> {
    let url = &update.download_url;
    if url.scheme() != "https" || url.host_str() != Some("github.com") || url.port().is_some()
        || !url.username().is_empty() || url.password().is_some() || url.query().is_some() || url.fragment().is_some()
        || !url.path().starts_with("/genoooool/xianyu-super-butler/releases/download/")
        || !url.path().ends_with(".app.tar.gz") {
        return Err("下载地址不是本工作台的正式 Release");
    }
    if update.raw_json.get("data_compatibility").and_then(Value::as_u64) != Some(COMPATIBILITY) {
        return Err("此版本需要调整数据格式，暂不支持应用内安装");
    }
    let remote = semver::Version::parse(&update.version).map_err(|_| "版本号无效")?;
    let local = semver::Version::parse(env!("CARGO_PKG_VERSION")).unwrap();
    if remote <= local || !remote.pre.is_empty() { return Err("不安装旧版本或测试版本"); }
    Ok(())
}

fn archive_contract(bytes: &[u8], version: &str) -> Result<(), &'static str> {
    let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(bytes));
    let mut contract = None;
    let mut root = None;
    let mut total = 0u64;
    for entry in archive.entries().map_err(|_| "更新包无法读取")? {
        let mut entry = entry.map_err(|_| "更新包损坏")?;
        total = total.saturating_add(entry.size());
        if total > 4 * 1024 * 1024 * 1024 { return Err("更新包超出允许大小"); }
        let path = entry.path().map_err(|_| "更新包路径无效")?.into_owned();
        let parts: Vec<_> = path.components().collect();
        if parts.is_empty() || parts.iter().any(|p| !matches!(p, Component::Normal(_))) {
            return Err("更新包含不安全路径");
        }
        let top = parts[0].as_os_str().to_string_lossy().to_string();
        if !top.ends_with(".app") || root.as_ref().is_some_and(|r| r != &top) {
            return Err("更新包必须仅包含一个 App");
        }
        root = Some(top);
        let kind = entry.header().entry_type();
        if kind.is_symlink() {
            let target = entry.link_name().map_err(|_| "无效链接")?.ok_or("无效链接")?;
            let mut depth = parts.len() - 1;
            for component in target.components() {
                match component {
                    Component::Normal(_) => depth += 1,
                    Component::CurDir => (),
                    Component::ParentDir if depth > 1 => depth -= 1,
                    _ => return Err("更新包链接超出 App 范围"),
                }
            }
        } else if !kind.is_file() && !kind.is_dir() { return Err("更新包包含不支持的文件类型"); }
        if path.ends_with("Contents/Resources/update-contract.json") {
            if contract.is_some() || !kind.is_file() || entry.size() > 4096 { return Err("更新兼容信息无效"); }
            let mut text = String::new();
            entry.read_to_string(&mut text).map_err(|_| "兼容信息无法读取")?;
            contract = Some(serde_json::from_str::<Value>(&text).map_err(|_| "兼容信息格式错误")?);
        }
    }
    let data = contract.ok_or("缺少签名包内的版本兼容信息")?;
    let target = format!("darwin-{}", std::env::consts::ARCH);
    if data["identifier"] != IDENTIFIER || data["version"] != version
        || data["data_compatibility"].as_u64() != Some(COMPATIBILITY) || data["target"] != target {
        return Err("安装包版本、机型或数据格式不匹配");
    }
    Ok(())
}

#[derive(Clone)]
struct Bridge { client: reqwest::Client, base: String, token: String }
impl Bridge {
    fn request(&self, method: reqwest::Method, route: &str) -> reqwest::RequestBuilder {
        self.client.request(method, format!("{}/desktop/updates/{route}", self.base))
            .header("Cookie", format!("xianyu_desktop_access={}", self.token))
            .header("X-Xianyu-Desktop-Token", &self.token)
    }
    async fn report(&self, report: &Report) -> bool {
        self.request(reqwest::Method::POST, "report").json(report).send().await
            .map(|r| r.status().is_success()).unwrap_or(false)
    }
}

pub async fn watch(app: AppHandle, base: String, token: String) {
    let Ok(client) = reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(5)).build() else { return; };
    let bridge = Bridge { client, base, token };
    let mut pending: Option<Update> = None;
    loop {
        tokio::time::sleep(Duration::from_secs(2)).await;
        let Ok(response) = bridge.request(reqwest::Method::GET, "poll").send().await else { continue; };
        let Ok(Poll { command: Some(command) }) = response.json::<Poll>().await else { continue; };
        let mut report = Report::new(&command, "checking");
        if command.action == "check" {
            pending = None;
            // Static Release endpoint is fixed in code and config, not a UI URL.
            let builder = app.updater_builder().endpoints(vec![ENDPOINT.parse().unwrap()]);
            let result = match builder {
                Ok(b) => match b.timeout(Duration::from_secs(30)).build() {
                    Ok(updater) => updater.check().await,
                    Err(e) => Err(e),
                },
                Err(e) => Err(e),
            };
            match result {
                Ok(Some(update)) => {
                    report.latest_version = update.version.clone();
                    report.notes = update.body.as_deref().unwrap_or("").chars().take(12000).collect();
                    match validate_release(&update) {
                        Ok(()) => { report.phase = "available".into(); pending = Some(update); },
                        Err(error) => { report.phase = "incompatible".into(); report.error = error.into(); },
                    }
                },
                Ok(None) => report.phase = "current".into(),
                Err(_) => {
                    // Distinguish no published manifest from a failed network check.
                    let response = reqwest::Client::new().get(ENDPOINT).timeout(Duration::from_secs(10)).send().await;
                    if response.is_ok_and(|r| r.status() == reqwest::StatusCode::NOT_FOUND) {
                        report.phase = "unpublished".into();
                    } else {
                        report.phase = "error".into(); report.error = "检查失败，请检查网络后重试".into();
                    }
                },
            }
            bridge.report(&report).await;
        } else if command.action == "install" {
            report.phase = "error".into();
            let Some(mut update) = pending.take() else {
                report.error = "请重新检查更新".into(); bridge.report(&report).await; continue;
            };
            report.latest_version = update.version.clone();
            if command.version != update.version || validate_release(&update).is_err() {
                report.error = "版本已变化，请重新检查".into(); bridge.report(&report).await; continue;
            }
            update.timeout = Some(Duration::from_secs(120));
            let progress = std::sync::Arc::new(std::sync::atomic::AtomicU32::new(0));
            let reported = progress.clone();
            let progress_bridge = bridge.clone();
            let mut downloading = report.clone(); downloading.phase = "downloading".into();
            let heartbeat = tauri::async_runtime::spawn(async move {
                loop {
                    downloading.progress = reported.load(std::sync::atomic::Ordering::Relaxed);
                    progress_bridge.report(&downloading).await;
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
            });
            let mut received = 0u64;
            let oversized = std::sync::Arc::new(tokio::sync::Notify::new());
            let abort_download = oversized.clone();
            let download = update.download(|size, total| {
                received += size as u64;
                if received > 512 * 1024 * 1024 || total.is_some_and(|t| t > 512 * 1024 * 1024) {
                    abort_download.notify_one();
                }
                if let Some(total) = total.filter(|t| *t > 0) {
                    progress.store(((received.saturating_mul(100) / total).min(99)) as u32, std::sync::atomic::Ordering::Relaxed);
                }
            }, || {});
            let bytes = tokio::select! {
                value = download => value,
                _ = oversized.notified() => Err(tauri_plugin_updater::Error::Network("Update too large".into())),
            };
            heartbeat.abort();
            let _ = heartbeat.await;
            let bytes = match bytes {
                Ok(b) => b,
                Err(_) => { report.error = "下载或签名校验失败，未安装；请重新检查更新".into(); bridge.report(&report).await; continue; },
            };
            if let Err(error) = archive_contract(&bytes, &update.version) {
                report.error = error.into(); bridge.report(&report).await; continue;
            }
            // Atomically refuse busy business operations before stopping the backend.
            let prepared = bridge.request(reqwest::Method::POST, "prepare").json(&json!({"id": command.id})).send().await;
            let ready = match prepared {
                Ok(response) if response.status().is_success() => response.json::<Value>().await
                    .is_ok_and(|v| v["ready"] == true && v["data_compatibility"].as_u64() == Some(COMPATIBILITY)),
                _ => false,
            };
            if !ready {
                let _ = bridge.request(reqwest::Method::POST, "release").send().await;
                report.phase = "available".into(); report.error = "当前仍有操作进行中或登录已失效，请稍后再试".into();
                pending = Some(update); bridge.report(&report).await; continue;
            }
            report.phase = "installing".into(); report.progress = 100;
            if !bridge.report(&report).await {
                let _ = bridge.request(reqwest::Method::POST, "release").send().await;
                continue;
            }
            if !super::stop_backend_for_update(&app).await {
                show_install_error(&app, "本地服务尚未完全退出，更新已停止。请退出软件后重新打开。");
                return;
            }
            if update.install(bytes).is_err() {
                show_install_error(&app, "更新安装失败。原有账号、QA 和订单目录未被清理，请从自己的 GitHub Releases 下载新版后替换 App。");
                return;
            }
            app.restart();
        }
    }
}

fn show_install_error(app: &AppHandle, message: &str) {
    if let Some(window) = app.get_webview_window("main") {
        let text = serde_json::to_string(message).unwrap();
        let _ = window.eval(format!("document.body.replaceChildren(); document.body.style.cssText='padding:48px;background:#181815;color:#eee;font:18px system-ui'; document.body.textContent={text};"));
        let _ = window.show();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn package(contract: Value) -> Vec<u8> {
        let encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
        let mut tar = tar::Builder::new(encoder);
        let body = contract.to_string();
        let mut header = tar::Header::new_gnu(); header.set_size(body.len() as u64); header.set_mode(0o644); header.set_cksum();
        tar.append_data(&mut header, "App.app/Contents/Resources/update-contract.json", body.as_bytes()).unwrap();
        tar.into_inner().unwrap().finish().unwrap()
    }
    #[test]
    fn signed_contract_must_match_version_identity_architecture_and_data() {
        let contract = json!({"identifier": IDENTIFIER, "version":"1.0.1", "data_compatibility":1,
            "target": format!("darwin-{}", std::env::consts::ARCH)});
        assert!(archive_contract(&package(contract.clone()), "1.0.1").is_ok());
        assert!(archive_contract(&package(contract.clone()), "1.0.2").is_err());
        for (key, wrong) in [("identifier", json!("other")), ("target", json!("windows-x86_64")), ("data_compatibility", json!(2))] {
            let mut bad = contract.clone(); bad[key] = wrong;
            assert!(archive_contract(&package(bad), "1.0.1").is_err());
        }
        assert!(archive_contract(b"broken", "1.0.1").is_err());
    }

    #[test]
    #[ignore = "Requires explicitly prepared disposable app, signed archive and loopback fixture server"]
    fn real_signed_download_install_and_wrong_signature_rejection() {
        use tauri::test::{mock_builder, mock_context, noop_assets};
        let root = std::path::PathBuf::from(std::env::var("XIANYU_UPDATER_TEST_DIR").unwrap());
        assert!(root.join("DISPOSABLE_UPDATER_FIXTURE").is_file());
        assert!(root.canonicalize().unwrap().starts_with("/private/tmp/"));
        let manifest: Value = serde_json::from_slice(&std::fs::read(root.join("fixture.json")).unwrap()).unwrap();
        let mut context = mock_context(noop_assets());
        context.config_mut().plugins.0.insert("updater".into(), json!({
            "pubkey":manifest["pubkey"], "dangerousInsecureTransportProtocol":true
        }));
        let app = mock_builder().plugin(tauri_plugin_updater::Builder::new().build()).build(context).unwrap();
        let executable = root.join("installed/闲鱼工作台.app/Contents/MacOS/xianyu-workbench");
        let data_file = root.join("user-data/qa-and-orders.json");
        let before = std::fs::read(&data_file).unwrap();
        let update = tauri::async_runtime::block_on(async {
            let update = app.updater_builder().executable_path(&executable)
                .endpoints(vec![manifest["endpoint"].as_str().unwrap().parse().unwrap()]).unwrap()
                .version_comparator(|_, _| true).build().unwrap().check().await.unwrap().unwrap();
            let good = update.download(|_, _| {}, || {}).await.unwrap();
            archive_contract(&good, &update.version).unwrap();
            update.install(good).unwrap();
            update
        });
        assert_eq!(before, std::fs::read(&data_file).unwrap());
        assert!(root.join("installed/闲鱼工作台.app/Contents/Resources/update-contract.json").is_file());
        let new_binary = std::fs::read(&executable).unwrap();
        let mut policy = update.clone();
        let local = semver::Version::parse(env!("CARGO_PKG_VERSION")).unwrap();
        let newer = semver::Version::new(local.major, local.minor, local.patch.saturating_add(1)).to_string();
        policy.version = newer.clone();
        policy.raw_json["data_compatibility"] = json!(1);
        policy.download_url = format!("https://github.com/genoooool/xianyu-super-butler/releases/download/v{newer}/app.app.tar.gz").parse().unwrap();
        assert!(validate_release(&policy).is_ok());
        for wrong in [env!("CARGO_PKG_VERSION"), "0.9.9", "1.0.1-beta.1", "not-a-version"] {
            policy.version = wrong.into(); assert!(validate_release(&policy).is_err());
        }
        policy.version = newer.clone();
        policy.raw_json["data_compatibility"] = json!(2);
        assert!(validate_release(&policy).is_err());
        policy.raw_json["data_compatibility"] = json!(1);
        for wrong in [
            format!("http://github.com/genoooool/xianyu-super-butler/releases/download/v{newer}/app.app.tar.gz"),
            format!("https://github.com/23Star/xianyu-super-butler/releases/download/v{newer}/app.app.tar.gz"),
            "https://example.com/app.app.tar.gz".into(),
            format!("https://github.com/genoooool/xianyu-super-butler/releases/download/v{newer}/app.app.tar.gz?redirect=other"),
        ] {
            policy.download_url = wrong.parse().unwrap(); assert!(validate_release(&policy).is_err());
        }
        let mut bad = update;
        bad.signature = "invalid-signature".into();
        assert!(tauri::async_runtime::block_on(bad.download(|_, _| {}, || {})).is_err());
        assert_eq!(new_binary, std::fs::read(&executable).unwrap());
        assert_eq!(before, std::fs::read(&data_file).unwrap());
    }
}
