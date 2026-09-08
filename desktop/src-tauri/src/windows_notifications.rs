use std::{
    collections::VecDeque,
    path::MAIN_SEPARATOR,
    sync::{LazyLock, Mutex},
};

use notify_rust::{Notification, NotificationResponse};

const MAX_PENDING_CLICKS: usize = 128;
static CLICKS: LazyLock<Mutex<VecDeque<String>>> = LazyLock::new(|| Mutex::new(VecDeque::new()));

fn record_click(target: String) {
    if target.is_empty() {
        return;
    }
    if let Ok(mut clicks) = CLICKS.lock() {
        while clicks.len() >= MAX_PENDING_CLICKS {
            clicks.pop_front();
        }
        clicks.push_back(target);
    }
}

pub fn take_click() -> Option<String> {
    CLICKS.lock().ok()?.pop_front()
}

pub fn show(identifier: &str, title: &str, body: &str, sound: bool, target: &str) -> Result<(), String> {
    let mut notification = Notification::new();
    notification.summary(title).body(body);
    // notify-rust maps no sound name to an explicitly silent Windows toast.
    // Let Windows apply its volume, app permission and Do Not Disturb settings.
    if sound {
        notification.sound_name("Default");
    }

    // Match tauri-plugin-notification: development executables have no
    // installed AppUserModelID, while the NSIS install registers identifier.
    let executable_dir = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(|parent| parent.display().to_string()));
    let target_debug = format!("{MAIN_SEPARATOR}target{MAIN_SEPARATOR}debug");
    let target_release = format!("{MAIN_SEPARATOR}target{MAIN_SEPARATOR}release");
    if executable_dir.as_deref().is_some_and(|directory| {
        !directory.ends_with(&target_debug) && !directory.ends_with(&target_release)
    }) {
        notification.app_id(identifier);
    }

    let handle = notification.show().map_err(|error| error.to_string())?;
    let target = target.to_owned();
    std::thread::Builder::new()
        .name("windows-notification-click".to_owned())
        .spawn(move || {
            let _ = handle.wait_for_response(move |response: &NotificationResponse| {
                if response.is_default_action() {
                    record_click(target);
                }
            });
        })
        .map_err(|error| error.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{record_click, take_click, MAX_PENDING_CLICKS};
    use std::sync::{LazyLock, Mutex};

    static TEST_LOCK: LazyLock<Mutex<()>> = LazyLock::new(|| Mutex::new(()));

    fn clear() {
        while take_click().is_some() {}
    }

    #[test]
    fn click_queue_preserves_opaque_target_order() {
        let _guard = TEST_LOCK.lock().unwrap();
        clear();
        record_click("first".to_owned());
        record_click("second".to_owned());
        assert_eq!(take_click().as_deref(), Some("first"));
        assert_eq!(take_click().as_deref(), Some("second"));
        assert_eq!(take_click(), None);
    }

    #[test]
    fn click_queue_is_bounded_and_ignores_empty_targets() {
        let _guard = TEST_LOCK.lock().unwrap();
        clear();
        record_click(String::new());
        for index in 0..=MAX_PENDING_CLICKS {
            record_click(index.to_string());
        }
        assert_eq!(take_click().as_deref(), Some("1"));
        clear();
    }

    #[test]
    #[ignore = "Shows two test notifications in the interactive Windows desktop"]
    fn native_sound_and_silent_notifications() {
        for (sound, label) in [(true, "有声测试"), (false, "静音测试")] {
            super::show("com.genoooool.xianyuworkbench", "闲鱼工作台测试提醒", label, sound, "").unwrap();
            std::thread::sleep(std::time::Duration::from_secs(2));
        }
    }
}
