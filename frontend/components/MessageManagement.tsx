import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  Image,
  Inbox,
  Loader2,
  Package,
  Plus,
  RefreshCw,
  Search,
  Send,
  Settings2,
  Smile,
  Zap,
  Trash2,
  UserRound,
} from 'lucide-react';

import {
  AccountDetail,
  ChatAccount,
  ChatConversation,
  ChatMessage,
  HumanHandoff,
  Item,
  MessageFilter,
  MessageFilterType,
  QuickPhrase,
} from '../types';
import {
  batchCreateMessageFilters,
  batchDeleteMessageFilters,
  deleteMessageFilter,
  getAccountDetails,
  getChatAccounts,
  getChatConversations,
  getChatMessages,
  getHumanHandoffs,
  resumeHumanHandoff,
  getItems,
  getMessageFilters,
  getQuickPhrases,
  sendChatMessage,
  useQuickPhrase,
  toggleMessageFilter,
} from '../services/api';
import { confirmAction, notify } from '../services/feedback';
import { EmptyState, SectionHeader } from './ui';
import { ReplyImagePicker } from './ReplyMedia';

type View = 'messages' | 'filters';
type MobilePane = 'list' | 'chat';

// 轮询间隔。会话列表与消息都要经 WebSocket 透传到闲鱼，频率过高会把账号
// 打到限流（429 flow controled）；聊天场景 10 秒的延迟是可接受的。
const CONVERSATION_POLL_MS = 10000;
const MESSAGE_POLL_MS = 10000;
const READ_WATERMARKS_STORAGE_KEY = 'xianyu-message-read-watermarks-v2';
const MAX_READ_WATERMARKS = 500;

type ReadWatermark = {
  lastMessageTime: number;
  lastMessageSummary: string;
  unreadCount: number;
};

type ReadWatermarks = Record<string, ReadWatermark>;

const readWatermarkKey = (accountId: string, cid: string) => `${accountId}\u0000${cid}`;

const loadReadWatermarks = (): ReadWatermarks => {
  if (typeof window === 'undefined') return {};
  try {
    const parsed = JSON.parse(window.localStorage.getItem(READ_WATERMARKS_STORAGE_KEY) || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(([, value]) => {
        if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
        const watermark = value as Partial<ReadWatermark>;
        return Number.isFinite(watermark.lastMessageTime)
          && typeof watermark.lastMessageSummary === 'string'
          && Number.isFinite(watermark.unreadCount);
      })
    ) as ReadWatermarks;
  } catch {
    return {};
  }
};

const saveReadWatermarks = (watermarks: ReadWatermarks) => {
  if (typeof window === 'undefined') return;
  try {
    const recentEntries = Object.entries(watermarks)
      .sort(([, left], [, right]) => right.lastMessageTime - left.lastMessageTime)
      .slice(0, MAX_READ_WATERMARKS);
    window.localStorage.setItem(
      READ_WATERMARKS_STORAGE_KEY,
      JSON.stringify(Object.fromEntries(recentEntries))
    );
  } catch {
    // 隐私模式或存储空间不足时，仍保留当前页面内的已读状态。
  }
};

const isCoveredByReadWatermark = (
  watermark: ReadWatermark | undefined,
  conversation: ChatConversation
) => {
  if (!watermark) return false;
  const lastMessageTime = Number(conversation.lastMessageTime) || 0;
  const lastMessageSummary = String(conversation.lastMessageSummary || '');
  const unreadCount = Number(conversation.unreadCount) || 0;
  return lastMessageTime < watermark.lastMessageTime
    || (
      lastMessageTime === watermark.lastMessageTime
      && lastMessageSummary === watermark.lastMessageSummary
      && unreadCount <= watermark.unreadCount
    );
};

interface MessageManagementProps {
  isActive?: boolean;
}

const normalizeImageUrl = (value?: string) => {
  if (!value) return '';
  return value.startsWith('//') ? `https:${value}` : value;
};

const formatTimestamp = (value?: number) => {
  if (!value) return '';
  const timestamp = value < 1_000_000_000_000 ? value * 1000 : value;
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '';
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
};

const formatDateTime = (value?: string) => {
  if (!value) return '-';
  const date = new Date(value.includes('T') ? value : value.replace(' ', 'T'));
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      });
};

const accountName = (account?: ChatAccount) =>
  account?.displayName || account?.accountId || '未选择账号';

const filterTypeLabel: Record<MessageFilterType, string> = {
  skip_reply: '跳过自动回复',
  skip_notify: '跳过外部通知',
};

const MessageManagement: React.FC<MessageManagementProps> = ({ isActive = true }) => {
  const [view, setView] = useState<View>('messages');
  const [mobilePane, setMobilePane] = useState<MobilePane>('list');
  const [accounts, setAccounts] = useState<ChatAccount[]>([]);
  const [accountDetails, setAccountDetails] = useState<AccountDetail[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [activeAccountId, setActiveAccountId] = useState('');
  const [conversations, setConversations] = useState<ChatConversation[]>([]);
  const [handoffs, setHandoffs] = useState<HumanHandoff[]>([]);
  const [resumingHandoff, setResumingHandoff] = useState(false);
  const [handoffLoadError, setHandoffLoadError] = useState(false);
  const handoffsRef = useRef(handoffs);
  handoffsRef.current = handoffs;
  const accountIdRef = useRef(activeAccountId);
  accountIdRef.current = activeAccountId;
  // 按图片地址记录加载失败的头像，避免反复请求同一个取不到的外部地址
  const [failedAvatars, setFailedAvatars] = useState<Set<string>>(new Set());
  const [activeCid, setActiveCid] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [query, setQuery] = useState('');
  const [searchInputUnlocked, setSearchInputUnlocked] = useState(false);
  const [searchInputName] = useState(
    () => `conversation-filter-${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
  const [accountsLoading, setAccountsLoading] = useState(true);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [draft, setDraft] = useState('');
  const [draftImages, setDraftImages] = useState<string[]>([]);
  const [imageUploading, setImageUploading] = useState(false);
  const [showImagePicker, setShowImagePicker] = useState(false);
  const destinationRef = useRef('');
  const destination = `${activeAccountId}:${activeCid}`;
  destinationRef.current = destination;
  useEffect(() => {
    setDraft(''); setDraftImages([]); setShowImagePicker(false); setImageUploading(false);
  }, [destination]);
  // 快捷短语：人工客服常用话术
  const [quickPhrases, setQuickPhrases] = useState<QuickPhrase[]>([]);
  const [showPhrases, setShowPhrases] = useState(false);
  const [sending, setSending] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const searchInputTouchedRef = useRef(false);
  const activeCidRef = useRef('');
  const readWatermarksRef = useRef<ReadWatermarks>(loadReadWatermarks());

  const [filters, setFilters] = useState<MessageFilter[]>([]);
  const [filtersLoading, setFiltersLoading] = useState(true);
  const [filterAccount, setFilterAccount] = useState('');
  const [filterType, setFilterType] = useState<MessageFilterType | ''>('');
  const [newAccount, setNewAccount] = useState('');
  const [newType, setNewType] = useState<MessageFilterType>('skip_reply');
  const [newKeywords, setNewKeywords] = useState('');
  const [selectedFilterIds, setSelectedFilterIds] = useState<number[]>([]);
  const [savingFilters, setSavingFilters] = useState(false);

  const activeAccount = accounts.find((account) => account.accountId === activeAccountId);
  const accountHandoffs = handoffs.filter((entry) => entry.cookie_id === activeAccountId);
  const activeHandoff = accountHandoffs.find((entry) => entry.chat_id === activeCid);
  // Pending chats stay reachable even when the platform list is offline, paged or rate-limited.
  const allConversations = useMemo(() => {
    const pending = handoffs.filter((entry) => entry.cookie_id === activeAccountId);
    const extra: ChatConversation[] = pending.filter((entry) => !conversations.some((c) => c.cid === entry.chat_id)).map((entry) => ({
      cid: entry.chat_id, rawCid: entry.chat_id, otherUserId: entry.buyer_id, otherUserName: entry.buyer_name,
      itemId: entry.item_id, lastMessageSummary: '等待人工客服处理', lastMessageTime: entry.created_ms, unreadCount: 0,
    }));
    const ids = new Set(pending.map((entry) => entry.chat_id));
    return [...extra, ...conversations].sort((a, b) => Number(ids.has(b.cid)) - Number(ids.has(a.cid)));
  }, [conversations, handoffs, activeAccountId]);
  const activeConversation = allConversations.find((conversation) => conversation.cid === activeCid);

  const itemMap = useMemo(
    () => new Map(items.map((item) => [`${item.cookie_id}:${item.item_id}`, item])),
    [items]
  );

  const activeItem = activeConversation?.itemId
    ? itemMap.get(`${activeAccountId}:${activeConversation.itemId}`)
    : undefined;

  const visibleConversations = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return allConversations;
    return allConversations.filter((conversation) =>
      [
        conversation.otherUserName,
        conversation.otherUserId,
        conversation.itemTitle,
        conversation.itemId,
        conversation.lastMessageSummary,
      ].some((value) => String(value || '').toLocaleLowerCase().includes(normalized))
    );
  }, [allConversations, query]);

  useEffect(() => {
    if (!isActive) return;
    let cancelled = false;
    let busy = false;
    const refresh = async () => {
      if (busy) return;
      busy = true;
      try {
        const entries = await getHumanHandoffs();
        if (!cancelled) { setHandoffs(entries); setHandoffLoadError(false); }
      } catch {
        if (!cancelled) setHandoffLoadError(true); // Do not clear still-pending badges on a transient failure.
      } finally { busy = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2000); // Local SQLite only; no Xianyu requests.
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [isActive]);

  const handleResumeHandoff = async () => {
    if (!activeHandoff || resumingHandoff) return;
    const target = activeHandoff;
    const accepted = await confirmAction('确认已处理完这段会话？恢复后只回复新消息，不补发旧消息，也不会开启原本关闭的 AI。', {
      title: '恢复自动回复', confirmLabel: '确认恢复', danger: false,
    });
    if (!accepted) return;
    setResumingHandoff(true);
    try {
      const result = await resumeHumanHandoff(target);
      setHandoffs((entries) => entries.filter((entry) => !(entry.cookie_id === target.cookie_id && entry.chat_id === target.chat_id && entry.revision === target.revision)));
      notify(result.message, 'success');
    } catch (error) {
      notify(`恢复未完成：${(error as Error).message}`, 'error');
    } finally { setResumingHandoff(false); }
  };

  const selectedAllFilters = filters.length > 0
    && filters.every((filter) => selectedFilterIds.includes(filter.id));

  const rememberConversationRead = (conversation: ChatConversation) => {
    if (!activeAccountId || !conversation.cid) return;
    const key = readWatermarkKey(activeAccountId, conversation.cid);
    const nextWatermark: ReadWatermark = {
      lastMessageTime: Number(conversation.lastMessageTime) || 0,
      lastMessageSummary: String(conversation.lastMessageSummary || ''),
      unreadCount: Number(conversation.unreadCount) || 0,
    };
    const previous = readWatermarksRef.current[key];
    if (
      previous
      && (
        previous.lastMessageTime > nextWatermark.lastMessageTime
        || (
          previous.lastMessageTime === nextWatermark.lastMessageTime
          && previous.lastMessageSummary === nextWatermark.lastMessageSummary
          && previous.unreadCount >= nextWatermark.unreadCount
        )
      )
    ) {
      return;
    }
    readWatermarksRef.current[key] = nextWatermark;
    saveReadWatermarks(readWatermarksRef.current);
  };

  const openConversation = (conversation: ChatConversation) => {
    rememberConversationRead(conversation);
    activeCidRef.current = conversation.cid;
    setConversations((current) => current.map((item) => (
      item.cid === conversation.cid ? { ...item, unreadCount: 0 } : item
    )));
    setActiveCid(conversation.cid);
    setMobilePane('chat');
  };

  const loadAccounts = async () => {
    setAccountsLoading(true);
    try {
      const data = await getChatAccounts();
      setAccounts(data);
      setActiveAccountId((current) => {
        if (current && data.some((account) => account.accountId === current)) return current;
        return data.find((account) => account.connected)?.accountId || data[0]?.accountId || '';
      });
    } catch (error) {
      notify(`加载消息账号失败：${(error as Error).message}`, 'error');
    } finally {
      setAccountsLoading(false);
    }
  };

  const loadConversations = async (silent = false) => {
    if (!activeAccountId) {
      setConversations([]);
      return;
    }
    if (!silent) setConversationsLoading(true);
    try {
      const result = await getChatConversations(activeAccountId);
      if (accountIdRef.current !== activeAccountId) return;
      const incoming = result.conversations || [];
      const currentCid = activeCidRef.current;
      const pendingCurrent = handoffsRef.current.some((entry) => entry.cookie_id === activeAccountId && entry.chat_id === currentCid);
      const nextCid = pendingCurrent || incoming.some((conversation) => conversation.cid === currentCid)
        ? currentCid
        : incoming[0]?.cid || '';
      const normalized = incoming.map((conversation) => {
        const isOpen = isActive
          && conversation.cid === nextCid
          && (
            mobilePane === 'chat'
            || window.matchMedia('(min-width: 1024px)').matches
          );
        const watermark = readWatermarksRef.current[
          readWatermarkKey(activeAccountId, conversation.cid)
        ];
        if (isOpen) rememberConversationRead(conversation);
        return isOpen || isCoveredByReadWatermark(watermark, conversation)
          ? { ...conversation, unreadCount: 0 }
          : conversation;
      });
      setConversations(normalized);
      setActiveCid((current) => {
        // 用户已经选了会话就不要动它。会话列表是定时刷新的，一旦某次刷新
        // 因限流或数据不全而没带上当前会话，这里就会把用户强行切回第一条 ——
        // 表现为「点第二个及之后的对话，消息区一片空白」。
        const next = current && (handoffsRef.current.some((entry) => entry.cookie_id === activeAccountId && entry.chat_id === current) || incoming.some((conversation) => conversation.cid === current))
          ? current
          : incoming[0]?.cid || '';
        activeCidRef.current = next;
        return next;
      });
    } catch (error) {
      if (!silent) notify(`加载会话失败：${(error as Error).message}`, 'error');
    } finally {
      if (!silent) setConversationsLoading(false);
    }
  };

  const loadMessages = async (silent = false) => {
    if (!activeAccountId || !activeCid) {
      setMessages([]);
      return;
    }
    if (!silent) setMessagesLoading(true);
    try {
      const result = await getChatMessages(activeAccountId, activeCid);
      setMessages(result.messages || []);
    } catch (error) {
      if (!silent) notify(`加载聊天记录失败：${(error as Error).message}`, 'error');
    } finally {
      if (!silent) setMessagesLoading(false);
    }
  };

  const loadFilters = async () => {
    setFiltersLoading(true);
    try {
      const data = await getMessageFilters({
        cookie_id: filterAccount || undefined,
        filter_type: filterType || undefined,
      });
      setFilters(data);
      setSelectedFilterIds((current) =>
        current.filter((id) => data.some((filter) => filter.id === id))
      );
    } catch (error) {
      notify(`加载过滤规则失败：${(error as Error).message}`, 'error');
    } finally {
      setFiltersLoading(false);
    }
  };

  useEffect(() => {
    const initialize = async () => {
      const detailsRequest = getAccountDetails()
        .then((data) => {
          setAccountDetails(data);
          setNewAccount(data[0]?.id || '');
        })
        .catch((error) => notify(`加载账号资料失败：${(error as Error).message}`, 'error'));
      const itemsRequest = getItems()
        .then(setItems)
        .catch((error) => notify(`加载商品资料失败：${(error as Error).message}`, 'error'));
      await Promise.all([loadAccounts(), detailsRequest, itemsRequest, loadFilters()]);
    };
    void initialize();
  }, []);

  useEffect(() => {
    activeCidRef.current = '';
    setActiveCid('');
    setMessages([]);
    setMobilePane('list');
    if (isActive) void loadConversations();
  }, [isActive, activeAccountId]);

  useEffect(() => {
    if (isActive) void loadMessages();
  }, [isActive, activeAccountId, activeCid]);

  useEffect(() => {
    if (!isActive || view !== 'messages' || !activeAccountId) return undefined;
    void loadConversations(true);
    // 10 秒一轮。原来 3 秒刷一次，每条请求都要经 WebSocket 转发到闲鱼，
    // 多开几个标签页就会把账号打到 429（flow controled），表现为消息加载失败。
    const timer = window.setInterval(() => void loadConversations(true), CONVERSATION_POLL_MS);
    return () => window.clearInterval(timer);
  }, [isActive, view, activeAccountId, mobilePane]);

  useEffect(() => {
    if (!isActive || view !== 'messages' || !activeAccountId || !activeCid) return undefined;
    void loadMessages(true);
    const timer = window.setInterval(() => void loadMessages(true), MESSAGE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [isActive, view, activeAccountId, activeCid]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => {
    // Chromium 和部分密码管理器会无视 autocomplete="off"，把本站保存的
    // 管理员用户名灌进页面上的第一个文本框。只清理用户尚未触碰过的值，
    // 避免定时器误删用户真正输入的搜索词。
    const clearUnexpectedAutofill = () => {
      if (searchInputTouchedRef.current || !searchInputRef.current) return;
      searchInputRef.current.value = '';
      setQuery('');
    };
    clearUnexpectedAutofill();
    const timers = [100, 500, 1500].map((delay) => (
      window.setTimeout(clearUnexpectedAutofill, delay)
    ));
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, []);

  useEffect(() => {
    getQuickPhrases()
      .then(setQuickPhrases)
      .catch(() => setQuickPhrases([]));
  }, []);

  // 插入短语到输入框而不是直接发送，方便先改再发
  const insertPhrase = (phrase: QuickPhrase) => {
    if (sending || imageUploading) return;
    const images = [...new Set([...draftImages, ...(phrase.image_ids || [])])];
    if (images.length > 4) { notify('每次最多发送4张图片，请先移除部分图片', 'warning'); return; }
    setDraft(current => (current ? `${current}${phrase.content}` : phrase.content));
    setDraftImages(images);
    if (images.length) setShowImagePicker(true);
    setShowPhrases(false);
    void useQuickPhrase(phrase.id).catch(() => undefined);
  };

  const sendMessage = async () => {
    const text = draft;
    if ((!text.trim() && !draftImages.length) || !activeConversation || !activeAccountId || sending || imageUploading) return;
    const sendingTo = destination;
    setSending(true);
    try {
      const result = await sendChatMessage(activeAccountId, {
        cid: activeConversation.cid,
        to_user_id: activeConversation.otherUserId,
        text,
        image_ids: draftImages,
      });
      if (!result.success) throw new Error(result.message || '未确认发送结果');
      if (destinationRef.current === sendingTo) {
        setDraft(''); setDraftImages([]);
        await Promise.all([loadMessages(true), loadConversations(true)]);
      }
      notify('消息已发送', 'success');
    } catch (error) {
      notify(`发送未确认：${(error as Error).message}。请先检查原会话，避免重复发送。`, 'error');
    } finally {
      setSending(false);
    }
  };

  const createFilters = async () => {
    const keywords = newKeywords.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
    if (!newAccount || keywords.length === 0) {
      notify('请选择账号并填写至少一个关键词', 'warning');
      return;
    }
    setSavingFilters(true);
    try {
      const result = await batchCreateMessageFilters({
        cookie_id: newAccount,
        filter_type: newType,
        keywords,
      });
      setNewKeywords('');
      await loadFilters();
      notify(`新增 ${result.created} 条，跳过重复 ${result.skipped} 条`, 'success');
    } catch (error) {
      notify(`新增规则失败：${(error as Error).message}`, 'error');
    } finally {
      setSavingFilters(false);
    }
  };

  const removeFilter = async (filter: MessageFilter) => {
    if (!await confirmAction(`删除过滤关键词“${filter.keyword}”？`)) return;
    try {
      await deleteMessageFilter(filter.id);
      await loadFilters();
      notify('规则已删除', 'success');
    } catch (error) {
      notify(`删除失败：${(error as Error).message}`, 'error');
    }
  };

  const removeSelectedFilters = async () => {
    if (
      selectedFilterIds.length === 0
      || !await confirmAction(`删除选中的 ${selectedFilterIds.length} 条规则？`)
    ) return;
    try {
      await batchDeleteMessageFilters(selectedFilterIds);
      setSelectedFilterIds([]);
      await loadFilters();
      notify('所选规则已删除', 'success');
    } catch (error) {
      notify(`批量删除失败：${(error as Error).message}`, 'error');
    }
  };

  const renderAvatar = (url: string | undefined, label: string, className: string) => {
    const normalized = normalizeImageUrl(url);
    const placeholder = (
      <div className={`${className} flex items-center justify-center bg-[#3a3427] text-sm font-bold text-white`}>
        {label.trim().slice(0, 1) || <UserRound className="h-4 w-4" />}
      </div>
    );
    // 买家头像由 DiceBear 生成，属于外部服务；加载不出来时回落到首字母，
    // 而不是让列表挂一排破图。
    return normalized && !failedAvatars.has(normalized) ? (
      <img
        src={normalized}
        alt=""
        className={`${className} object-cover`}
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={() => setFailedAvatars((previous) => new Set(previous).add(normalized))}
      />
    ) : (
      placeholder
    );
  };

  const renderMessages = () => (
    <div className="grid h-full min-h-0 overflow-hidden bg-[var(--surface)] lg:grid-cols-[356px_minmax(0,1fr)]">
      <aside className={`${mobilePane === 'chat' ? 'hidden lg:flex' : 'flex'} min-h-0 flex-col border-b border-[var(--border)] lg:border-b-0 lg:border-r`}>
        <div className="flex h-[68px] shrink-0 items-center gap-3 border-b border-[var(--border)] px-4">
          <select
            value={activeAccountId}
            onChange={(event) => setActiveAccountId(event.target.value)}
            aria-label="消息账号"
            className="h-10 min-w-0 flex-1 rounded-md border border-[var(--border-strong)] bg-[var(--surface)] px-3 text-sm font-bold text-[var(--text)] outline-none focus:border-[var(--brand)]"
          >
            {accounts.length === 0 && <option value="">暂无账号</option>}
            {accounts.map((account) => (
              <option key={account.accountId} value={account.accountId}>
                {account.connected ? '在线' : '离线'} · {accountName(account)}
                {handoffs.some((entry) => entry.cookie_id === account.accountId) ? ` · 待人工 ${handoffs.filter((entry) => entry.cookie_id === account.accountId).length}` : ''}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void Promise.all([loadAccounts(), loadConversations()])}
            title="刷新账号和会话"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full hover:bg-[var(--surface-hover)]"
          >
            <RefreshCw className={`h-4 w-4 ${
              accountsLoading || conversationsLoading ? 'animate-spin' : ''
            }`} />
          </button>
          <button
            type="button"
            onClick={() => setView('filters')}
            title="消息过滤规则"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full hover:bg-[var(--surface-hover)]"
          >
            <Settings2 className="h-4 w-4" />
          </button>
        </div>

        <div className="border-b border-[var(--border)] p-3">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-soft)]" />
            <input
              ref={searchInputRef}
              type="search"
              name={searchInputName}
              autoComplete="new-password"
              autoCorrect="off"
              spellCheck={false}
              aria-label="搜索联系人、商品或消息"
              data-lpignore="true"
              data-1p-ignore="true"
              data-bwignore="true"
              readOnly={!searchInputUnlocked}
              value={query}
              onFocus={() => {
                searchInputTouchedRef.current = true;
                setSearchInputUnlocked(true);
              }}
              onBlur={() => {
                searchInputTouchedRef.current = false;
                setSearchInputUnlocked(false);
              }}
              onChange={(event) => {
                if (!searchInputTouchedRef.current) {
                  event.currentTarget.value = '';
                  setQuery('');
                  return;
                }
                setQuery(event.target.value);
              }}
              placeholder="搜索联系人、商品或消息"
              className="h-9 w-full rounded-md bg-[var(--surface-subtle)] pl-9 pr-3 text-sm text-[var(--text)] outline-none placeholder:text-[var(--text-soft)] focus:ring-2 focus:ring-[var(--brand)]"
            />
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {handoffLoadError && <p role="status" className="px-4 py-2 text-xs text-red-600">待人工状态暂时无法刷新，请稍后重试；自动回复不会因此恢复。</p>}
          {accountHandoffs.length > 0 && <p className="px-4 py-2 text-xs font-bold text-red-600">待人工处理 {accountHandoffs.length} 个会话 · 已暂停自动回复</p>}
          {conversationsLoading && conversations.length === 0 && (
            <div className="flex justify-center py-16">
              <Loader2 className="h-6 w-6 animate-spin text-[#d6b600]" />
            </div>
          )}
          {visibleConversations.map((conversation) => {
            const selected = conversation.cid === activeCid;
            const title = conversation.otherUserName || `闲鱼用户 ${conversation.otherUserId}`;
            return (
              <button
                key={conversation.cid}
                type="button"
                onClick={() => openConversation(conversation)}
                className={`grid w-full grid-cols-[48px_minmax(0,1fr)_auto] gap-3 px-4 py-3 text-left ${
                  selected ? 'bg-[var(--surface-strong)]' : 'hover:bg-[var(--surface-hover)]'
                }`}
              >
                {renderAvatar(conversation.otherUserAvatar, title, 'h-12 w-12 rounded-full')}
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="truncate text-sm font-bold text-[var(--text)]">{title}</p>
                    {accountHandoffs.some((entry) => entry.chat_id === conversation.cid) && (
                      <span className="shrink-0 rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-bold text-red-700">待人工</span>
                    )}
                    {conversation.unreadCount > 0 && (
                      <span className="min-w-5 rounded-full bg-[var(--unread-badge)] px-1.5 text-center text-[10px] leading-5 text-white">
                        {conversation.unreadCount > 99 ? '99+' : conversation.unreadCount}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 truncate text-xs text-[var(--text-muted)]">
                    {conversation.lastMessageSummary || '暂无消息'}
                  </p>
                  <p className="mt-1 truncate text-[10px] text-[var(--text-soft)]">
                    {conversation.itemTitle || (conversation.itemId ? `商品 ${conversation.itemId}` : '普通会话')}
                  </p>
                </div>
                <span className="pt-0.5 text-[10px] text-[var(--text-soft)]">
                  {formatTimestamp(conversation.lastMessageTime)}
                </span>
              </button>
            );
          })}
          {!conversationsLoading && visibleConversations.length === 0 && (
            <div className="px-6 py-20 text-center">
              <Inbox className="mx-auto h-9 w-9 text-[var(--text-soft)]" />
              <p className="mt-3 text-sm text-[var(--text-muted)]">
                {activeAccount?.connected ? '暂无会话' : '账号离线，无法读取会话'}
              </p>
            </div>
          )}
        </div>
      </aside>

      <section className={`${mobilePane === 'list' ? 'hidden lg:flex' : 'flex'} min-h-0 min-w-0 flex-col`}>
        {activeConversation ? (
          <>
            <header className="flex h-[68px] shrink-0 items-center justify-between border-b border-[var(--border)] px-5">
              <div className="flex min-w-0 items-center gap-2">
                <button
                  type="button"
                  onClick={() => setMobilePane('list')}
                  className="-ml-2 flex h-9 w-9 shrink-0 items-center justify-center rounded-md hover:bg-[var(--surface-hover)] lg:hidden"
                  title="返回会话列表"
                  aria-label="返回会话列表"
                >
                  <ArrowLeft className="h-5 w-5" />
                </button>
                <div className="min-w-0">
                  <h3 className="truncate text-base font-bold text-[var(--text)]">
                    {activeConversation.otherUserName || `闲鱼用户 ${activeConversation.otherUserId}`}
                  </h3>
                  <p className="mt-0.5 truncate text-xs text-[var(--text-soft)]">
                    {activeConversation.otherUserId}
                  </p>
                </div>
              </div>
              <span className={`inline-flex items-center gap-1.5 text-xs font-bold ${
                activeAccount?.connected ? 'text-emerald-600' : 'text-[#999]'
              }`}>
                <span className={`h-2 w-2 rounded-full ${
                  activeAccount?.connected ? 'bg-emerald-500' : 'bg-[#b8ac8e]'
                }`} />
                {activeAccount?.connected ? '账号在线' : '账号离线'}
              </span>
            </header>

            {activeHandoff && (
              <div role="status" className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] bg-[var(--surface-subtle)] px-5 py-3">
                <div className="min-w-0 text-xs text-[var(--text-muted)]">
                  <span className="mr-2 inline-flex rounded-full bg-red-100 px-2 py-1 font-bold text-red-700">待人工处理</span>
                  自动回复已暂停，重启后也不会自动恢复。
                  <p className="mt-2">{activeHandoff.send_status === 'confirmed'
                    ? '转人工话术已收到平台发送回执。'
                    : activeHandoff.send_status === 'withheld'
                      ? '发送前状态已改变，转人工话术未发送，请直接接手。'
                      : '转人工话术发送结果未确认，请先查看原会话，避免重复发送。'}</p>
                </div>
                <button type="button" disabled={resumingHandoff} onClick={() => void handleResumeHandoff()}
                  className="shrink-0 rounded-full bg-[var(--brand)] px-4 py-2 text-xs font-bold text-[var(--brand-ink)] disabled:opacity-50">
                  {resumingHandoff ? '正在恢复…' : '已处理，恢复自动回复'}
                </button>
              </div>
            )}

            <div className="flex min-h-[84px] shrink-0 items-center gap-3 border-b border-[var(--border)] px-4 py-3 sm:min-h-[92px] sm:gap-4 sm:px-5">
              {normalizeImageUrl(activeConversation.itemImage || activeItem?.item_image) ? (
                <img
                  src={normalizeImageUrl(activeConversation.itemImage || activeItem?.item_image)}
                  alt=""
                  className="h-14 w-14 shrink-0 rounded-md object-cover sm:h-16 sm:w-16"
                />
              ) : (
                <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-md bg-[var(--surface-strong)] text-[var(--text-soft)] sm:h-16 sm:w-16">
                  <Package className="h-5 w-5" />
                </div>
              )}
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-bold text-[var(--text)]">
                  {activeConversation.itemTitle || activeItem?.item_title || '未关联商品'}
                </p>
                {activeItem?.item_price && (
                  <p className="mt-1 text-base font-extrabold text-[#ff3b30]">
                    <span className="text-xs">¥</span>
                    {String(activeItem.item_price).replace(/^[¥￥]\s*/, '')}
                  </p>
                )}
                <p className="mt-1 truncate text-xs text-[var(--text-soft)]">
                  {activeConversation.itemId ? `商品 ID ${activeConversation.itemId}` : '普通会话'}
                </p>
              </div>
              <span className="hidden rounded-md bg-[var(--brand)] px-4 py-2 text-xs font-bold text-[var(--brand-ink)] sm:inline-flex">
                {accountName(activeAccount)}
              </span>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto bg-[var(--app-bg)] px-4 py-6 sm:px-8">
              {messagesLoading && messages.length === 0 ? (
                <div className="flex h-full items-center justify-center">
                  <Loader2 className="h-6 w-6 animate-spin text-[#d6b600]" />
                </div>
              ) : (
                <div className="mx-auto max-w-4xl space-y-4">
                  {messages.map((message, index) => {
                    const previous = messages[index - 1];
                    const showTime = !previous || Math.abs(message.time - previous.time) > 300_000;
                    const senderLabel = message.isSelf
                      ? accountName(activeAccount)
                      : activeConversation.otherUserName || activeConversation.otherUserId;
                    return (
                      <div key={message.messageId || `${message.time}-${index}`}>
                        {showTime && (
                          <p className="mb-3 text-center text-[11px] text-[var(--text-soft)]">
                            {formatTimestamp(message.time)}
                          </p>
                        )}
                        <div className={`flex items-start gap-2.5 ${message.isSelf ? 'justify-end' : ''}`}>
                          {!message.isSelf && renderAvatar(
                            activeConversation.otherUserAvatar,
                            senderLabel,
                            'h-9 w-9 shrink-0 rounded-full'
                          )}
                          <div className={`max-w-[76%] rounded-md px-3.5 py-2.5 text-sm leading-6 ${
                            message.isSelf ? 'bg-[var(--brand)] text-[var(--brand-ink)]' : 'bg-[var(--surface-strong)] text-[var(--text)]'
                          }`}>
                            {message.images.map((url) => (
                              <img
                                key={url}
                                src={normalizeImageUrl(url)}
                                alt="聊天图片"
                                className="mb-2 max-h-80 max-w-full rounded object-contain last:mb-0"
                              />
                            ))}
                            {message.text && (
                              <p className="whitespace-pre-wrap break-words">{message.text}</p>
                            )}
                          </div>
                          {message.isSelf && renderAvatar(
                            activeAccount?.avatarUrl,
                            senderLabel,
                            'h-9 w-9 shrink-0 rounded-full'
                          )}
                        </div>
                      </div>
                    );
                  })}
                  {messages.length === 0 && !messagesLoading && (
                    <p className="py-16 text-center text-sm text-[var(--text-soft)]">暂无聊天记录</p>
                  )}
                  <div ref={messagesEndRef} />
                </div>
              )}
            </div>

            <footer className="shrink-0 border-t border-[var(--border)] bg-[var(--surface)] px-4 py-3 sm:px-5">
              <div className="mb-2 flex items-center gap-4 text-[var(--text-muted)]">
                <button type="button" title="表情（暂未开放）" className="hover:text-[var(--text)]">
                  <Smile className="h-5 w-5" />
                </button>
                <button type="button" title="添加图片" onClick={() => setShowImagePicker(value => !value)} disabled={sending || imageUploading} className="hover:text-[var(--text)]">
                  <Image className="h-5 w-5" />
                </button>
                <div className="relative">
                  <button
                    type="button"
                    title="快捷短语"
                    onClick={() => setShowPhrases(value => !value)}
                    className={`hover:text-[var(--text)] ${showPhrases ? 'text-[var(--text)]' : ''}`}
                  >
                    <Zap className="h-5 w-5" />
                  </button>
                  {showPhrases && (
                    <div className="absolute bottom-8 left-0 z-20 max-h-72 w-80 overflow-y-auto rounded-lg border border-[var(--border)] bg-[var(--surface)] p-2 shadow-lg">
                      {quickPhrases.length === 0 ? (
                        <p className="px-2 py-3 text-xs text-gray-500">
                          还没有快捷短语，可在「设置」中添加。
                        </p>
                      ) : (
                        quickPhrases.map(phrase => (
                          <button
                            key={phrase.id}
                            type="button"
                            onClick={() => insertPhrase(phrase)}
                            className="block w-full rounded-md px-2 py-2 text-left hover:bg-[var(--surface-hover)]"
                          >
                            <span className="block text-xs font-semibold text-[var(--text)]">
                              [{phrase.category}] {phrase.title}
                            </span>
                            <span className="mt-0.5 block truncate text-xs text-gray-500">
                              {phrase.content}{phrase.image_ids?.length ? ` [${phrase.image_ids.length}张图片]` : ''}
                            </span>
                          </button>
                        ))
                      )}
                    </div>
                  )}
                </div>
              </div>
              {showImagePicker && <div className="mb-3"><ReplyImagePicker key={destination} ids={draftImages} onChange={setDraftImages} disabled={sending} onBusy={setImageUploading} /></div>}
              <div className="flex items-end gap-2 sm:gap-3">
                <textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey) {
                      event.preventDefault();
                      void sendMessage();
                    }
                  }}
                  rows={2}
                  placeholder={activeAccount?.connected ? '输入消息' : '账号离线，暂时无法发送'}
                  disabled={!activeAccount?.connected || sending}
                  className="min-h-[56px] min-w-0 flex-1 resize-none border-0 bg-[var(--surface)] px-0 py-1 text-sm leading-6 text-[var(--text)] outline-none placeholder:text-[var(--text-soft)] disabled:bg-[var(--surface)] sm:min-h-[72px]"
                />
                <button
                  type="button"
                  onClick={() => void sendMessage()}
                  disabled={(!draft.trim() && !draftImages.length) || sending || imageUploading || !activeAccount?.connected}
                  className="flex h-9 shrink-0 items-center gap-2 rounded-md bg-[var(--brand)] px-4 text-sm font-bold text-[var(--brand-ink)] hover:bg-[var(--brand-hover)] disabled:cursor-not-allowed disabled:bg-[var(--surface-strong)] disabled:text-[var(--text-soft)] sm:px-5"
                >
                  {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                  发送
                </button>
              </div>
            </footer>
          </>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
            <Inbox className="h-12 w-12 text-[var(--text-soft)]" />
            <p className="mt-4 text-sm font-bold text-[var(--text-muted)]">选择一条会话查看消息</p>
            <p className="mt-1 text-xs text-[var(--text-soft)]">会话和聊天记录直接来自当前闲鱼账号</p>
          </div>
        )}
      </section>
    </div>
  );

  const renderFilters = () => (
    <div className="h-full overflow-y-auto bg-[var(--app-bg)] p-4 sm:p-6 lg:p-8">
      <div className="page-stack mx-auto max-w-[1320px]">
        <header className="page-header">
          <div>
            <h1 className="page-title">消息过滤规则</h1>
            <p className="page-description">集中管理无需自动回复或无需外部通知的消息关键词。</p>
          </div>
          <button
            type="button"
            onClick={() => setView('messages')}
            className="ios-btn-secondary flex items-center gap-2 rounded-md px-4 py-2.5 text-sm"
          >
            <ArrowLeft className="h-4 w-4" />
            返回消息
          </button>
        </header>

        <section className="section-panel">
          <SectionHeader
            title="批量添加规则"
            description="每行填写一个关键词，重复内容会自动跳过。"
            icon={Plus}
          />
          <div className="grid gap-3 p-4 lg:grid-cols-[220px_180px_minmax(240px,1fr)_auto]">
            <select
              value={newAccount}
              onChange={(event) => setNewAccount(event.target.value)}
              className="ios-input rounded-md px-3 py-2.5 text-sm"
            >
              <option value="">选择账号</option>
              {accountDetails.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.nickname || account.remark || account.id}
                </option>
              ))}
            </select>
            <select
              value={newType}
              onChange={(event) => setNewType(event.target.value as MessageFilterType)}
              className="ios-input rounded-md px-3 py-2.5 text-sm"
            >
              <option value="skip_reply">跳过自动回复</option>
              <option value="skip_notify">跳过外部通知</option>
            </select>
            <textarea
              value={newKeywords}
              onChange={(event) => setNewKeywords(event.target.value)}
              rows={2}
              placeholder={'每行一个关键词，例如：\n系统通知'}
              className="ios-input min-h-20 resize-y rounded-md px-3 py-2.5 text-sm"
            />
            <button
              type="button"
              onClick={() => void createFilters()}
              disabled={savingFilters}
              className="ios-btn-primary flex min-h-11 items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm disabled:opacity-50"
            >
              {savingFilters ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
              批量添加
            </button>
          </div>
        </section>

        <section className="section-panel">
          <SectionHeader
            title="现有规则"
            description={`当前筛选共 ${filters.length} 条`}
            icon={Settings2}
          />
          <div className="toolbar rounded-none border-x-0 border-t-0 shadow-none">
            <div className="toolbar__group">
            <select
              value={filterAccount}
              onChange={(event) => setFilterAccount(event.target.value)}
              className="ios-input rounded-md px-3 py-2.5 text-sm sm:min-w-52"
            >
              <option value="">全部账号</option>
              {accountDetails.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.nickname || account.remark || account.id}
                </option>
              ))}
            </select>
            <select
              value={filterType}
              onChange={(event) => setFilterType(event.target.value as MessageFilterType | '')}
              className="ios-input rounded-md px-3 py-2.5 text-sm sm:min-w-44"
            >
              <option value="">全部用途</option>
              <option value="skip_reply">跳过自动回复</option>
              <option value="skip_notify">跳过外部通知</option>
            </select>
            <button
              type="button"
              onClick={() => void loadFilters()}
              className="ios-btn-secondary flex items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
            >
              <RefreshCw className={`h-4 w-4 ${filtersLoading ? 'animate-spin' : ''}`} />
              查询
            </button>
            </div>
            {selectedFilterIds.length > 0 && (
              <button
                type="button"
                onClick={() => void removeSelectedFilters()}
                className="ios-btn-danger flex items-center justify-center gap-2 rounded-md px-4 py-2.5 text-sm"
              >
                <Trash2 className="h-4 w-4" />
                删除所选
              </button>
            )}
          </div>

          <div className="divide-y divide-[#eeeeee] px-4">
            {filters.length > 0 && (
              <label className="flex items-center gap-3 py-3 text-xs font-bold text-[var(--text-muted)]">
                <input
                  type="checkbox"
                  checked={selectedAllFilters}
                  onChange={(event) =>
                    setSelectedFilterIds(event.target.checked ? filters.map((filter) => filter.id) : [])
                  }
                  className="h-4 w-4 accent-[#f5c400]"
                />
                全选当前列表
              </label>
            )}
            {filters.map((filter) => (
              <div
                key={filter.id}
                className="grid gap-3 py-4 sm:grid-cols-[24px_52px_180px_minmax(0,1fr)_44px] sm:items-center"
              >
                <input
                  type="checkbox"
                  checked={selectedFilterIds.includes(filter.id)}
                  onChange={(event) =>
                    setSelectedFilterIds((current) =>
                      event.target.checked
                        ? [...current, filter.id]
                        : current.filter((id) => id !== filter.id)
                    )
                  }
                  className="h-4 w-4 accent-[#f5c400]"
                />
                <button
                  type="button"
                  role="switch"
                  aria-checked={filter.enabled}
                  onClick={() => void toggleMessageFilter(filter.id).then(loadFilters)}
                  title={filter.enabled ? '停用规则' : '启用规则'}
                  className={`relative h-6 w-11 rounded-full transition-colors ${
                    filter.enabled ? 'bg-[#f5c800]' : 'bg-gray-300'
                  }`}
                >
                  <span className={`absolute left-1 top-1 h-4 w-4 rounded-full bg-white transition-transform ${
                    filter.enabled ? 'translate-x-5' : ''
                  }`} />
                </button>
                <div>
                  <p className="break-all text-sm font-bold text-[var(--text)]">{filter.cookie_id}</p>
                  <p className="mt-1 text-xs text-[var(--text-muted)]">{filterTypeLabel[filter.filter_type]}</p>
                </div>
                <div className="min-w-0">
                  <p className="break-words text-sm font-medium text-[var(--text)]">{filter.keyword}</p>
                  <p className="mt-1 text-xs text-[var(--text-soft)]">
                    {formatDateTime(filter.updated_at || filter.created_at)}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => void removeFilter(filter)}
                  title="删除规则"
                  className="flex h-9 w-9 items-center justify-center rounded-md bg-red-50 text-red-600 hover:bg-red-100"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))}
            {!filtersLoading && filters.length === 0 && (
              <EmptyState compact title="暂无消息过滤规则" description="添加规则后可跳过指定消息的自动回复或外部通知。" icon={Settings2} />
            )}
          </div>
        </section>
      </div>
    </div>
  );

  return view === 'messages' ? renderMessages() : renderFilters();
};

export default MessageManagement;
