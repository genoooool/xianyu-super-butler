//! Keep foreground banners explicit; the legacy macOS notifier suppresses them.
use std::ffi::{c_char, CString, NulError};

extern "C" {
    fn xianyu_notifications_initialize();
    fn xianyu_notifications_show(title: *const c_char, body: *const c_char);
}

pub fn initialize() {
    // Called on Tauri's main thread before any notification can be submitted.
    unsafe { xianyu_notifications_initialize() };
}

pub fn show(title: &str, body: &str) -> Result<(), NulError> {
    let title = CString::new(title)?;
    let body = CString::new(body)?;
    // The bridge copies both strings before returning. Scheduling is asynchronous,
    // and its result (not a visible-delivery receipt) is logged by the bridge.
    unsafe { xianyu_notifications_show(title.as_ptr(), body.as_ptr()) };
    Ok(())
}
