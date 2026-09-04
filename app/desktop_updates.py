"""In-memory native update bridge and brief business-operation install gate."""
import asyncio
import functools
import threading
import time
import uuid

APP_VERSION = '1.0.1'
DATA_COMPATIBILITY = 1
RELEASES_URL = 'https://github.com/genoooool/xianyu-super-butler/releases'


class UpdateBusy(RuntimeError):
    pass


class UpdateGate:
    def __init__(self, clock=time.monotonic):
        self.lock = threading.RLock()
        self.clock = clock
        self.active = 0
        self.until = 0

    def enter(self):
        with self.lock:
            if self.clock() < self.until:
                raise UpdateBusy('正在安装更新，请稍后操作')
            self.active += 1

    def leave(self):
        with self.lock:
            self.active -= 1

    def prepare(self):
        with self.lock:
            if self.active:
                raise UpdateBusy('仍有消息、发货或页面操作进行中，请稍后再点升级')
            self.until = self.clock() + 30

    def release(self):
        with self.lock:
            self.until = 0

    def operation(self, function):
        @functools.wraps(function)
        async def guarded(*args, **kwargs):
            while True:
                try:
                    self.enter()
                    break
                except UpdateBusy:
                    await asyncio.sleep(0.1)
            try:
                return await function(*args, **kwargs)
            finally:
                self.leave()
        return guarded


class DesktopUpdates:
    def __init__(self, clock=time.monotonic):
        self.lock = threading.RLock()
        self.clock = clock
        self.updated = 0
        self.command = None
        self.owner = None
        self.state = dict(phase='idle', version=APP_VERSION, latest_version='', notes='', error='', progress=0)

    def status(self):
        with self.lock:
            if self.state['phase'] in ('checking', 'downloading') and self.clock() - self.updated > 180:
                self.state.update(phase='error', error='更新服务响应超时，请重新检查')
                self.command = None
                self.owner = None
            return {**self.state, 'releases_url': RELEASES_URL}

    def request(self, action, token, version=''):
        with self.lock:
            self.status()
            if self.state['phase'] in ('checking', 'downloading', 'installing'):
                raise UpdateBusy('更新操作正在进行，请勿重复点击')
            if action == 'install' and (self.state['phase'] != 'available' or version != self.state['latest_version']):
                raise UpdateBusy('版本信息已变化，请重新检查更新')
            self.owner = token
            self.command = dict(id=uuid.uuid4().hex, action=action, version=version)
            self.updated = self.clock()
            self.state.update(phase='checking' if action == 'check' else 'downloading', error='', progress=0)
            return self.status()

    def poll(self, validate):
        with self.lock:
            self.status()
            if not self.command:
                return None
            if not validate(self.owner):
                self.command = None
                self.owner = None
                self.state.update(phase='idle', error='登录已失效，请重新检查更新')
                return None
            if self.command.get('taken'):
                return None
            self.command['taken'] = True
            return {k: self.command[k] for k in ('id', 'action', 'version')}

    def authorize(self, request_id, validate):
        with self.lock:
            return bool(self.command and self.command['id'] == request_id and validate(self.owner))

    def report(self, request_id, status, validate):
        with self.lock:
            if not self.authorize(request_id, validate):
                raise UpdateBusy('更新请求或登录已失效')
            self.updated = self.clock()
            self.state.update(status)
            if status['phase'] not in ('checking', 'downloading', 'installing'):
                self.command = None
                self.owner = None
            return self.status()


update_gate = UpdateGate()
desktop_updates = DesktopUpdates()
