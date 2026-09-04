"""Display-only, shop-scoped nickname cache; never used to identify an order.

Reuse user_settings without a schema migration. Only platform nicknames are
accepted, never recipient/address fields. Failure must not affect fulfillment.
"""
import json
import re

LIMIT = 2000
PREFIX = "buyer_names:"


def clean_buyer_name(value):
    value = str(value or "").strip()
    # Legacy versions cached notification copy as a nickname. Filter it on read
    # as well as write without editing any order or deleting saved user data.
    normalized = re.sub(r'[\s~～!！。.…]', '', value).lower()
    if normalized in {'快给ta一个评价吧', '快给他一个评价吧', '快给她一个评价吧',
                      '[你已发货]', '[买家已付款]', '[交易关闭]', '[交易成功]'}:
        return ''
    return value[:100] if value and not value.isdecimal() and value not in {"未知用户", "未知买家", "用户"} else ""


def _load(db, owner, cookie_id):
    saved = db.get_user_setting(owner, PREFIX + str(cookie_id))
    try:
        data = json.loads(saved["value"]) if saved else {}
        return {str(k): clean_buyer_name(v) for k, v in data.items() if clean_buyer_name(v)} if isinstance(data, dict) else {}
    except (ValueError, TypeError, KeyError):
        return {}


def buyer_names(db, owner, cookie_id):
    try:
        with db.lock:
            row = db.conn.execute("SELECT user_id FROM cookies WHERE id=?", (cookie_id,)).fetchone()
            return _load(db, owner, cookie_id) if row and row[0] == owner else {}
    except Exception:
        return {}


def remember_buyer_names(db, cookie_id, pairs, *, overwrite=True):
    try:
        incoming = {}
        for uid, name in pairs:
            if uid and clean_buyer_name(name):
                incoming.setdefault(str(uid), clean_buyer_name(name))
        if not incoming:
            return
        with db.lock:
            row = db.conn.execute("SELECT user_id FROM cookies WHERE id=?", (cookie_id,)).fetchone()
            if not row:
                return
            owner = row[0]
            names = _load(db, owner, cookie_id)
            if not overwrite:
                incoming = {uid: name for uid, name in incoming.items() if uid not in names}
            if all(names.get(uid) == name for uid, name in incoming.items()):
                return
            for uid, name in incoming.items():
                names.pop(uid, None)
                names[uid] = name
            names = dict(list(names.items())[-LIMIT:])
            db.set_user_setting(owner, PREFIX + str(cookie_id), json.dumps(names, ensure_ascii=False), "买家昵称显示缓存")
    except Exception:
        # Display enrichment must never abort order/message processing.
        return


def enrich_conversation_names(db, owner, cookie_id, conversations):
    """Historical last-message titles must not replace an already known name."""
    remember_buyer_names(db, cookie_id,
                         [(c.get('otherUserId'), c.get('otherUserName')) for c in conversations], overwrite=False)
    names = buyer_names(db, owner, cookie_id)
    for conversation in conversations:
        uid = conversation.get('otherUserId')
        conversation['otherUserName'] = names.get(uid) or clean_buyer_name(conversation.get('otherUserName'))
