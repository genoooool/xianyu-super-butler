import React, { useEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, GripVertical, Zap } from 'lucide-react';
import { createQuickPhrase, deleteQuickPhrase, getQuickPhrases, updateQuickPhrase, getQuickPhraseGroups, saveQuickPhraseGroups } from '../services/api';
import { notify } from '../services/feedback';
import type { QuickPhrase } from '../types';
import { EnabledBadge, ReplyImage, ReplyImagePicker, ReplyModal } from './ReplyMedia';
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
  const [savedGroups, setSavedGroups] = useState<string[]>([]);
  const [newGroup, setNewGroup] = useState<string | null>(null);
  const [groupError, setGroupError] = useState('');
  const [editing, setEditing] = useState<QuickPhrase | null>(null);
  const [editUploading, setEditUploading] = useState(false);
  const [editError, setEditError] = useState('');
  const pointerDrag = useRef<{ id: number; x: number; y: number; active: boolean; target: HTMLElement | null } | null>(null);
  const categories = [...new Set(['默认', ...savedGroups, ...phrases.map(p => p.category)])];
  const visibleCategories = filter && categories.includes(filter) ? [filter] : categories;
  const load = async () => {
    const [next, groups] = await Promise.all([getQuickPhrases(true), getQuickPhraseGroups()]);
    setPhrases(next); setSavedGroups(groups); setError('');
  };
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
  const saveEdit = async () => {
    if (!editing || busyRef.current || editUploading) return;
    busyRef.current = true; setBusy(true); setEditError('');
    try {
      const previous = phrases.find(phrase => phrase.id === editing.id);
      await updateQuickPhrase(editing.id, {
        title: editing.title.trim(), content: editing.content, image_ids: editing.image_ids || [],
        category: editing.category,
        ...(previous?.category !== editing.category ? { sort_order: appendOrder(editing.category) } : {}),
      });
      await load(); setFilter(editing.category); setEditing(null);
    } catch (error) { setEditError(`保存未完成：${(error as Error).message}，编辑内容已保留，可重试。`); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const createGroup = async () => {
    const name = newGroup?.trim();
    if (!name || busyRef.current) return;
    if (categories.includes(name)) { setGroupError('这个分组已存在，请换一个名称'); return; }
    busyRef.current = true; setBusy(true);
    try {
      const groups = [...savedGroups, name];
      await saveQuickPhraseGroups(groups);
      setSavedGroups(groups); setForm(current => ({ ...current, category: name }));
      setFilter(name); setNewGroup(null);
    } catch { setGroupError('分组保存失败，请重试'); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const cancelDrag = () => { pointerDrag.current = null; setDragging(null); setDropTarget(null); };
  const pointerMove = (event: React.PointerEvent<HTMLButtonElement>) => {
    const drag = pointerDrag.current;
    if (!drag) return;
    if (!drag.active && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 5) return;
    drag.active = true; setDragging(drag.id);
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('[data-phrase-drop]') || null;
    drag.target = target;
    setDropTarget(target?.dataset.phraseDrop || null);
    let parent = event.currentTarget.parentElement;
    while (parent && parent.scrollHeight <= parent.clientHeight) parent = parent.parentElement;
    if (parent) {
      const bounds = parent.getBoundingClientRect();
      if (event.clientY < bounds.top + 40) parent.scrollTop -= 20;
      else if (event.clientY > bounds.bottom - 40) parent.scrollTop += 20;
    }
  };
  return <section className="section-panel" aria-busy={busy}>
    <SectionHeader title="快捷短语" description="按分组管理，拖动左侧手柄调整顺序；聊天中选择后仍需确认发送。" icon={Zap} />
    <div className="flex flex-wrap items-center gap-3 px-4 pt-4">
      <label className="flex items-center gap-2 text-sm text-[var(--text-muted)]">添加到分组
        <select aria-label="短语分组" value={form.category} disabled={disabled} onChange={e => setForm({ ...form, category: e.target.value })} className="ios-input min-w-40 rounded-md px-3 py-2.5">
          {categories.map(category => <option key={category}>{category}</option>)}
        </select>
      </label>
      <button type="button" disabled={disabled} onClick={() => { setNewGroup(''); setGroupError(''); }} className="ios-btn-secondary px-3 py-2 text-sm">＋ 新建分组</button>
    </div>
    <div className="grid gap-3 p-4 sm:grid-cols-[200px_1fr_auto]">
      <input aria-label="短语标题" value={form.title} maxLength={80} disabled={disabled} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="标题" className="ios-input rounded-md px-3 py-2.5" />
      <input aria-label="话术内容" value={form.content} maxLength={2000} disabled={disabled} onChange={e => setForm({ ...form, content: e.target.value })} placeholder="话术内容" className="ios-input rounded-md px-3 py-2.5" />
      <button type="button" onClick={add} disabled={disabled || !form.title.trim() || (!form.content.trim() && !form.image_ids.length)} className="ios-btn-primary px-4 py-2.5 text-sm disabled:opacity-50">添加</button>
    </div>
    {newGroup !== null && <ReplyModal title="新建分组" busy={busy} onClose={() => setNewGroup(null)}>
      <form onSubmit={event => { event.preventDefault(); void createGroup(); }} className="space-y-4">
        <label className="block text-sm">分组名称<input autoFocus aria-label="分组名称" maxLength={80} value={newGroup} disabled={busy} onChange={event => { setNewGroup(event.target.value); setGroupError(''); }} className="ios-input mt-2 w-full rounded-md px-3 py-2.5" placeholder="例如：售前咨询、售后服务" /></label>
        {groupError && <p role="alert" className="text-sm text-red-500">{groupError}</p>}
        <div className="flex justify-end gap-2"><button type="button" disabled={busy} onClick={() => setNewGroup(null)} className="ios-btn-secondary px-4 py-2">取消</button><button type="submit" disabled={busy || !newGroup.trim()} className="ios-btn-primary px-4 py-2">创建分组</button></div>
      </form>
    </ReplyModal>}
    {editing && <ReplyModal title="编辑快捷短语" busy={busy || editUploading} onClose={() => setEditing(null)}>
      <form className="space-y-4" onSubmit={event => { event.preventDefault(); void saveEdit(); }}>
        <label className="block text-sm">分组<select aria-label="编辑短语分组" value={editing.category} disabled={busy || editUploading} onChange={event => setEditing({ ...editing, category: event.target.value })} className="ios-input mt-2 w-full rounded-md px-3 py-2.5">{categories.map(category => <option key={category}>{category}</option>)}</select></label>
        <label className="block text-sm">标题<input aria-label="编辑短语标题" value={editing.title} maxLength={80} disabled={busy || editUploading} onChange={event => setEditing({ ...editing, title: event.target.value })} className="ios-input mt-2 w-full rounded-md px-3 py-2.5" /></label>
        <label className="block text-sm">话术内容<textarea aria-label="编辑话术内容" value={editing.content} maxLength={2000} rows={4} disabled={busy || editUploading} onChange={event => setEditing({ ...editing, content: event.target.value })} className="ios-input mt-2 w-full resize-y rounded-md px-3 py-2.5" /></label>
        <ReplyImagePicker ids={editing.image_ids || []} onChange={image_ids => setEditing(current => current && { ...current, image_ids })} disabled={busy} onBusy={setEditUploading} />
        {editError && <p role="alert" className="text-sm text-red-500">{editError}</p>}
        <div className="flex justify-end gap-2"><button type="button" disabled={busy || editUploading} onClick={() => setEditing(null)} className="ios-btn-secondary px-4 py-2">取消</button><button type="submit" disabled={busy || editUploading || !editing.title.trim() || (!editing.content.trim() && !editing.image_ids?.length)} className="ios-btn-primary px-4 py-2">保存修改</button></div>
      </form>
    </ReplyModal>}
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
        <h3 data-phrase-drop={`group:${category}`} data-drop-category={category}
          className={`px-4 py-3 text-sm font-semibold ${dropTarget === `group:${category}` ? 'bg-[var(--brand)] text-[var(--brand-ink)]' : 'bg-[var(--surface-subtle)] text-[var(--text)]'}`}>{category} <span className="ml-2 text-xs font-normal">{rows.length} 条</span></h3>
        {!rows.length && <p className="p-4 text-xs text-[var(--text-muted)]">分组已创建，可以添加短语或将已有短语移入。</p>}
        {rows.map((phrase, index) => <div key={phrase.id} data-phrase-id={phrase.id} data-phrase-drop={String(phrase.id)} data-drop-category={category}
          className={`flex flex-wrap items-center gap-3 border-t border-[var(--border)] px-4 py-3 ${dropTarget === String(phrase.id) ? 'bg-[var(--surface-hover)] ring-1 ring-inset ring-[var(--border-strong)]' : ''} ${dragging === phrase.id ? 'opacity-40' : ''}`}>
          <button type="button" disabled={disabled} aria-label={`拖动排序 ${phrase.title}`} title="拖动排序，也可使用上下移动按钮"
            onPointerDown={event => {
              if (event.button !== 0) return;
              event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId);
              pointerDrag.current = { id: phrase.id, x: event.clientX, y: event.clientY, active: false, target: null };
            }} onPointerMove={pointerMove}
            onPointerUp={event => {
              const drag = pointerDrag.current;
              cancelDrag(); event.currentTarget.releasePointerCapture(event.pointerId);
              if (drag?.active && drag.target) move(drag.id, drag.target.dataset.dropCategory!, drag.target.dataset.phraseId ? Number(drag.target.dataset.phraseId) : undefined);
            }} onPointerCancel={cancelDrag} onLostPointerCapture={cancelDrag}
            onKeyDown={event => { if (event.key === 'Escape') cancelDrag(); }}
            className="touch-none select-none cursor-grab p-2 text-[var(--text-soft)] active:cursor-grabbing"><GripVertical size={18} /></button>
          <div className="min-w-0 flex-1"><p className="break-words text-sm font-semibold text-[var(--text)]">{phrase.title}</p>
            <p className="line-clamp-2 break-words text-xs text-[var(--text-muted)]">{phrase.content}</p>
            {!!phrase.image_ids?.length && <div className="mt-2 flex flex-wrap gap-2">{phrase.image_ids.map(id => <ReplyImage key={id} id={id} compact />)}</div>}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <select aria-label={`移动分组 ${phrase.title}`} value={category} disabled={disabled} onChange={e => move(phrase.id, e.target.value)} className="ios-input max-w-32 rounded-md px-2 py-1 text-xs">{categories.map(value => <option key={value}>{value}</option>)}</select>
            <button type="button" aria-label={`上移 ${phrase.title}`} disabled={disabled || index === 0} onClick={() => move(phrase.id, category, rows[index - 1].id)} className="text-[var(--text-muted)] disabled:opacity-25"><ArrowUp size={15} /></button>
            <button type="button" aria-label={`下移 ${phrase.title}`} disabled={disabled || index === rows.length - 1} onClick={() => move(phrase.id, category, rows[index + 1].id)} className="text-[var(--text-muted)] disabled:opacity-25"><ArrowDown size={15} /></button>
            <EnabledBadge enabled={phrase.enabled} />
            <button type="button" disabled={disabled} aria-label={`编辑 ${phrase.title}`} onClick={() => { setEditing({ ...phrase, image_ids: [...(phrase.image_ids || [])] }); setEditError(''); }} className="text-xs text-[var(--text)] hover:underline">编辑</button>
            <button type="button" disabled={disabled} onClick={() => void mutate(() => updateQuickPhrase(phrase.id, { enabled: !phrase.enabled }))} className="text-xs text-[var(--text-muted)] hover:underline">{phrase.enabled ? '停用' : '启用'}</button>
            <button type="button" disabled={disabled} onClick={() => void mutate(() => deleteQuickPhrase(phrase.id))} className="text-xs text-red-500 hover:underline">删除</button>
          </div>
        </div>)}
      </section>;
    })}
  </section>;
}
