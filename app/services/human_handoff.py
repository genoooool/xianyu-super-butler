"""Durable, account/conversation-scoped human takeover; never a model side effect."""
import asyncio
import math
import time

HANDOFF_REPLY = "请稍等，我马上召唤人工客服。耐心等待一下哦亲。"
HANDOFF_MARKER = "__HUMAN_HANDOFF__"


class HandoffReply(str):
    """A generated decision, not an ordinary empty/skipped response or QA answer."""
    def __new__(cls, reason):
        value = super().__new__(cls, HANDOFF_REPLY)
        value.reason = reason
        return value


def initialize_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS chat_human_handoffs (
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,
        chat_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        pending INTEGER NOT NULL CHECK(pending IN (0,1)),
        reason TEXT NOT NULL,
        buyer_id TEXT NOT NULL,
        buyer_name TEXT NOT NULL,
        item_id TEXT NOT NULL,
        created_ms INTEGER NOT NULL,
        resumed_ms INTEGER NOT NULL DEFAULT 0,
        send_status TEXT NOT NULL CHECK(send_status IN ('unknown','confirmed','withheld')),
        PRIMARY KEY(owner_id,cookie_id,chat_id)
    )""")


def chat_key(chat_id):
    value = str(chat_id or '').split('@', 1)[0].strip()
    if not value or len(value) > 128:
        raise ValueError('无效的会话编号')
    return value


def message_timestamp(message):
    try:
        value = float(message['1']['5'])
        return int(value) if math.isfinite(value) and value > 0 else 0
    except (TypeError, ValueError, KeyError, OverflowError):
        return 0


def outgoing_precedes_resume(db, cookie_id, chat_id, message_ms):
    """Delayed self-echoes must not re-pause a conversation already resumed."""
    if not isinstance(message_ms, (int, float)) or not math.isfinite(message_ms) or message_ms <= 0:
        return False
    service = HumanHandoffs(db)
    with db.lock:
        owner = service.owner(cookie_id)
        state = service.state(owner, cookie_id, chat_id)
    return bool(state and not state['pending'] and 0 < message_ms <= state['resumed_ms'])


class HumanHandoffs:
    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock

    def owner(self, cookie_id):
        row = self.db.conn.execute('SELECT user_id FROM cookies WHERE id=?', (cookie_id,)).fetchone()
        if not row or row[0] is None:
            raise PermissionError('账号不存在或无权限')
        return row[0]

    def _require_owner(self, owner_id, cookie_id):
        if self.owner(cookie_id) != owner_id:
            raise PermissionError('账号不存在或无权限')

    def state(self, owner_id, cookie_id, chat_id):
        with self.db.lock:
            self._require_owner(owner_id, cookie_id)
            cursor = self.db.conn.execute('''SELECT * FROM chat_human_handoffs
                WHERE owner_id=? AND cookie_id=? AND chat_id=?''', (owner_id, cookie_id, chat_key(chat_id)))
            row = cursor.fetchone()
            return dict(zip([c[0] for c in cursor.description], row)) if row else None

    def can_reply(self, owner_id, cookie_id, chat_id, revision, message_ms):
        state = self.state(owner_id, cookie_id, chat_id)
        if not state:
            return revision == 0
        return (not state['pending'] and state['revision'] == revision
                and message_ms > state['resumed_ms'])

    def begin(self, owner_id, cookie_id, chat_id, revision, message_ms, reason, buyer_id, buyer_name, item_id):
        """Claim once BEFORE sending. A crash/timeout cannot permit replay or more AI replies."""
        with self.db.lock, self.db.conn:
            if not self.can_reply(owner_id, cookie_id, chat_id, revision, message_ms):
                return None
            now = int(self.clock() * 1000)
            cursor = self.db.conn.execute('''INSERT INTO chat_human_handoffs
                (owner_id,cookie_id,chat_id,revision,pending,reason,buyer_id,buyer_name,item_id,created_ms,send_status)
                VALUES (?,?,?,?,1,?,?,?,?,?,'unknown')
                ON CONFLICT(owner_id,cookie_id,chat_id) DO UPDATE SET
                revision=excluded.revision,pending=1,reason=excluded.reason,buyer_id=excluded.buyer_id,
                buyer_name=excluded.buyer_name,item_id=excluded.item_id,created_ms=excluded.created_ms,send_status='unknown'
                WHERE chat_human_handoffs.pending=0 AND chat_human_handoffs.revision=?
                    AND chat_human_handoffs.resumed_ms<?
                ''', (owner_id, cookie_id, chat_key(chat_id), revision + 1, str(reason)[:80],
                      str(buyer_id)[:128], str(buyer_name)[:120], str(item_id or '')[:128], now, revision, message_ms))
            if cursor.rowcount != 1:
                return None
            return self.state(owner_id, cookie_id, chat_id)

    def current(self, ticket):
        state = self.state(ticket['owner_id'], ticket['cookie_id'], ticket['chat_id'])
        return bool(state and state['pending'] and state['revision'] == ticket['revision'])

    def finish_send(self, ticket, status):
        if status not in {'unknown', 'confirmed', 'withheld'}:
            raise ValueError('无效的发送状态')
        with self.db.lock, self.db.conn:
            if self.current(ticket):
                self.db.conn.execute('''UPDATE chat_human_handoffs SET send_status=?
                    WHERE owner_id=? AND cookie_id=? AND chat_id=? AND revision=?''',
                    (status, ticket['owner_id'], ticket['cookie_id'], ticket['chat_id'], ticket['revision']))

    def pending(self, owner_id):
        with self.db.lock:
            cursor = self.db.conn.execute('''SELECT h.* FROM chat_human_handoffs h
                JOIN cookies c ON c.id=h.cookie_id AND c.user_id=h.owner_id
                WHERE h.owner_id=? AND h.pending=1 ORDER BY h.created_ms DESC''', (owner_id,))
            return [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]

    def resume(self, owner_id, cookie_id, chat_id, revision):
        with self.db.lock, self.db.conn:
            state = self.state(owner_id, cookie_id, chat_id)
            if not state or not state['pending'] or state['revision'] != revision:
                raise ValueError('会话状态已改变，请刷新后再操作')
            cursor = self.db.conn.execute('''UPDATE chat_human_handoffs SET pending=0,revision=revision+1,resumed_ms=?
                WHERE owner_id=? AND cookie_id=? AND chat_id=? AND pending=1 AND revision=?''',
                (int(self.clock() * 1000), owner_id, cookie_id, chat_key(chat_id), revision))
            if cursor.rowcount != 1:
                raise ValueError('会话状态已改变，请刷新后再操作')


async def request_handoff(instance, db, *, owner_id, chat_id, buyer_id, buyer_name, item_id,
                          reason, revision, message_ms, check, notify=True):
    """One terminal reply attempt. Notification failure never re-enables the conversation."""
    service = HumanHandoffs(db)
    if not check():
        return None
    ticket = service.begin(owner_id, instance.cookie_id, chat_id, revision, message_ms,
                           reason, buyer_id, buyer_name, item_id)
    if not ticket:
        return None
    # Content-free native alert. The persistent UI queue works even if alerts are disabled.
    try:
        from app.desktop_notifications import desktop_notifications
        if notify:
            desktop_notifications.publish_handoff(user_id=owner_id, identity=(instance.cookie_id, ticket['chat_id'], ticket['revision']))
    except Exception:
        pass
    try:
        if not service.current(ticket) or not check():
            service.finish_send(ticket, 'withheld')
            return service.state(owner_id, instance.cookie_id, chat_id)
        from app.services.reply_delivery import require_receipt
        receipt = await instance.send_im_text(chat_id, buyer_id, HANDOFF_REPLY)
        require_receipt(receipt)
        service.finish_send(ticket, 'confirmed')
    except asyncio.CancelledError:
        # The durable claim already survives cancellation; never retry an unknown send.
        raise
    except Exception:
        service.finish_send(ticket, 'unknown')
    return service.state(owner_id, instance.cookie_id, chat_id)
