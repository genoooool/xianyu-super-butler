import React, { useState } from 'react';
import { DocumentPreview, KnowledgeDraft, KnowledgeEntry, importDocument, previewDocument } from '../services/knowledge';

const KnowledgeImport: React.FC<{
  target: Pick<KnowledgeDraft, 'scope' | 'cookie_id' | 'item_id'>;
  label: string; disabled: boolean; onBusy: (busy: boolean) => void; onImported: (entry: KnowledgeEntry) => void;
}> = ({ target, label, disabled, onBusy, onImported }) => {
  const [preview, setPreview] = useState<DocumentPreview | null>(null);
  const [topic, setTopic] = useState('');
  const [keywords, setKeywords] = useState('');
  const [error, setError] = useState('');
  const errorText = (err: any) => typeof err?.response?.data?.detail === 'string' ? err.response.data.detail : '处理失败，请重试';
  return <div className="space-y-3 rounded-md border border-gray-200 p-4" aria-label="导入知识文档">
    <h3 className="font-bold">导入 TXT / Markdown</h3>
    <p className="text-sm text-gray-500">UTF-8 文本，单文件最多256KB、60000字。先预览，再确认；保存为可编辑资料，不另存原文件。长文按段检索，不会整篇发送给模型。</p>
    <label className="block"><span className="field-label">选择知识文档</span>
      <input aria-label="选择知识文档" type="file" accept=".txt,.md" disabled={disabled} className="block w-full min-w-0 text-sm"
        onChange={async event => {
          const file = event.target.files?.[0]; event.target.value = '';
          if (!file) return;
          setError(''); setPreview(null);
          if (file.size > 256 * 1024) { setError('文件不能超过256KB'); return; }
          onBusy(true);
          try { const result = await previewDocument(file); setPreview(result); setTopic(result.topic); setKeywords(''); }
          catch (err) { setError(errorText(err)); }
          finally { onBusy(false); }
        }} />
    </label>
    {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    {preview && <div className="space-y-3" aria-label="文档导入预览">
      <p className="text-sm">{preview.filename} · {preview.content.length}字 · {preview.chunks.length}段 · 将保存到：{label}</p>
      <label className="block"><span className="field-label">文档主题（同名主题按范围整体覆盖）</span>
        <input aria-label="文档主题" value={topic} maxLength={80} disabled={disabled} onChange={event => setTopic(event.target.value)} className="ios-input w-full rounded-md px-3 py-2" />
      </label>
      <label className="block"><span className="field-label">常见问法 / 触发词（可选）</span>
        <input aria-label="文档触发词" value={keywords} maxLength={300} disabled={disabled} onChange={event => setKeywords(event.target.value)} className="ios-input w-full rounded-md px-3 py-2" />
      </label>
      <div className="max-h-72 space-y-2 overflow-y-auto rounded-md bg-gray-50 p-3" aria-label="文档分段">
        {preview.chunks.map((chunk, index) => <div key={index}><p className="text-xs text-gray-500">第{index + 1}段</p><pre className="whitespace-pre-wrap break-words font-sans text-sm">{chunk}</pre></div>)}
      </div>
      <p className="text-xs text-gray-500">确认后立即启用，供下次 AI 回复检索。请确认内容真实且不含密码、卡密或买家隐私；同范围同名资料不会被导入覆盖。</p>
      <div className="flex gap-2">
        <button type="button" disabled={disabled || !topic.trim()} className="ios-btn-primary px-4 py-2 text-sm" onClick={async () => {
          setError(''); onBusy(true);
          try { const result = await importDocument({ ...target, topic: topic.trim(), keywords, content: preview.content, enabled: true }); onImported(result.entry); setPreview(null); }
          catch (err) { setError(errorText(err)); }
          finally { onBusy(false); }
        }}>确认导入并启用</button>
        <button type="button" disabled={disabled} className="ios-btn-secondary px-4 py-2 text-sm" onClick={() => { setPreview(null); setError(''); }}>取消导入</button>
      </div>
    </div>}
  </div>;
};

export default KnowledgeImport;
