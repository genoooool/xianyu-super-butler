import React, { useEffect, useState } from 'react';
import { Plus, MessageSquare, Key, Bot } from 'lucide-react';
import { getAccountDetails, getItems, getDefaultReply, updateDefaultReply, getReplyRules } from '../services/api';
import { AccountDetail, Item } from '../types';
import { KnowledgeDraft, KnowledgeEntry, listKnowledge, createFixedReply, updateFixedReply, deleteFixedReply } from '../services/knowledge';
import { confirmAction, notify } from '../services/feedback';
import { PageHeader, PageTabs } from './ui';
import { EnabledBadge, ReplyImage, ReplyImagePicker, ReplyModal } from './ReplyMedia';

type Tab = 'keyword' | 'intent' | 'default';
const names = { shared: '共用', account: '店铺', item: '商品专属' };
const fresh = (account: string, tab: Tab): KnowledgeDraft => ({ scope: 'account', cookie_id: account, item_id: '', item_ids: [], topic: '', keywords: '', content: '', enabled: true, entry_type: 'qa', match_mode: tab === 'keyword' ? 'exact' : 'hybrid', image_ids: [] });
const productIds = (entry: KnowledgeDraft) => entry.item_ids?.length ? entry.item_ids : entry.item_id ? [entry.item_id] : [];
const errorDetail = (error: unknown, fallback: string) => {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' ? detail : fallback;
};

const AutoReplies: React.FC = () => {
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [account, setAccount] = useState('');
  const [items, setItems] = useState<Item[]>([]);
  const [entries, setEntries] = useState<KnowledgeEntry[]>([]);
  const [tab, setTab] = useState<Tab>('intent');
  const [draft, setDraft] = useState<KnowledgeDraft | null>(null);
  const [original, setOriginal] = useState('');
  const [editing, setEditing] = useState<KnowledgeEntry | null>(null);
  const [productSearch, setProductSearch] = useState('');
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [legacyCount, setLegacyCount] = useState(0);
  const [fallback, setFallback] = useState({ enabled: false, reply_content: '', reply_once: false, reply_image_url: '' });
  useEffect(() => {
    Promise.all([getAccountDetails(), getItems(), listKnowledge()]).then(([shops, products, knowledge]) => {
      setAccounts(shops); setItems(products); setEntries(knowledge.entries);
      const saved = localStorage.getItem('reply_account'); setAccount(shops.some(s => s.id === saved) ? saved! : shops[0]?.id || '');
    }).catch(() => setError('加载失败，请重新打开页面'));
  }, []);
  useEffect(() => {
    if (!account) return;
    let active = true; localStorage.setItem('reply_account', account);
    Promise.all([getDefaultReply(account), getReplyRules(account)]).then(([value, legacy]) => {
      if (active) { setFallback({ enabled: false, reply_content: '', reply_once: false, reply_image_url: '', ...value }); setLegacyCount(legacy.length); }
    }).catch(() => { if (active) setError('默认回复加载失败'); });
    return () => { active = false; };
  }, [account]);
  const all = entries.filter(r => r.entry_type === 'qa' && (r.scope === 'shared' || r.cookie_id === account));
  const visible = all.filter(r => tab === 'intent' ? r.match_mode === 'hybrid' : r.match_mode !== 'hybrid');
  const open = (value: KnowledgeDraft, target: KnowledgeEntry | null = null) => {
    const next = { ...value, item_ids: productIds(value) };
    setDraft(next); setOriginal(JSON.stringify(next)); setEditing(target); setProductSearch('');
  };
  const close = async () => {
    if (busy || uploading) return;
    if (JSON.stringify(draft) !== original && !await confirmAction('内容还没有保存，确定关闭吗？')) return;
    setDraft(null);
  };
  const save = async (value: KnowledgeDraft, target = editing) => {
    setBusy(true);
    try {
      const result = target ? await updateFixedReply(target.id, target.revision, value) : await createFixedReply(value);
      setEntries(current => [result.entry, ...current.filter(row => row.id !== result.entry.id)]);
      setDraft(null); setEditing(null);
      notify('固定回复已保存，无需再点AI配置保存', 'success');
    }
    catch (error) { notify(errorDetail(error, '保存失败，请检查主题、答案和图片是否完整'), 'error'); }
    finally { setBusy(false); }
  };
  const remove = async (row: KnowledgeEntry) => {
    setBusy(true);
    try {
      const range = row.scope === 'item' ? `所选 ${productIds(row).length} 个商品` : names[row.scope];
      if (!await confirmAction(`删除“${row.topic}”后，${range}将不再使用这条回复；可能改用店铺或共用的同主题规则。确定删除吗？`,
        { title: '删除固定回复', confirmLabel: '确认删除' })) return;
      await deleteFixedReply(row.id, row.revision);
      setEntries(current => current.filter(entry => entry.id !== row.id));
      notify('固定回复已删除', 'success');
    } catch (error) { notify(errorDetail(error, '删除失败，请刷新后再试'), 'error'); }
    finally { setBusy(false); }
  };
  const selected = draft ? productIds(draft) : [];
  const accountItems = items.filter(item => item.cookie_id === account);
  const choices = [...accountItems, ...selected.filter(id => !accountItems.some(item => item.item_id === id))
    .map(id => ({ cookie_id: account, item_id: id, item_title: `未同步或已移除的商品 · ${id}` }))]
    .filter(item => `${item.item_title || ''} ${item.item_id}`.toLowerCase().includes(productSearch.trim().toLowerCase()));
  const selectProduct = (id: string, checked: boolean) => {
    if (!draft) return;
    const ids = checked ? [...new Set([...selected, id])].sort() : selected.filter(value => value !== id);
    setDraft({ ...draft, item_ids: ids, item_id: ids[0] || '' });
  };
  return <div className="page-stack">
    <PageHeader title="自动回复" description="关键词或 AI 意图匹配后，原样发送固定答案。知识库在 AI 回复页单独管理。" icon={MessageSquare} />
    {error && <p role="alert" className="text-red-600">{error}</p>}
    <div className="flex flex-wrap items-center gap-3">
      <label className="min-w-56"><span className="field-label">当前店铺</span><select aria-label="回复店铺" value={account} disabled={!!draft || busy} onChange={e => setAccount(e.target.value)} className="ios-input w-full px-3 py-2 rounded-md">
        {accounts.map(a => <option key={a.id} value={a.id}>{a.nickname || a.remark || a.id}</option>)}</select></label>
      {tab !== 'default' && <button disabled={!account || busy} className="ios-btn-primary mt-5 flex items-center gap-2 px-4 py-2" onClick={() => open(fresh(account, tab))}><Plus size={16} />{tab === 'keyword' ? '添加关键词回复' : '添加意图回复'}</button>}
    </div>
    <PageTabs value={tab} onChange={setTab} ariaLabel="自动回复类型" items={[
      { id: 'keyword', label: '关键词回复', icon: Key, count: all.filter(r => r.match_mode !== 'hybrid').length },
      { id: 'intent', label: '意图回复 QA', icon: Bot, count: all.filter(r => r.match_mode === 'hybrid').length },
      { id: 'default', label: '默认回复' }]} />
    {tab !== 'default' ? <section className="section-panel p-4 space-y-3">
      <p className="text-sm text-[var(--text-muted)]">显示当前店铺的全部范围，包含商品专属与共用，不会因重新打开而隐藏商品条目。{tab === 'intent' && '常见问法先直接匹配；同义表达需在AI回复页启用模型。'}</p>
      {!!legacyCount && <p className="text-amber-600 text-sm">此账号还有 {legacyCount} 条旧关键词规则；保留原数据，固定 QA 优先处理。</p>}
      {visible.length === 0 && <p className="py-6 text-center text-[var(--text-muted)]">暂无{tab === 'keyword' ? '关键词' : '意图'}回复</p>}
      {visible.map(row => <article key={row.id} className="rounded-xl border border-[var(--border)] p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">{row.topic}</h3><EnabledBadge enabled={row.enabled} /><span className="break-words text-xs text-[var(--text-muted)]">{names[row.scope]}{row.scope === 'item' && ` · ${productIds(row).length} 个商品`}</span></div>
        {row.scope === 'item' && <p className="break-words text-xs text-[var(--text-muted)]">{productIds(row).map(id => items.find(i => i.cookie_id === row.cookie_id && i.item_id === id)?.item_title || id).join('、')}</p>}
        <p className="text-sm text-[var(--text-muted)]">{row.keywords || row.topic}</p>
        {row.content && <p className="whitespace-pre-wrap break-words text-sm">{row.content}</p>}
        <div className="flex flex-wrap gap-2">{row.image_ids?.map(id => <ReplyImage key={id} id={id} />)}</div>
        <div className="flex flex-wrap gap-2"><button disabled={busy} onClick={() => open(row, row)} className="ios-btn-secondary px-3 py-1.5 text-sm" aria-label={`编辑 ${row.topic}`}>编辑</button>
          <button disabled={busy} onClick={() => void save({ ...row, enabled: !row.enabled }, row)} className="ios-btn-secondary px-3 py-1.5 text-sm" aria-label={`${row.enabled ? '停用' : '启用'} ${row.topic}`}>{row.enabled ? '停用' : '启用'}</button>
          <button disabled={busy} onClick={() => void remove(row)} className="ios-btn-secondary px-3 py-1.5 text-sm text-red-600" aria-label={`删除 ${row.topic}`}>删除</button></div>
      </article>)}
    </section> : <section className="section-panel p-5 space-y-3">
      <p className="text-sm text-[var(--text-muted)]">仅在未启用 AI 且没有匹配固定回复时使用。AI 分类含糊、出错或发送被拦截时，不会用默认回复绕过。</p>
      <label className="flex items-center gap-2"><input type="checkbox" checked={fallback.enabled} onChange={e => setFallback({ ...fallback, enabled: e.target.checked })} />启用默认回复<EnabledBadge enabled={fallback.enabled} /></label>
      <textarea aria-label="默认回复内容" className="ios-input w-full min-h-28 rounded-md p-3" value={fallback.reply_content} onChange={e => setFallback({ ...fallback, reply_content: e.target.value })} />
      <button className="ios-btn-primary px-4 py-2" disabled={busy || !account} onClick={async () => { setBusy(true); try { await updateDefaultReply(account, fallback); notify('默认回复已保存', 'success'); } catch { notify('保存失败', 'error'); } finally { setBusy(false); } }}>保存默认回复</button>
    </section>}
    {draft && <ReplyModal title={editing ? '编辑固定回复' : tab === 'keyword' ? '添加关键词回复' : '添加意图回复'} onClose={() => void close()} busy={busy || uploading}>
      <form className="space-y-4" onSubmit={e => { e.preventDefault(); void save(draft); }}>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label><span className="field-label">适用范围</span><select aria-label="回复范围" disabled={busy || uploading} value={draft.scope} className="ios-input w-full rounded-md p-2" onChange={e => setDraft({ ...draft, scope: e.target.value as KnowledgeDraft['scope'], cookie_id: e.target.value === 'shared' ? '' : account, item_id: '', item_ids: [] })}>{Object.entries(names).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
          <label><span className="field-label">匹配方式</span><select aria-label="匹配方式" disabled={busy} value={draft.match_mode} className="ios-input w-full rounded-md p-2" onChange={e => setDraft({ ...draft, match_mode: e.target.value as KnowledgeDraft['match_mode'] })}><option value="exact">完整问法匹配</option><option value="contains">包含关键词（可能误触发）</option><option value="hybrid">常见问法 + AI 意图识别</option></select></label>
        </div>
        {draft.scope === 'shared' && <p className="text-sm text-amber-600">共用会应用到你在此工作台下的所有店铺。商品专属与店铺同主题回复仍优先。</p>}
        {draft.scope === 'item' && <fieldset className="space-y-2" aria-label="回复所属商品" disabled={busy || uploading}>
          <legend className="field-label">所属商品（可多选） · 已选 {selected.length}/200</legend>
          <input aria-label="搜索回复商品" value={productSearch} onChange={event => setProductSearch(event.target.value)} placeholder="搜索当前店铺的商品名称或编号" className="ios-input w-full rounded-md p-2" />
          <div className="max-h-48 space-y-1 overflow-y-auto rounded-lg border border-[var(--border)] p-2">
            {choices.map(item => <label key={item.item_id} className="flex cursor-pointer items-start gap-2 rounded-md p-2 hover:bg-[var(--surface-secondary)]">
              <input type="checkbox" aria-label={`选择商品 ${item.item_title || item.item_id}`} checked={selected.includes(item.item_id)} disabled={!selected.includes(item.item_id) && selected.length >= 200}
                onChange={event => selectProduct(item.item_id, event.target.checked)} className="mt-1 shrink-0" />
              <span className="min-w-0 break-words text-sm">{item.item_title || item.item_id}<span className="block text-xs text-[var(--text-muted)]">{item.item_id}</span></span>
            </label>)}
            {!choices.length && <p className="p-2 text-sm text-[var(--text-muted)]">没有找到商品，请先同步当前店铺的商品或调整搜索。</p>}
          </div>
          <p className="text-xs text-[var(--text-muted)]">一条回复对应所选商品，修改后统一生效；未选商品和其他店铺不会使用这条规则。</p>
        </fieldset>}
        <label className="block"><span className="field-label">问题意图 / 主题</span><input aria-label="QA问题意图" required maxLength={80} disabled={busy} value={draft.topic} onChange={e => setDraft({ ...draft, topic: e.target.value })} placeholder="例如：客户询价、打招呼、询问完成时间" className="ios-input w-full rounded-md p-3" /></label>
        <label className="block"><span className="field-label">关键词 / 常见问法（用逗号或换行分隔）</span><textarea aria-label="QA常见问法" maxLength={300} disabled={busy} value={draft.keywords} onChange={e => setDraft({ ...draft, keywords: e.target.value })} placeholder="多少钱，怎么收费，10元多少个" className="ios-input w-full rounded-md p-3" /></label>
        <label className="block"><span className="field-label">直接发送的答案（可留空只发图片）</span><textarea aria-label="QA固定答案" maxLength={2000} disabled={busy} value={draft.content} onChange={e => setDraft({ ...draft, content: e.target.value })} placeholder="点开商品购买页面下面就有对应的价格哦亲。" className="ios-input min-h-28 w-full rounded-md p-3" /></label>
        <ReplyImagePicker ids={draft.image_ids || []} disabled={busy} onBusy={setUploading} onChange={image_ids => setDraft(current => current ? { ...current, image_ids } : current)} />
        <label className="flex items-center gap-2"><input type="checkbox" disabled={busy} checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />启用这条回复<EnabledBadge enabled={draft.enabled} /></label>
        <div className="flex justify-end gap-3"><button type="button" disabled={busy || uploading} onClick={() => void close()} className="ios-btn-secondary px-4 py-2">取消</button><button type="submit" disabled={busy || uploading || !draft.topic.trim() || (!draft.content.trim() && !draft.image_ids?.length) || (draft.scope === 'item' && !selected.length)} className="ios-btn-primary px-4 py-2">{busy ? '保存中…' : '保存固定回复'}</button></div>
      </form>
    </ReplyModal>}
  </div>;
};
export default AutoReplies;
