import { useEffect, useRef, useState } from 'react';
import type { AccountReplyControl, ConversationReplyControl } from '../types';
import { ACCOUNT_REPLY_CHANGED, getConversationReplyControl, setConversationReplyControl } from '../services/api';

const keyFor = (account: string, cid: string) => JSON.stringify([account, cid]);

export function useConversationReplyControl(account: string, cid: string, active: boolean, refreshTick: number) {
  const key = keyFor(account, cid);
  const selected = useRef(key);
  selected.current = key;
  const known = useRef<Record<string, ConversationReplyControl>>({});
  const updating = useRef(new Set<string>());
  const [state, setState] = useState<{ key: string; value: ConversationReplyControl } | null>(null);
  const [busyKey, setBusyKey] = useState('');
  const [failedKey, setFailedKey] = useState('');

  const accept = (target: string, value: ConversationReplyControl) => {
    if (keyFor(value.cookie_id, value.chat_id) !== target) return;
    const previous = known.current[target];
    if (previous && (previous.revision > value.revision || previous.account_revision > value.account_revision)) return;
    known.current[target] = value;
    // A delayed result from another store/chat may update its cache, never this view.
    if (selected.current === target) {
      setState({ key: target, value });
      setFailedKey('');
    }
  };

  useEffect(() => {
    if (!active || !account || !cid) return;
    let cancelled = false;
    let reading = false;
    const refresh = async () => {
      if (reading || updating.current.has(key)) return;
      reading = true;
      try {
        const value = await getConversationReplyControl(account, cid);
        if (!cancelled && !updating.current.has(key)) accept(key, value);
      } catch {
        if (!cancelled && selected.current === key && !updating.current.has(key)) setFailedKey(key);
      } finally { reading = false; }
    };
    void refresh();
    const changed = (event: Event) => {
      const accountState = (event as CustomEvent<AccountReplyControl>).detail;
      const local = known.current[key];
      if (accountState.cookie_id !== account) return;
      if (local && accountState.revision >= local.account_revision) accept(key, {
        ...local, account_enabled: accountState.enabled, account_revision: accountState.revision,
        enabled: accountState.enabled && local.conversation_enabled,
      });
      void refresh();
    };
    window.addEventListener(ACCOUNT_REPLY_CHANGED, changed);
    const timer = window.setInterval(() => void refresh(), 2000); // Local SQLite, never platform traffic.
    return () => { cancelled = true; window.clearInterval(timer); window.removeEventListener(ACCOUNT_REPLY_CHANGED, changed); };
  }, [key, active, refreshTick]);

  const toggle = async (metadata: { buyer_id: string; buyer_name: string; item_id: string }) => {
    const current = state?.key === key ? state.value : null;
    if (!current || !current.account_enabled || failedKey === key || updating.current.has(key)) return null;
    updating.current.add(key);
    setBusyKey(key);
    try {
      const result = await setConversationReplyControl(account, cid, {
        ...metadata, enabled: !current.conversation_enabled, revision: current.revision,
      });
      accept(key, result);
      return result;
    } catch (error) {
      // An uncertain write is never blindly repeated. Re-read its persisted result.
      try { accept(key, await getConversationReplyControl(account, cid)); }
      catch { if (selected.current === key) setFailedKey(key); }
      throw error;
    } finally {
      updating.current.delete(key);
      setBusyKey((value) => value === key ? '' : value);
    }
  };

  return { state: state?.key === key ? state.value : null, busy: busyKey === key, unavailable: failedKey === key, toggle };
}
