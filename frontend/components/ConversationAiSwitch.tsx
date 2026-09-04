import React from 'react';

interface Props {
  enabled: boolean | null;
  busy: boolean;
  unavailable: boolean;
  onToggle: () => void;
}

export const ConversationAiSwitch: React.FC<Props> = ({ enabled, busy, unavailable, onToggle }) => {
  const unknown = enabled === null || unavailable;
  const label = unknown ? 'AI 自动回复状态暂不可用，正在重新读取'
    : enabled ? 'AI 自动回复已开启；点击关闭当前会话' : 'AI 自动回复已关闭；点击开启当前会话';
  return (
    <button type="button" role="switch" aria-label="当前会话 AI 自动回复"
      aria-checked={enabled === true} aria-busy={busy || enabled === null}
      disabled={busy || unknown} onClick={onToggle}
      title={`${label}。只影响当前会话，仍遵循店铺回复设置。`}
      className="ml-auto inline-flex min-h-9 shrink-0 items-center gap-[9px] rounded-md py-1 pl-2 text-[var(--text-muted)] outline-offset-4 hover:text-[var(--text)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--brand)] disabled:cursor-wait disabled:opacity-60 [@media(pointer:coarse)]:min-h-11">
      <span aria-hidden="true" className="text-[13px] font-medium tracking-[0.015em]">AI</span>
      <span aria-hidden="true"
        className={`relative block h-6 w-11 rounded-full transition-colors duration-150 motion-reduce:transition-none ${
          unknown ? 'bg-[var(--surface-strong)]' : enabled ? 'bg-[#438864]' : 'bg-[#a45b5b]'
        }`}>
        <span className={`absolute left-[3px] top-[3px] h-[18px] w-[18px] rounded-full bg-[#efeee7] shadow-[0_1px_2px_rgba(0,0,0,0.16)] transition-transform duration-[180ms] ease-out motion-reduce:transition-none ${
          unknown ? 'translate-x-[10px]' : enabled ? 'translate-x-5' : 'translate-x-0'
        }`} />
      </span>
    </button>
  );
};
