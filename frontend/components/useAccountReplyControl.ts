import { useEffect, useRef, useState } from 'react';
import type { AccountReplyControl } from '../types';
import { ACCOUNT_REPLY_CHANGED, getAccountReplyControl, setAccountReplyControl } from '../services/api';

export function useAccountReplyControl(account: string) {
  const selected = useRef(account);
  selected.current = account;
  const known = useRef<Record<string, AccountReplyControl>>({});
  const updating = useRef(new Set<string>());
  const [value, setValue] = useState<AccountReplyControl | null>(null);
  const [busyAccount, setBusyAccount] = useState('');
  const [failedAccount, setFailedAccount] = useState('');
  const accept = (state: AccountReplyControl) => {
    if ((known.current[state.cookie_id]?.revision ?? -1) > state.revision) return;
    known.current[state.cookie_id] = state;
    if (selected.current === state.cookie_id) {
      setValue(state);
      setFailedAccount('');
    }
  };
  useEffect(() => {
    if (!account) return;
    let cancelled = false;
    let reading = false;
    const refresh = async () => {
      if (reading || updating.current.has(account)) return;
      reading = true;
      try {
        const state = await getAccountReplyControl(account);
        if (!cancelled && !updating.current.has(account)) accept(state);
      } catch {
        if (!cancelled && !updating.current.has(account)) setFailedAccount(account);
      } finally { reading = false; }
    };
    const changed = (event: Event) => {
      const state = (event as CustomEvent<AccountReplyControl>).detail;
      if (state.cookie_id === account) accept(state);
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2000);
    window.addEventListener(ACCOUNT_REPLY_CHANGED, changed);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener(ACCOUNT_REPLY_CHANGED, changed);
    };
  }, [account]);

  const state = value?.cookie_id === account ? value : null;
  const toggle = async () => {
    if (!state || failedAccount === account || updating.current.has(account)) return;
    updating.current.add(account);
    setBusyAccount(account);
    try { accept(await setAccountReplyControl(account, !state.enabled, state.revision)); }
    catch (error) {
      // A timeout may follow a committed write. Read back, never blindly retry it.
      try {
        const actual = await getAccountReplyControl(account);
        accept(actual);
        window.dispatchEvent(new CustomEvent(ACCOUNT_REPLY_CHANGED, { detail: actual }));
      } catch { if (selected.current === account) setFailedAccount(account); }
      throw error;
    } finally {
      updating.current.delete(account);
      setBusyAccount(current => current === account ? '' : current);
    }
  };
  return { state, busy: busyAccount === account, unavailable: failedAccount === account, toggle };
}
