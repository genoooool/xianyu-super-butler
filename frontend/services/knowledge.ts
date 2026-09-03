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
}
export interface KnowledgeEntry extends KnowledgeDraft {
  id: number;
  source: string;
  updated_at: string;
}
export const listKnowledge = () => get<{ entries: KnowledgeEntry[] }>('/ai-knowledge');
export const saveKnowledge = (entry: KnowledgeDraft) => post<{ entry: KnowledgeEntry }>('/ai-knowledge', entry);
export const previewKnowledge = (cookie_id: string, item_id: string, message: string) =>
  post<{ entries: KnowledgeEntry[]; model_called: false }>('/ai-knowledge/preview', { cookie_id, item_id, message });
