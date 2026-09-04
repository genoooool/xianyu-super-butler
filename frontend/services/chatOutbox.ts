import type { ChatMessage } from '../types';

export type OutgoingMessage = ChatMessage & {
  accountId: string;
  cid: string;
  toUserId: string;
  toUserName: string;
  imageIds: string[];
  status: 'sending' | 'sent' | 'failed' | 'unconfirmed';
  receiptIds: string[];
};

export type DisplayMessage = ChatMessage & { outgoing?: OutgoingMessage };

// Match the existing 50-message history window. Keep failed/unknown payloads in
// memory, but do not mount hundreds of image blobs during a long-running session.
export const CHAT_WINDOW_SIZE = 50;
export function trimConfirmedOutbox(entries: OutgoingMessage[]): OutgoingMessage[] {
  const recent = new Set(entries.filter(entry => entry.status === 'sent').slice(-CHAT_WINDOW_SIZE).map(entry => entry.messageId));
  return entries.filter(entry => entry.status !== 'sent' || recent.has(entry.messageId));
}

// Per-page, per-destination outbox. Keep confirmed bubbles until the page closes:
// a lagging history response must not remove them. Only exact receipt IDs replace
// server copies; identical buyer/seller text is NEVER a deduplication key.
export function mergeChatMessages(history: ChatMessage[], outbox: OutgoingMessage[], accountId: string, cid: string): DisplayMessage[] {
  const local = outbox.filter(entry => entry.accountId === accountId && entry.cid === cid);
  const confirmed = new Set(local.flatMap(entry => entry.receiptIds));
  return [
    ...history.filter(entry => !(entry.isSelf && entry.messageId && confirmed.has(entry.messageId))),
    ...local.map(entry => ({ ...entry, outgoing: entry })),
  ].sort((left, right) => left.time - right.time);
}
