"""Workbench-owner scoped canned text and private image references."""
import json
from app.services.reply_assets import ReplyAssets, image_ids


def initialize_schema(cursor):
    columns = {r[1] for r in cursor.execute('PRAGMA table_info(chat_quick_phrases)')}
    if not columns:
        return
    for name, kind in (("owner_id", "INTEGER"), ("image_ids", "TEXT NOT NULL DEFAULT '[]'")):
        if name not in columns:
            cursor.execute(f'ALTER TABLE chat_quick_phrases ADD COLUMN {name} {kind}')
    owners = cursor.execute('SELECT id FROM users').fetchall()
    # Only a single-owner old database has unambiguous ownership. Never guess for shared servers.
    if len(owners) == 1:
        cursor.execute('UPDATE chat_quick_phrases SET owner_id=? WHERE owner_id IS NULL', (owners[0][0],))


class QuickPhrases:
    def __init__(self, db):
        self.db = db

    def list(self, owner, include_disabled=False):
        with self.db.lock:
            cursor = self.db.conn.execute('SELECT * FROM chat_quick_phrases WHERE owner_id=?' +
                ('' if include_disabled else ' AND enabled=1') + ' ORDER BY category,sort_order,id', (owner,))
            rows = [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]
        for row in rows:
            row['enabled'] = bool(row['enabled'])
            row['image_ids'] = image_ids(row['image_ids'])
            row.pop('owner_id', None)
        return rows

    def save(self, owner, data, phrase_id=None, *, commit=True):
        with self.db.lock:
            existing = next((r for r in self.list(owner, True) if r['id'] == phrase_id), None) if phrase_id else None
            if phrase_id and not existing:
                raise PermissionError('短语不存在或无权限')
            value = {**(existing or {}), **{k: v for k, v in data.items() if v is not None}}
            title, content = str(value.get('title', '')).strip(), str(value.get('content', ''))
            images = image_ids(value.get('image_ids', []))
            if not title or len(title) > 80 or len(content) > 2000 or (not content.strip() and not images) or '__IMAGE_SEND__' in content:
                raise ValueError('标题必填，答案需包含文字或图片，文字最多2000字')
            ReplyAssets(self.db).validate(owner, images)
            args = (title, content, str(value.get('category') or '默认')[:80], int(value.get('sort_order') or 0),
                    int(bool(value.get('enabled', True))), json.dumps(images))
            if phrase_id:
                self.db.conn.execute('UPDATE chat_quick_phrases SET title=?,content=?,category=?,sort_order=?,enabled=?,image_ids=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND owner_id=?', (*args, phrase_id, owner))
            else:
                cursor = self.db.conn.execute('INSERT INTO chat_quick_phrases(title,content,category,sort_order,enabled,image_ids,owner_id) VALUES(?,?,?,?,?,?,?)', (*args, owner))
                phrase_id = cursor.lastrowid
            if commit:
                self.db.conn.commit()
            return phrase_id

    def delete(self, owner, phrase_id):
        with self.db.lock:
            cursor = self.db.conn.execute('DELETE FROM chat_quick_phrases WHERE owner_id=? AND id=?', (owner, phrase_id))
            self.db.conn.commit()
            if not cursor.rowcount:
                raise PermissionError('短语不存在或无权限')

    def export_backup(self, owner=None):
        owners = [owner] if owner is not None else [r[0] for r in self.db.conn.execute('SELECT id FROM users')]
        return [dict(row, owner_id=user) for user in owners for row in self.list(user, True)]

    def restore_backup(self, entries, owner=None, asset_map=None):
        for row in entries:
            target = owner if owner is not None else row.get('owner_id')
            if not self.db.conn.execute('SELECT 1 FROM users WHERE id=?', (target,)).fetchone():
                raise ValueError('快捷短语所属用户不存在')
            value = dict(row, image_ids=[(asset_map or {}).get((target, asset), asset) for asset in image_ids(row.get('image_ids', []))])
            existing = next((r for r in self.list(target, True) if r['title'] == value['title'] and r['category'] == value.get('category', '默认')), None)
            self.save(target, value, existing['id'] if existing else None, commit=False)

    def use(self, owner, phrase_id):
        with self.db.lock:
            cursor = self.db.conn.execute('UPDATE chat_quick_phrases SET use_count=use_count+1 WHERE owner_id=? AND id=? AND enabled=1', (owner, phrase_id))
            self.db.conn.commit()
            return cursor.rowcount == 1
