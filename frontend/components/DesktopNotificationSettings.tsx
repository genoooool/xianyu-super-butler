import React, { useEffect, useState } from 'react';
import { Bell } from 'lucide-react';
import { SectionHeader } from './ui';
import { notify } from '../services/feedback';
import {
  desktopNotificationsEnabled, desktopNotificationSoundEnabled, getDesktopNotificationStatus,
  setDesktopNotifications, setDesktopNotificationSound, testDesktopNotification,
} from '../services/desktopNotifications';

export default function DesktopNotificationSettings() {
  const [available, setAvailable] = useState(false);
  const [enabled, setEnabled] = useState(desktopNotificationsEnabled);
  const [soundAvailable, setSoundAvailable] = useState(false);
  const [sound, setSound] = useState(desktopNotificationSoundEnabled);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    getDesktopNotificationStatus().then(status => {
      setAvailable(status.available);
      setSoundAvailable(status.sound_available);
      setEnabled(desktopNotificationsEnabled());
      setSound(desktopNotificationSoundEnabled());
    }).catch(() => {});
  }, []);
  if (!available) return null;
  const toggle = async () => {
    setBusy(true);
    try {
      await setDesktopNotifications(!enabled);
      setEnabled(!enabled);
    } catch {
      notify('提醒设置未保存，请重试', 'error');
    } finally {
      setBusy(false);
    }
  };
  const toggleSound = async () => {
    setBusy(true);
    try {
      await setDesktopNotificationSound(!sound);
      setSound(!sound);
    } catch {
      notify('提示音设置未保存，请重试', 'error');
    } finally {
      setBusy(false);
    }
  };
  const test = async () => {
    setBusy(true);
    try {
      await testDesktopNotification();
      notify('测试提醒已提交；是否弹出取决于系统通知权限和免打扰设置', 'success');
    } catch {
      notify('测试提醒未提交，请稍候重试', 'error');
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="section-panel">
      <SectionHeader title="本机消息提醒" description="无需停在消息页，收到新消息时由系统弹窗提醒。" icon={Bell} />
      <div className="grid gap-4 p-4">
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-sm font-bold">新消息弹窗</p>
            <p className="mt-1 text-xs leading-5 text-gray-500">只显示新消息数量，不展示买家姓名或聊天内容。开关立即生效。</p>
          </div>
          <button type="button" role="switch" aria-label="新消息弹窗" aria-checked={enabled}
            disabled={busy} onClick={toggle}
            className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${enabled ? 'bg-[#ffe100]' : 'bg-gray-300'}`}>
            <span className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white transition-transform ${enabled ? 'translate-x-5' : ''}`} />
          </button>
        </div>
        {soundAvailable && (
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="text-sm font-bold">消息提示音</p>
              <p className="mt-1 text-xs leading-5 text-gray-500">随弹窗播放系统提示音。若无声，请检查系统音量及“闲鱼工作台”的通知声音设置；免打扰时不会强制发声。</p>
            </div>
            <button type="button" role="switch" aria-label="消息提示音" aria-checked={sound}
              disabled={!enabled || busy} onClick={toggleSound}
              className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${sound ? 'bg-[#ffe100]' : 'bg-gray-300'}`}>
              <span className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white transition-transform ${sound ? 'translate-x-5' : ''}`} />
            </button>
          </div>
        )}
        <p className="text-xs leading-5 text-gray-500">关闭窗口、缩入托盘后仍可提醒；完全退出软件后停止。若未弹出，请在系统通知设置中允许“闲鱼工作台”，并检查免打扰模式。</p>
        <button type="button" className="inline-flex items-center justify-self-start rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm font-bold text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50" disabled={!enabled || busy} onClick={test}>测试弹窗</button>
      </div>
    </section>
  );
}
