import { get, post } from '../lib/request';

export type KnowledgeScope = 'shared' | 'account' | 'item';
export interface KnowledgeDraft {
  scope: KnowledgeScope;
  cookie_id: string;
  item_id: string;
  topic: string;
  keywords: string;
  content: string;
  enabled: boolean;
  entry_type?: 'knowledge' | 'qa';
  match_mode?: 'exact' | 'contains' | 'hybrid';
  image_ids?: string[];
}
export interface KnowledgeEntry extends KnowledgeDraft {
  id: number;
  source: string;
  updated_at: string;
  chunk_index?: number;
}
export interface DocumentPreview { filename: string; topic: string; content: string; chunks: string[]; model_called: false }
export const previewDocument = (file: File) => post<DocumentPreview>('/ai-knowledge/documents/preview', file,
  { params: { filename: file.name }, headers: { 'Content-Type': 'application/octet-stream' } });
export const importDocument = (entry: KnowledgeDraft) => post<{ entry: KnowledgeEntry }>('/ai-knowledge/documents/import', entry);
export const listKnowledge = () => get<{ entries: KnowledgeEntry[] }>('/ai-knowledge');
export const saveKnowledge = (entry: KnowledgeDraft) => post<{ entry: KnowledgeEntry }>('/ai-knowledge', entry);
export const previewKnowledge = (cookie_id: string, item_id: string, message: string) =>
  post<{ entries: KnowledgeEntry[]; model_called: false }>('/ai-knowledge/preview', { cookie_id, item_id, message });

export const uploadReplyImage = (file: File) => post<{ id: string; width: number; height: number }>('/ai-knowledge/images', file,
  { headers: { 'Content-Type': 'application/octet-stream' } });
export const getReplyImage = (id: string) => get<Blob>(`/ai-knowledge/images/${id}`, undefined, { responseType: 'blob' });
