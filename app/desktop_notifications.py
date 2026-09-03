"""Bounded, content-free notification queue shared by account and HTTP threads."""

import hashlib
import json
import math
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

    def configure(self, token, user_id, enabled, sound=False):
        with self.lock:
            if not enabled:
                if self.session and self.session[0] == token:
                    self.session = None
                    self.sound = False
                    self.events.clear()
            elif not self.session or self.session[:2] != (token, user_id):
                self.session = (token, user_id, self.clock())
                self.events.clear()
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
            # Hash the fallback identity; no buyer name, ID or message is retained.
            identity = [account_id, message_id] if message_id else [account_id, sender_id, chat_id, timestamp_ms, content]
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            while self.seen and next(iter(self.seen.values())) < now - self.ttl:
                self.seen.popitem(last=False)
            if key in self.seen:
                return False
            self.seen[key] = now
            while len(self.seen) > self.limit:
                self.seen.popitem(last=False)
            self._append(now, False)
            return True

    def _append(self, now, test):
        self.sequence += 1
        self.events.append((self.sequence, now, test))

    def test(self, token):
        with self.lock:
            if not self.session or self.session[0] != token:
                return False
            # A double click must not flood the notification center.
            now = self.clock()
            if any(test and stamp > now - 5 for _, stamp, test in self.events):
                return False
            self._append(now, True)
            return True

    def poll(self, after, validate):
        with self.lock:
            now = self.clock()
            if self.session and not validate(self.session[0], self.session[1]):
                self.session = None
                self.sound = False
                self.events.clear()
            eligible = [event for event in self.events if event[0] > after and event[1] >= now - self.ttl]
            return {"cursor": self.sequence, "count": sum(not e[2] for e in eligible),
                    "test_count": sum(e[2] for e in eligible), "sound": self.sound}


desktop_notifications = DesktopNotifications()
