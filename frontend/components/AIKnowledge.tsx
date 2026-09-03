import React, { useEffect, useMemo, useState } from 'react';
import { BookOpen, Loader2, Plus, Search } from 'lucide-react';
import { getItems } from '../services/api';
import { Item } from '../types';
import { KnowledgeDraft, KnowledgeEntry, KnowledgeScope, listKnowledge, previewKnowledge, saveKnowledge } from '../services/knowledge';
import { notify } from '../services/feedback';
import { SectionHeader } from './ui';
import KnowledgeImport from './KnowledgeImport';

const names: Record<KnowledgeScope, string> = { shared: '共用资料', account: '当前店铺', item: '商品专属' };
const emptyDraft = { topic: '', keywords: '', content: '', enabled: true };

const AIKnowledge: React.FC<{ accountId: string }> = ({ accountId }) => {
  const [entries, setEntries] = useState<KnowledgeEntry[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [scope, setScope] = useState<KnowledgeScope>('account');
  const [itemId, setItemId] = useState('');
  const [draft, setDraft] = useState(emptyDraft);
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState(false);
  const [question, setQuestion] = useState('支持售后吗？');
  const [previewItemId, setPreviewItemId] = useState('');
  const [matches, setMatches] = useState<KnowledgeEntry[] | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([listKnowledge(), getItems()]).then(([knowledge, products]) => {
      if (!active) return;
      setEntries(knowledge.entries);
      setItems(products.filter(item => item.cookie_id === accountId));
    }).catch(error => {
      if (active) setLoadError(error instanceof Error ? error.message : '知识库加载失败');
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [accountId]);

  const visible = useMemo(() => entries.filter(entry => entry.scope === scope &&
    (scope === 'shared' || entry.cookie_id === accountId) && (scope !== 'item' || entry.item_id === itemId)),
  [entries, scope, accountId, itemId]);

  const reset = () => { setDraft(emptyDraft); setEditing(false); };
  const save = async (entry: KnowledgeDraft) => {
    setBusy(true);
    try {
      const result = await saveKnowledge(entry);
      setEntries(current => [result.entry, ...current.filter(row => row.id !== result.entry.id)]);
      setMatches(null);
      reset();
      notify('知识资料已保存，下次 AI 回复生效', 'success');
    } catch (error) {
      notify(error instanceof Error ? error.message : '保存失败', 'error');
    } finally { setBusy(false); }
  };

  const preview = async () => {
    setBusy(true);
    setMatches(null);
    try {
      const result = await previewKnowledge(accountId, previewItemId, question.trim());
      setMatches(result.entries);
    } catch (error) {
      notify(error instanceof Error ? error.message : '检索失败', 'error');
    } finally { setBusy(false); }
  };

  return (
    <section className="section-panel" aria-label="AI 知识库">
      <SectionHeader title="AI 知识库" description="商品专属优先于店铺，店铺优先于共用。先查资料，再交给模型组织回复。" icon={BookOpen} />
      <div className="space-y-5 p-5">
        <p className="text-sm text-gray-500">
          同一主题名称自动覆盖，例如三层都叫“售后期限”时只采用最高层。其他主题继续补充。
          共用仅限你自己的店铺；停用某条资料后恢复下层同主题规则。
          命中的资料会发给你配置的 AI 服务，请勿填写密码、卡密或买家隐私。
        </p>
        {loading ? <p role="status">正在加载知识资料…</p> : loadError ? <p role="alert" className="text-red-600">{loadError}</p> : <>
          <div className="grid gap-3 sm:grid-cols-2">
            <label><span className="field-label">资料范围</span>
              <select aria-label="资料范围" value={scope} disabled={busy} className="ios-input w-full rounded-md px-3 py-2"
                onChange={event => { setScope(event.target.value as KnowledgeScope); reset(); }}>
                {Object.entries(names).map(([value, name]) => <option key={value} value={value}>{name}</option>)}
              </select>
            </label>
            {scope === 'item' && <label><span className="field-label">所属商品</span>
              <select aria-label="知识所属商品" value={itemId} disabled={busy} className="ios-input w-full rounded-md px-3 py-2"
                onChange={event => { setItemId(event.target.value); reset(); }}>
                <option value="">请选择当前店铺的商品</option>
                {items.map(item => <option key={item.item_id} value={item.item_id}>{item.item_title || item.item_id}</option>)}
              </select>
            </label>}
          </div>
          <KnowledgeImport key={`${accountId}:${scope}:${itemId}`}
            target={{ scope, cookie_id: scope === 'shared' ? '' : accountId, item_id: scope === 'item' ? itemId : '' }}
            label={`${names[scope]}${scope === 'item' ? ' · ' + (items.find(item => item.item_id === itemId)?.item_title || itemId) : ''}`}
            disabled={busy || (scope === 'item' && !itemId)} onBusy={setBusy} onImported={entry => {
              setEntries(current => [entry, ...current.filter(row => row.id !== entry.id)]); setMatches(null);
              notify('文档已导入，可编辑或停用', 'success');
            }} />
          <div className="space-y-2" aria-label="知识条目">
            {visible.length === 0 && <p className="text-sm text-gray-500">这个范围暂无资料。可在下方添加，或从较通用范围建立同主题规则。</p>}
            {visible.map(entry => <div key={entry.id} className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-gray-200 p-3">
              <div className="min-w-0 flex-1"><p className="break-words font-semibold">{entry.topic} <span className="text-xs font-normal text-gray-500">{entry.enabled ? '已启用' : '已停用'}</span></p>
                <p className="mt-1 line-clamp-2 break-words text-sm text-gray-500">{entry.content}</p>
              </div>
              <button type="button" disabled={busy} className="ios-btn-secondary px-3 py-1.5 text-sm" onClick={() => {
                setDraft({ topic: entry.topic, keywords: entry.keywords, content: entry.content, enabled: entry.enabled }); setEditing(true);
              }} aria-label={`编辑 ${entry.topic}`}>编辑</button>
              <button type="button" disabled={busy} className="ios-btn-secondary px-3 py-1.5 text-sm"
                onClick={() => save({ ...entry, enabled: !entry.enabled })} aria-label={`${entry.enabled ? '停用' : '启用'} ${entry.topic}`}>{entry.enabled ? '停用' : '启用'}</button>
            </div>)}
          </div>
          <form className="space-y-3 rounded-md border border-gray-200 p-4" onSubmit={event => {
            event.preventDefault();
            save({ ...draft, scope, cookie_id: scope === 'shared' ? '' : accountId, item_id: scope === 'item' ? itemId : '' });
          }}>
            <div className="flex items-center justify-between"><h3 className="font-bold">{editing ? '编辑资料' : '添加资料'}</h3>
              {editing && <button type="button" disabled={busy} onClick={reset} className="flex items-center gap-1 text-sm"><Plus size={15} />新建另一主题</button>}
            </div>
            <label className="block"><span className="field-label">主题名称（相同名称才能覆盖）</span>
              <input aria-label="知识主题" required maxLength={80} disabled={busy || editing} className="ios-input w-full rounded-md px-3 py-2" value={draft.topic}
                placeholder="例如：售后期限、使用方法、发货时间" onChange={event => setDraft({ ...draft, topic: event.target.value })} />
            </label>
            <label className="block"><span className="field-label">触发词 / 常见问法（用逗号或换行分隔）</span>
              <textarea aria-label="知识触发词" maxLength={300} disabled={busy} className="ios-input w-full rounded-md px-3 py-2" value={draft.keywords}
                placeholder="例如：售后, 能退吗, 退款, 用不了" onChange={event => setDraft({ ...draft, keywords: event.target.value })} />
            </label>
            <label className="block"><span className="field-label">确定可对买家说明的内容</span>
              <textarea aria-label="知识内容" required maxLength={60000} disabled={busy} className="ios-input min-h-32 w-full rounded-md px-3 py-2" value={draft.content}
                placeholder="填写问答或真实规则，不要填卡密、密码或未确认的承诺。最多60000字，长文按段检索。" onChange={event => setDraft({ ...draft, content: event.target.value })} />
            </label>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={busy} checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用这条资料</label>
            <button type="submit" disabled={busy || !draft.topic.trim() || !draft.content.trim() || (scope === 'item' && !itemId)} className="ios-btn-primary px-4 py-2 text-sm">{busy ? '处理中…' : '保存知识资料'}</button>
          </form>
          <div className="space-y-3 border-t border-gray-200 pt-4">
            <h3 className="font-bold">检索预览</h3>
            <p className="text-sm text-gray-500">检查这个问题会采用哪些资料，不调用模型、不发送消息。按主题和触发词匹配，最多选6条；不是全文语义搜索。</p>
            <select aria-label="预览商品" value={previewItemId} disabled={busy} className="ios-input w-full rounded-md px-3 py-2"
              onChange={event => { setPreviewItemId(event.target.value); setMatches(null); }}>
              <option value="">仅预览店铺与共用资料</option>
              {items.map(item => <option key={item.item_id} value={item.item_id}>{item.item_title || item.item_id}</option>)}
            </select>
            <div className="flex gap-2"><input aria-label="知识测试问题" maxLength={2000} className="ios-input min-w-0 flex-1 rounded-md px-3 py-2" value={question}
              onChange={event => { setQuestion(event.target.value); setMatches(null); }} />
              <button type="button" disabled={busy || !question.trim()} onClick={preview} className="ios-btn-secondary flex items-center gap-1 px-3 py-2 text-sm">
                {busy ? <Loader2 size={16} className="animate-spin" /> : <Search size={16} />}检索预览</button>
            </div>
            {matches?.length === 0 && <p role="status" className="text-sm text-amber-700">未匹配到资料。可补充主题或触发词；这不代表模型已知道答案。</p>}
            {matches && matches.length > 0 && <div aria-label="检索结果" className="space-y-2">{matches.map((entry, index) => <div key={`${entry.id}:${index}`} className="rounded-md bg-gray-50 p-3">
              <p className="font-semibold">{entry.topic} <span className="text-xs text-gray-500">来源：{entry.source}</span></p>
              <p className="mt-1 whitespace-pre-wrap break-words text-sm">{entry.content}</p>
            </div>)}</div>}
          </div>
        </>}
      </div>
    </section>
  );
};

export default AIKnowledge;
