"""Bounded exact-ID correlation for workbench sends, never text-based deduplication."""
import asyncio
import threading
import time

from app.services.reply_delivery import require_receipt


class OutgoingEchoes:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.requests = {}
        self.messages = {}
        self.observations = {}

    def _prune(self):
        now = self.clock()
        for entries in (self.requests, self.messages):
            for key, value in list(entries.items()):
                if value[-1] <= now:
                    entries.pop(key, None)
            while len(entries) > 2048:
                entries.pop(next(iter(entries)))

    def register(self, cookie_id, mid, cid):
        with self.lock:
            self.requests[(str(cookie_id), str(mid))] = (str(cid).split('@', 1)[0], self.clock() + 120)
            self._prune()

    def resolve(self, cookie_id, response):
        if not isinstance(response, dict) or not isinstance(response.get('headers'), dict):
            return
        with self.lock:
            self._prune()
            pending = self.requests.pop((str(cookie_id), str(response['headers'].get('mid', ''))), None)
            if pending is None:
                return
            try:
                require_receipt(response, explicit_success=True)
            except RuntimeError:
                return
            body = response['body']
            for layer in (body, body.get('data')):
                if not isinstance(layer, dict):
                    continue
                value = layer.get('messageId') or layer.get('msgId')
                if isinstance(value, (str, int)) and not isinstance(value, bool) and 0 < len(str(value)) <= 128:
                    self.messages[(str(cookie_id), pending[0], str(value))] = (self.clock() + 3600,)
            self._prune()

    def contains(self, cookie_id, cid, message_id):
        if not message_id:
            return False
        with self.lock:
            self._prune()
            return (str(cookie_id), str(cid).split('@', 1)[0], str(message_id)) in self.messages

    def unresolved(self, cookie_id, cid):
        """Transient hold while an early self-push waits for its in-flight send receipt."""
        key = (str(cookie_id), str(cid).split('@', 1)[0])
        with self.lock:
            return any(value[:2] == key and value not in self.messages for value in self.observations.values())

    async def wait_settled(self, cookie_id, cid):
        # Only this conversation waits; the WebSocket reader must remain free to resolve receipts.
        while self.unresolved(cookie_id, cid):
            await asyncio.sleep(.02)

    async def match_self_push(self, cookie_id, cid, message_id, timeout=15):
        if self.contains(cookie_id, cid, message_id):
            return True
        if not message_id:
            return False
        key = (str(cookie_id), str(cid).split('@', 1)[0])
        with self.lock:
            self._prune()
            pending = {request for request, value in self.requests.items()
                       if request[0] == key[0] and value[0] == key[1]}
            if not pending:
                return False
            token = object()
            self.observations[token] = (*key, str(message_id))
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.contains(*key, message_id):
                    return True
                with self.lock:
                    if not pending.intersection(self.requests):
                        break
                await asyncio.sleep(.02)
            return self.contains(*key, message_id)
        finally:
            with self.lock:
                self.observations.pop(token, None)


outgoing_echoes = OutgoingEchoes()
