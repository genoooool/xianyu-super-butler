//! Keep foreground banners explicit; the legacy macOS notifier suppresses them.
use std::{ffi::{c_char, CStr, CString, NulError}, sync::Mutex};

static CLICKED: Mutex<Option<String>> = Mutex::new(None);

extern "C" fn on_click(target: *const c_char) {
    if target.is_null() { return; }
    // Copy during the callback; the ObjC string does not outlive this call.
    let value = unsafe { CStr::from_ptr(target) }.to_string_lossy();
    if value.len() <= 64 && value.bytes().all(|b| b.is_ascii_hexdigit()) {
        if let Ok(mut pending) = CLICKED.lock() { *pending = Some(value.into_owned()); }
    }
}

pub fn take_click() -> Option<String> {
    CLICKED.lock().ok()?.take()
}

extern "C" {
    fn xianyu_notifications_initialize(on_click: extern "C" fn(*const c_char));
    fn xianyu_notifications_show(title: *const c_char, body: *const c_char, sound: bool, target: *const c_char);
}

pub fn initialize() {
    // Called on Tauri's main thread before any notification can be submitted.
    unsafe { xianyu_notifications_initialize(on_click) };
}

pub fn show(title: &str, body: &str, sound: bool, target: &str) -> Result<(), NulError> {
    let title = CString::new(title)?;
    let body = CString::new(body)?;
    let target = CString::new(target)?;
    // The bridge copies both strings before returning. Scheduling is asynchronous,
    // and its result (not a visible-delivery receipt) is logged by the bridge.
    unsafe { xianyu_notifications_show(title.as_ptr(), body.as_ptr(), sound, target.as_ptr()) };
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn callback_copies_opaque_ticket_replaces_old_and_rejects_arbitrary_text() {
        on_click(CString::new("abc123").unwrap().as_ptr());
        on_click(CString::new("def456").unwrap().as_ptr());
        on_click(CString::new("javascript:bad").unwrap().as_ptr());
        assert_eq!(take_click().as_deref(), Some("def456"));
        assert!(take_click().is_none());
        on_click(CString::new("").unwrap().as_ptr());
        assert_eq!(take_click().as_deref(), Some(""));
    }
}
