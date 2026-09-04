import React from 'react';
import { notify } from '../services/feedback';
import { useAccountReplyControl } from './useAccountReplyControl';

export const AccountReplySwitch: React.FC<{ control: ReturnType<typeof useAccountReplyControl> }> = ({ control }) => {
  const { state, busy, unavailable } = control;
  const unknown = !state || unavailable;
  return <button type="button" role="switch" aria-label="店铺自动回复总开关"
    aria-checked={!unknown && state.enabled} aria-busy={busy}
    disabled={unknown || busy}
    onClick={() => void control.toggle().catch(error => notify(error instanceof Error ? error.message : '开关保存失败，请重新读取状态', 'error'))}
    title="立即保存，与账号设置、AI 回复及聊天窗口同步。开启时保留人工接管的会话。"
    className="inline-flex shrink-0 items-center gap-3 rounded-md py-2 disabled:cursor-wait disabled:opacity-60">
    <span className="text-sm font-bold">{unknown ? '读取状态中' : busy ? '保存中' : state.enabled ? '已开启' : '已关闭'}</span>
    <span aria-hidden="true" className={`relative h-6 w-11 rounded-full transition-colors ${unknown ? 'bg-gray-400' : state.enabled ? 'bg-[#438864]' : 'bg-[#a45b5b]'}`}>
      <span className={`absolute left-[3px] top-[3px] h-[18px] w-[18px] rounded-full bg-[#efeee7] transition-transform ${!unknown && state.enabled ? 'translate-x-5' : ''}`} />
    </span>
  </button>;
};
