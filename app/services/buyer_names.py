"""Display-only, shop-scoped nickname cache; never used to identify an order.

Reuse user_settings without a schema migration. Only platform nicknames are
accepted, never recipient/address fields. Failure must not affect fulfillment.
"""
import json

LIMIT = 2000
PREFIX = "buyer_names:"


def _clean(value):
    value = str(value or "").strip()
    return value[:100] if value and not value.isdecimal() and value not in {"未知用户", "未知买家", "用户"} else ""


def _load(db, owner, cookie_id):
    saved = db.get_user_setting(owner, PREFIX + str(cookie_id))
    try:
        data = json.loads(saved["value"]) if saved else {}
        return {str(k): _clean(v) for k, v in data.items() if _clean(v)} if isinstance(data, dict) else {}
    except (ValueError, TypeError, KeyError):
        return {}


def buyer_names(db, owner, cookie_id):
    try:
        with db.lock:
            row = db.conn.execute("SELECT user_id FROM cookies WHERE id=?", (cookie_id,)).fetchone()
            return _load(db, owner, cookie_id) if row and row[0] == owner else {}
    except Exception:
        return {}


def remember_buyer_names(db, cookie_id, pairs):
    try:
        incoming = {str(uid): _clean(name) for uid, name in pairs if uid and _clean(name)}
        if not incoming:
            return
        with db.lock:
            row = db.conn.execute("SELECT user_id FROM cookies WHERE id=?", (cookie_id,)).fetchone()
            if not row:
                return
            owner = row[0]
            names = _load(db, owner, cookie_id)
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
