import { useEffect } from 'react';
import { get, post } from '../lib/request';

const preferenceKey = 'desktop_message_notifications';
const preferenceEvent = 'desktop-notification-preference';
export const desktopNotificationsEnabled = () => localStorage.getItem(preferenceKey) !== 'false';
export const getDesktopNotificationStatus = () => get<{ available: boolean; active: boolean }>('/desktop/notifications/status');
export const testDesktopNotification = () => post('/desktop/notifications/test', {});

let pendingConfiguration: Promise<unknown> = Promise.resolve();
function syncPreference() {
  const token = localStorage.getItem('auth_token');
  // Serialize startup/retry/toggle requests so an older enable cannot overtake disable.
  pendingConfiguration = pendingConfiguration.catch(() => {}).then(() => {
    if (token && token === localStorage.getItem('auth_token')) {
      return post('/desktop/notifications/session', { enabled: desktopNotificationsEnabled() });
    }
  });
  return pendingConfiguration;
}

export async function setDesktopNotifications(enabled: boolean) {
  const previous = desktopNotificationsEnabled();
  localStorage.setItem(preferenceKey, String(enabled));
  try {
    await syncPreference();
  } catch (error) {
    localStorage.setItem(preferenceKey, String(previous));
    throw error;
  }
  window.dispatchEvent(new Event(preferenceEvent));
}

// Native Rust polls the in-memory event queue even when this webview is hidden.
// The UI only binds that queue to its authenticated user, never to a page tab.
export function useDesktopNotifications(loggedIn: boolean) {
  useEffect(() => {
    if (!loggedIn) return;
    let stopped = false;
    const token = localStorage.getItem('auth_token');
    const activate = async () => {
      try {
        const status = await getDesktopNotificationStatus();
        if (!stopped && status.available && token === localStorage.getItem('auth_token')) {
          await syncPreference();
        }
      } catch {
        // Transient startup failures retry; never claim OS delivery from HTTP success.
      }
    };
    void activate();
    const timer = window.setInterval(activate, 30000);
    window.addEventListener(preferenceEvent, activate);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      window.removeEventListener(preferenceEvent, activate);
    };
  }, [loggedIn]);
}
