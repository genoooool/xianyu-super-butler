import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { X, ImagePlus, Loader2 } from 'lucide-react';
import { getReplyImage, uploadReplyImage } from '../services/knowledge';
import { notify } from '../services/feedback';

export const EnabledBadge: React.FC<{ enabled: boolean }> = ({ enabled }) =>
  <span className={`inline-flex shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${enabled ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>{enabled ? '已启用' : '已停用'}</span>;

export const ReplyImage: React.FC<{ id: string }> = ({ id }) => {
  const [url, setUrl] = useState('');
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true; let objectUrl = '';
    setUrl(''); setFailed(false);
    getReplyImage(id).then(blob => {
      if (!active) return;
      objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [id]);
  return url ? <img src={url} alt="回复图片" className="h-24 w-24 rounded-lg border border-[var(--border)] object-contain" /> :
    <span className="flex h-24 w-24 items-center justify-center rounded-lg border text-xs">{failed ? '图片加载失败' : '加载图片…'}</span>;
};

export const ReplyImagePicker: React.FC<{ ids: string[]; onChange: (ids: string[]) => void; disabled?: boolean; onBusy?: (busy: boolean) => void }> = ({ ids, onChange, disabled, onBusy }) => {
  const [uploading, setUploading] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  return <div className="space-y-2">
    <div className="flex flex-wrap gap-3">{ids.map((id, index) => <div key={id} className="relative">
      <ReplyImage id={id} />
      <button type="button" disabled={disabled || uploading} aria-label={`移除回复图片 ${index + 1}`} onClick={() => onChange(ids.filter(value => value !== id))}
        className="absolute -right-1 -top-1 rounded-full bg-red-100 p-1 text-red-800"><X size={14} /></button>
    </div>)}</div>
    <label className={`ios-btn-secondary inline-flex cursor-pointer items-center gap-2 px-3 py-2 text-sm ${(disabled || uploading || ids.length >= 4) ? 'pointer-events-none opacity-50' : ''}`}>
      {uploading ? <Loader2 size={16} className="animate-spin" /> : <ImagePlus size={16} />}{uploading ? '上传中…' : '添加图片'}
      <input type="file" aria-label="添加回复图片" accept="image/png,image/jpeg,image/webp" className="sr-only" disabled={disabled || uploading || ids.length >= 4}
        onChange={async event => {
          const file = event.target.files?.[0]; event.target.value = '';
          if (!file) return;
          if (file.size > 5 * 1024 * 1024) { notify('图片不能超过5MB', 'error'); return; }
          setUploading(true); onBusy?.(true);
          try { const asset = await uploadReplyImage(file); if (mounted.current) onChange([...new Set([...ids, asset.id])]); }
          catch { if (mounted.current) notify('图片上传失败，请确认是5MB以内的静态图片', 'error'); }
          finally { if (mounted.current) { setUploading(false); onBusy?.(false); } }
        }} />
    </label>
    <p className="text-xs text-[var(--text-muted)]">最多4张静态图片，每张5MB。答案可以只有图片；发送时不由 AI 改写。</p>
  </div>;
};

export const ReplyModal: React.FC<{ title: string; onClose: () => void; busy?: boolean; children: React.ReactNode }> = ({ title, onClose, busy, children }) => {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose); closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); if (!busy) closeRef.current(); }
      if (event.key === 'Tab') {
        const elements: HTMLElement[] = Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex="0"]') || []) as HTMLElement[];
        const first = elements[0], last = elements[elements.length - 1];
        if (event.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener('keydown', handler);
    const oldOverflow = document.body.style.overflow; document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', handler); document.body.style.overflow = oldOverflow; previous?.focus(); };
  }, [busy]);
  return createPortal(<div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4">
    <div ref={ref} tabIndex={-1} role="dialog" aria-modal="true" aria-label={title} className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-2xl outline-none">
      <div className="mb-5 flex items-center justify-between"><h2 className="text-lg font-bold">{title}</h2><button type="button" aria-label="关闭编辑弹窗" disabled={busy} onClick={onClose} className="rounded-full p-2"><X size={20} /></button></div>
      {children}
    </div>
  </div>, document.body);
};
