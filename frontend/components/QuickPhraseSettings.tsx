import React, { useEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, GripVertical, Zap } from 'lucide-react';
import { createQuickPhrase, deleteQuickPhrase, getQuickPhrases, updateQuickPhrase } from '../services/api';
import { notify } from '../services/feedback';
import type { QuickPhrase } from '../types';
import { EnabledBadge, ReplyImage, ReplyImagePicker } from './ReplyMedia';
import { SectionHeader } from './ui';

export default function QuickPhraseSettings() {
  const [phrases, setPhrases] = useState<QuickPhrase[]>([]);
  const [form, setForm] = useState({ category: '默认', title: '', content: '', image_ids: [] as string[] });
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('');
  const [dragging, setDragging] = useState<number | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const categories = [...new Set(phrases.map(p => p.category))];
  const visibleCategories = filter && categories.includes(filter) ? [filter] : categories;
  const load = async () => { const next = await getQuickPhrases(true); setPhrases(next); setError(''); };
  useEffect(() => { void load().catch(() => setError('快捷短语加载失败，请重试')); }, []);

  const mutate = async (work: () => Promise<unknown>) => {
    if (busyRef.current || uploading) return;
    busyRef.current = true; setBusy(true);
    try { await work(); await load(); }
    catch (error) {
      notify(`保存未完成：${(error as Error).message}。请核对刷新后的列表再重试。`, 'error');
      await load().catch(() => setError('无法读取最新顺序，请重试后再操作'));
    } finally { busyRef.current = false; setBusy(false); }
  };
  const appendOrder = (category: string) => Math.max(0, ...phrases.filter(p => p.category === category).map(p => p.sort_order)) + 1024;
  const add = () => void mutate(async () => {
    const category = form.category.trim() || '默认';
    await createQuickPhrase(form.title.trim(), form.content, category, appendOrder(category), form.image_ids);
    setForm(current => ({ ...current, category, title: '', content: '', image_ids: [] }));
    setFilter(category);
  });
  const move = (id: number, category: string, targetId?: number) => {
    setDragging(null); setDropTarget(null);
    const phrase = phrases.find(p => p.id === id);
    if (!phrase || targetId === id || busyRef.current || error) return;
    const group = phrases.filter(p => p.category === category);
    const previousIndex = group.findIndex(p => p.id === id);
    const targetIndex = targetId === undefined ? group.length : group.findIndex(p => p.id === targetId);
    if (targetIndex < 0) return;
    const next = group.filter(p => p.id !== id);
    next.splice(Math.min(targetIndex, next.length), 0, { ...phrase, category });
    if (previousIndex >= 0 && group.every((p, i) => p.id === next[i]?.id)) return;
    void mutate(async () => {
      // Existing owner-scoped API persists the actual order; on partial failure reload it.
      for (const [index, row] of next.entries()) {
        const sort_order = (index + 1) * 1024;
        const original = phrases.find(p => p.id === row.id)!;
        if (original.category !== category || original.sort_order !== sort_order) await updateQuickPhrase(row.id, { category, sort_order });
      }
    });
  };
  const disabled = busy || uploading || Boolean(error);
  return <section className="section-panel" aria-busy={busy}>
    <SectionHeader title="快捷短语" description="按分组管理，拖动左侧手柄调整顺序；聊天中选择后仍需确认发送。" icon={Zap} />
    <div className="grid gap-3 p-4 sm:grid-cols-[160px_200px_1fr_auto]">
      <input aria-label="短语分组" list="quick-phrase-groups" value={form.category} maxLength={80} disabled={disabled}
        onChange={e => setForm({ ...form, category: e.target.value })} placeholder="选择或输入新分组" className="ios-input rounded-md px-3 py-2.5" />
      <datalist id="quick-phrase-groups">{categories.map(category => <option key={category} value={category} />)}</datalist>
      <input aria-label="短语标题" value={form.title} maxLength={80} disabled={disabled} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="标题" className="ios-input rounded-md px-3 py-2.5" />
      <input aria-label="话术内容" value={form.content} maxLength={2000} disabled={disabled} onChange={e => setForm({ ...form, content: e.target.value })} placeholder="话术内容" className="ios-input rounded-md px-3 py-2.5" />
      <button type="button" onClick={add} disabled={disabled || !form.title.trim() || (!form.content.trim() && !form.image_ids.length)} className="ios-btn-primary px-4 py-2.5 text-sm disabled:opacity-50">添加</button>
    </div>
    <p className="px-4 pb-3 text-xs text-[var(--text-muted)]">输入新分组名称并添加短语，即可创建分组。也可拖到其他分组标题下。</p>
    <div className="px-4 pb-4"><ReplyImagePicker ids={form.image_ids} onChange={image_ids => setForm(current => ({ ...current, image_ids }))} disabled={disabled} onBusy={setUploading} /></div>
    <div className="flex flex-wrap gap-2 border-t border-[var(--border)] p-4" aria-label="筛选短语分组">
      {['', ...categories].map(category => <button key={category} type="button" aria-pressed={filter === category} onClick={() => setFilter(category)}
        className={`rounded-full px-3 py-1.5 text-xs ${filter === category ? 'bg-[var(--brand)] text-[var(--brand-ink)]' : 'bg-[var(--surface-strong)] text-[var(--text-muted)]'}`}>
        {category || '全部'} · {category ? phrases.filter(p => p.category === category).length : phrases.length}
      </button>)}
      {busy && <span role="status" className="self-center text-xs text-[var(--text-muted)]">正在保存…</span>}
    </div>
    {error && <p role="alert" className="p-4 text-sm text-red-500">{error} <button onClick={() => void load().catch(() => undefined)}>重新加载</button></p>}
    {!phrases.length && !error && <p className="p-6 text-center text-sm text-[var(--text-muted)]">还没有快捷短语</p>}
    {visibleCategories.map(category => {
      const rows = phrases.filter(p => p.category === category);
      return <section key={category} aria-label={`分组 ${category}`} className="border-t border-[var(--border)]">
        <h3 onDragOver={e => { if (dragging !== null && !disabled) { e.preventDefault(); setDropTarget(`group:${category}`); } }}
          onDrop={e => { e.preventDefault(); if (dragging !== null) move(dragging, category); }}
          className={`px-4 py-3 text-sm font-semibold ${dropTarget === `group:${category}` ? 'bg-[var(--brand)] text-[var(--brand-ink)]' : 'bg-[var(--surface-subtle)] text-[var(--text)]'}`}>{category} <span className="ml-2 text-xs font-normal">{rows.length} 条</span></h3>
        {rows.map((phrase, index) => <div key={phrase.id} data-phrase-id={phrase.id}
          onDragOver={e => { if (dragging !== null && !disabled) { e.preventDefault(); setDropTarget(String(phrase.id)); } }}
          onDrop={e => { e.preventDefault(); if (dragging !== null) move(dragging, category, phrase.id); }}
          className={`flex flex-wrap items-center gap-3 border-t border-[var(--border)] px-4 py-3 ${dropTarget === String(phrase.id) ? 'bg-[var(--surface-hover)] ring-1 ring-inset ring-[var(--border-strong)]' : ''} ${dragging === phrase.id ? 'opacity-40' : ''}`}>
          <button type="button" draggable={!disabled} disabled={disabled} aria-label={`拖动排序 ${phrase.title}`} title="拖动排序，也可使用上下移动按钮"
            onDragStart={e => { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(phrase.id)); setDragging(phrase.id); }}
            onDragEnd={() => { setDragging(null); setDropTarget(null); }} className="cursor-grab text-[var(--text-soft)] active:cursor-grabbing"><GripVertical size={18} /></button>
          <div className="min-w-0 flex-1"><p className="break-words text-sm font-semibold text-[var(--text)]">{phrase.title}</p>
            <p className="line-clamp-2 break-words text-xs text-[var(--text-muted)]">{phrase.content}</p>
            {!!phrase.image_ids?.length && <div className="mt-2 flex flex-wrap gap-2">{phrase.image_ids.map(id => <ReplyImage key={id} id={id} compact />)}</div>}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <select aria-label={`移动分组 ${phrase.title}`} value={category} disabled={disabled} onChange={e => move(phrase.id, e.target.value)} className="ios-input max-w-32 rounded-md px-2 py-1 text-xs">{categories.map(value => <option key={value}>{value}</option>)}</select>
            <button type="button" aria-label={`上移 ${phrase.title}`} disabled={disabled || index === 0} onClick={() => move(phrase.id, category, rows[index - 1].id)} className="text-[var(--text-muted)] disabled:opacity-25"><ArrowUp size={15} /></button>
            <button type="button" aria-label={`下移 ${phrase.title}`} disabled={disabled || index === rows.length - 1} onClick={() => move(phrase.id, category, rows[index + 1].id)} className="text-[var(--text-muted)] disabled:opacity-25"><ArrowDown size={15} /></button>
            <EnabledBadge enabled={phrase.enabled} />
            <button type="button" disabled={disabled} onClick={() => void mutate(() => updateQuickPhrase(phrase.id, { enabled: !phrase.enabled }))} className="text-xs text-[var(--text-muted)] hover:underline">{phrase.enabled ? '停用' : '启用'}</button>
            <button type="button" disabled={disabled} onClick={() => void mutate(() => deleteQuickPhrase(phrase.id))} className="text-xs text-red-500 hover:underline">删除</button>
          </div>
        </div>)}
      </section>;
    })}
  </section>;
}
