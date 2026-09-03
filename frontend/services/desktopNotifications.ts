import { useEffect } from 'react';
import { get, post } from '../lib/request';

const preferenceKey = 'desktop_message_notifications';
const soundPreferenceKey = 'desktop_message_notification_sound';
const preferenceEvent = 'desktop-notification-preference';
export const desktopNotificationsEnabled = () => localStorage.getItem(preferenceKey) !== 'false';
export const desktopNotificationSoundEnabled = () => localStorage.getItem(soundPreferenceKey) !== 'false';
type NotificationStatus = {
  available: boolean; active: boolean; sound_available: boolean;
  preference: { enabled: boolean; sound: boolean } | null;
};
let hydratedToken: string | null = null;
export async function getDesktopNotificationStatus() {
  const token = localStorage.getItem('auth_token');
  const status = await get<NotificationStatus>('/desktop/notifications/status');
  // The loopback origin changes on every app launch. Restore the user's saved
  // preferences once per login before binding the native queue to that login.
  if (status.available && token && token === localStorage.getItem('auth_token') && hydratedToken !== token) {
    if (status.preference) {
      localStorage.setItem(preferenceKey, String(status.preference.enabled));
      localStorage.setItem(soundPreferenceKey, String(status.preference.sound));
    }
    hydratedToken = token;
  }
  return status;
}
export const testDesktopNotification = () => post('/desktop/notifications/test', {});

let pendingConfiguration: Promise<unknown> = Promise.resolve();
function syncPreference(save = false) {
  const token = localStorage.getItem('auth_token');
  // Serialize startup/retry/toggle requests so an older enable cannot overtake disable.
  pendingConfiguration = pendingConfiguration.catch(() => {}).then(() => {
    if (token && token === localStorage.getItem('auth_token')) {
      return post('/desktop/notifications/session', {
        enabled: desktopNotificationsEnabled(), sound: desktopNotificationSoundEnabled(), save,
      });
    }
  });
  return pendingConfiguration;
}

export async function setDesktopNotifications(enabled: boolean) {
  return setPreference(preferenceKey, enabled);
}

export async function setDesktopNotificationSound(enabled: boolean) {
  return setPreference(soundPreferenceKey, enabled);
}

async function setPreference(key: string, enabled: boolean) {
  const previous = localStorage.getItem(key);
  localStorage.setItem(key, String(enabled));
  try {
    await syncPreference(true);
  } catch (error) {
    if (previous === null) localStorage.removeItem(key);
    else localStorage.setItem(key, previous);
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
