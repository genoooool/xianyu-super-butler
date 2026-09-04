"""Store-wide customer reply gate; conversation/manual and delivery state stay separate."""
import json
import time


class AccountReplyControl:
    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock

    @staticmethod
    def key(cookie_id):
        return 'customer_reply_control:' + str(cookie_id)

    def state(self, owner_id, cookie_id):
        with self.db.lock:
            row = self.db.conn.execute('SELECT user_id FROM cookies WHERE id=?', (cookie_id,)).fetchone()
            if not row or row[0] != owner_id:
                raise PermissionError('账号不存在或无权限')
            row = self.db.conn.execute('SELECT ai_enabled FROM ai_reply_settings WHERE cookie_id=?', (cookie_id,)).fetchone()
            enabled = bool(row and row[0])
            saved = self.db.conn.execute('SELECT value FROM user_settings WHERE user_id=? AND key=?',
                                         (owner_id, self.key(cookie_id))).fetchone()
            meta = json.loads(saved[0]) if saved else {'revision': 0, 'changed_ms': 0}
            if (not isinstance(meta, dict) or any(type(meta.get(k)) is not int or meta[k] < 0
                                                  for k in ('revision', 'changed_ms'))):
                raise ValueError('店铺开关记录无效，请检查本地存储')
            return dict(cookie_id=cookie_id, enabled=enabled, revision=meta['revision'], changed_ms=meta['changed_ms'])

    def record_change(self, owner_id, previous):
        """Caller owns the same DB lock and transaction as the ai_enabled write."""
        meta = dict(revision=previous['revision'] + 1,
                    changed_ms=max(int(self.clock() * 1000), previous['changed_ms'] + 1))
        self.db.conn.execute('''INSERT INTO user_settings(user_id,key,value,description)
            VALUES(?,?,?,'店铺客服总开关版本') ON CONFLICT(user_id,key) DO UPDATE SET
            value=excluded.value,updated_at=CURRENT_TIMESTAMP''',
            (owner_id, self.key(previous['cookie_id']), json.dumps(meta)))

    def set_enabled(self, owner_id, cookie_id, enabled, revision):
        if type(enabled) is not bool or type(revision) is not int or revision < 0:
            raise ValueError('无效的店铺开关状态')
        with self.db.lock, self.db.conn:
            previous = self.state(owner_id, cookie_id)
            if previous['revision'] != revision:
                raise ValueError('店铺状态已改变，请重新读取后操作')
            if previous['enabled'] != enabled:
                self.db.conn.execute('''INSERT INTO ai_reply_settings(cookie_id,ai_enabled) VALUES(?,?)
                    ON CONFLICT(cookie_id) DO UPDATE SET ai_enabled=excluded.ai_enabled,updated_at=CURRENT_TIMESTAMP''',
                    (cookie_id, int(enabled)))
                self.record_change(owner_id, previous)
            return self.state(owner_id, cookie_id)

    def allows(self, owner_id, cookie_id, revision, message_ms):
        state = self.state(owner_id, cookie_id)
        return state['enabled'] and state['revision'] == revision and message_ms > state['changed_ms']
