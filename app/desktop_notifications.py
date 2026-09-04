"""Bounded, content-free notification queue shared by account and HTTP threads."""

import hashlib
import json
import math
import secrets
import threading
import time
from collections import deque, OrderedDict


class DesktopNotifications:
    def __init__(self, clock=time.time, limit=512, ttl=90):
        self.clock = clock
        self.limit = limit
        self.ttl = ttl
        self.lock = threading.Lock()
        self.events = deque(maxlen=limit)
        self.seen = OrderedDict()
        self.sequence = 0
        self.session = None
        self.sound = False
        # Only opaque tickets leave through native polling. Targets stay local,
        # bounded, short-lived and bound to the current login.
        self.targets = OrderedDict()
        self.event_targets = OrderedDict()
        self.activation = None

    def _clear(self):
        self.events.clear()
        self.targets.clear()
        self.event_targets.clear()
        self.activation = None

    def configure(self, token, user_id, enabled, sound=False):
        with self.lock:
            if not enabled:
                if self.session and self.session[0] == token:
                    self.session = None
                    self.sound = False
                    self._clear()
            elif not self.session or self.session[:2] != (token, user_id):
                self.session = (token, user_id, self.clock())
                self._clear()
            if enabled:
                self.sound = bool(sound)
            return {"available": True, "active": bool(self.session and self.session[0] == token)}

    def status(self, token):
        with self.lock:
            return {"available": True, "active": bool(self.session and self.session[0] == token)}

    def publish(self, *, user_id, account_id, sender_id, own_id, chat_id,
                timestamp_ms, message_id, content, session_type="1", filtered=False):
        # Keep this entry point defensive: it must never disrupt reply/delivery.
        if filtered or str(session_type) == "30" or not sender_id or str(sender_id) == str(own_id):
            return False
        if not content or content in {"发来一条消息", "发来一条新消息"}:
            return False
        try:
            sent_at = float(timestamp_ms) / 1000
        except (ValueError, TypeError):
            return False
        if not math.isfinite(sent_at):
            return False
        with self.lock:
            now = self.clock()
            if not self.session or self.session[1] != user_id:
                return False
            if sent_at < max(self.session[2], now - self.ttl) or sent_at > now + 60:
                return False
            # Dedup contains no raw content; routing IDs live only in the local map.
            identity = [account_id, message_id] if message_id else [account_id, sender_id, chat_id, timestamp_ms, content]
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            while self.seen and next(iter(self.seen.values())) < now - self.ttl:
                self.seen.popitem(last=False)
            if key in self.seen:
                return False
            self.seen[key] = now
            while len(self.seen) > self.limit:
                self.seen.popitem(last=False)
            self._append(now, False, dict(account_id=str(account_id), chat_id=str(chat_id).split('@')[0], buyer_id=str(sender_id)))
            return True

    def _append(self, now, test, target=None):
        self.sequence += 1
        self.events.append((self.sequence, now, test))
        if target and target.get('chat_id'):
            ticket = secrets.token_hex(16)
            self.targets[ticket] = (now, target)
            self.event_targets[self.sequence] = ticket
            while len(self.targets) > self.limit:
                self.targets.popitem(last=False)
            while len(self.event_targets) > self.limit:
                self.event_targets.popitem(last=False)

    def publish_handoff(self, *, user_id, identity, buyer_id=''):
        """Separate generic alert; ordinary-message dedup must not swallow a takeover."""
        with self.lock:
            if not self.session or self.session[1] != user_id:
                return False
            now = self.clock()
            key = hashlib.sha256(json.dumps(['handoff', identity], ensure_ascii=False).encode()).hexdigest()
            if key in self.seen:
                return False
            self.seen[key] = now
            while len(self.seen) > self.limit:
                self.seen.popitem(last=False)
            self._append(now, 'handoff', dict(account_id=str(identity[0]), chat_id=str(identity[1]).split('@')[0], buyer_id=str(buyer_id)))
            return True

    def test(self, token):
        with self.lock:
            if not self.session or self.session[0] != token:
                return False
            # A double click must not flood the notification center.
            now = self.clock()
            if any(test is True and stamp > now - 5 for _, stamp, test in self.events):
                return False
            self._append(now, True)
            return True

    def poll(self, after, validate):
        with self.lock:
            now = self.clock()
            if self.session and not validate(self.session[0], self.session[1]):
                self.session = None
                self.sound = False
                self._clear()
            eligible = [event for event in self.events if event[0] > after and event[1] >= now - self.ttl]
            result = {"cursor": self.sequence, "count": sum(e[2] is False for e in eligible),
                    "test_count": sum(e[2] is True for e in eligible),
                    "handoff_count": sum(e[2] == 'handoff' for e in eligible), "sound": self.sound}
            # The title and click destination must refer to the same priority.
            candidates = [e for e in eligible if e[2] == 'handoff'] or [e for e in eligible if e[2] is False]
            if candidates:
                result['target'] = self.event_targets.get(candidates[-1][0], '')
            return result

    def activate(self, ticket, validate):
        with self.lock:
            if not self.session or not validate(*self.session[:2]):
                self._clear()
                return False
            saved = self.targets.get(ticket)
            target = saved[1].copy() if saved and saved[0] >= self.clock() - 86400 else {}
            # Expired/generic notifications open the message center, never guess.
            self.activation = dict(id=secrets.token_hex(16), **target)
            return True

    def take_activation(self, token, owner, owns_account):
        with self.lock:
            if not self.session or self.session[:2] != (token, owner):
                return None
            result, self.activation = self.activation, None
            if result and result.get('account_id') and not owns_account(result['account_id']):
                return {'id': result['id']}
            return result


desktop_notifications = DesktopNotifications()
