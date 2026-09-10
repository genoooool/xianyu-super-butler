"""Optional local OpenCLI connection to the user's existing Chrome profile.

Uses the installed daemon/extension only. No browser restart, profile copying,
cookie injection, fingerprint override, or CAPTCHA automation.
"""
import asyncio
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx

from utils.browser_im_verification import (
    BrowserIMReceipt, IM_APP_KEY, MESSAGE_URL, TOKEN_URL, _token_payload, _official,
)
from utils.platform_session import marshal_cookies

BRIDGE = 'http://127.0.0.1:19825'
BRIDGE_HEADERS = {'X-OpenCLI': '1'}


async def regular_chrome_profile():
    """Read capability only; never start/install a daemon or select another profile."""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            response = await client.get(BRIDGE + '/status', headers=BRIDGE_HEADERS)
            response.raise_for_status()
            status = response.json()
        if (status.get('extensionConnected') and not status.get('profileRequired')
                and not status.get('profileDisconnected') and isinstance(status.get('contextId'), str)):
            return status['contextId']
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return None


def first_party(cookies):
    return {item['name']: item['value'] for item in cookies
            if (item.get('domain', '').lstrip('.') == 'goofish.com'
                or item.get('domain', '').endswith('.goofish.com'))
            and (item.get('expirationDate', -1) == -1 or item['expirationDate'] > time.time())}


def bridge_receipt(entry, cookies, account, started_at):
    """Require a fresh official response and its account-bound SDK device ID."""
    parts, expected = urlsplit(entry.get('url', '')), urlsplit(TOKEN_URL)
    if ((parts.scheme, parts.netloc, parts.path.rstrip('/')) !=
            (expected.scheme, expected.netloc, expected.path.rstrip('/'))
            or entry.get('responseStatus') != 200 or entry.get('timestamp', 0) < started_at
            or entry.get('responseBodyTruncated') or entry.get('requestBodyTruncated')):
        return None
    values = first_party(cookies)
    if not account or values.get('unb') != account:
        return None
    headers = {str(k).lower(): str(v) for k, v in entry.get('requestHeaders', {}).items()}
    if not _official(headers.get('origin') or headers.get('referer', '')):
        return None
    params = parse_qs(parts.query)
    params.update(parse_qs(entry.get('requestBodyPreview') or ''))
    data = json.loads((params.get('data') or ['{}'])[0])
    device_id = data.get('deviceId') if isinstance(data, dict) else None
    if (not isinstance(device_id, str) or not device_id.endswith('-' + account)
            or len(device_id) > 256 or data.get('appKey') != IM_APP_KEY):
        return None
    payload = _token_payload(entry.get('responsePreview', ''))
    if not isinstance(payload, dict):
        return None
    ret, data = payload.get('ret'), payload.get('data')
    token = data.get('accessToken') if isinstance(data, dict) else None
    if (not isinstance(ret, list) or not any(str(x).split('::', 1)[0] == 'SUCCESS' for x in ret)
            or not isinstance(token, str) or not token):
        return None
    return BrowserIMReceipt(account, device_id, marshal_cookies(values), token)


class RegularChromeSession:
    def __init__(self, profile, prepare_url, *, client=None):
        parts = urlsplit(prepare_url)
        if parts.scheme != 'http' or parts.hostname != '127.0.0.1':
            raise ValueError('Chrome preparation page must use the local workbench')
        self.profile = profile
        self.prepare_url = prepare_url
        self.session = 'xianyu-verify-' + uuid.uuid4().hex
        self.page = None
        self.client = client or httpx.AsyncClient(trust_env=False, timeout=25)

    async def command(self, action, **params):
        payload = dict(id=uuid.uuid4().hex, action=action, session=self.session,
                       surface='browser', contextId=self.profile, windowMode='foreground',
                       idleTimeout=600, timeout=15, deadlineAt=int(time.time() * 1000) + 15000)
        payload.update(params)
        response = await self.client.post(BRIDGE + '/command', json=payload, headers=BRIDGE_HEADERS)
        response.raise_for_status()
        result = response.json()
        if not result.get('ok'):
            # Error payloads can contain page URLs/tickets, keep them out of logs/UI.
            raise RuntimeError('常用 Chrome 连接中断或本次标签页已关闭，原授权已保留')
        return result

    async def cookies(self):
        result = await self.command('cookies', url='https://www.goofish.com/', domain='goofish.com')
        data = result.get('data')
        if not isinstance(data, list):
            raise RuntimeError('无法读取常用 Chrome 的闲鱼登录状态')
        return data

    async def open(self, account):
        if first_party(await self.cookies()).get('unb') != account:
            raise RuntimeError('常用 Chrome 当前登录的闲鱼账号不匹配，请先在 Chrome 中登录此账号')
        # Await the creation receipt even on cancellation, so cleanup knows the
        # exact owned tab. Never close a browser process or another session.
        creation = asyncio.create_task(self.command('navigate', url=self.prepare_url))
        try:
            result = await asyncio.shield(creation)
        except asyncio.CancelledError:
            result = await creation
            self.page = result.get('page')
            raise
        self.page = result.get('page')
        if not isinstance(self.page, str) or not self.page:
            raise RuntimeError('Chrome 未返回本次标签页，未继续请求闲鱼')

    async def wait(self, account, timeout):
        await self.open(account)
        await self.command('network-capture-start', page=self.page, pattern='mtop.taobao.idlemessage.pc.login.token/')
        started = time.time() * 1000
        # Direct structured navigation; no OpenCLI Page.goto stealth injection.
        await self.command('navigate', page=self.page, url=MESSAGE_URL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            data = (await self.command('network-capture-read', page=self.page)).get('data', [])
            cookies = await self.cookies()
            if first_party(cookies).get('unb') != account:
                raise RuntimeError('Chrome 中的闲鱼账号已切换，本轮未保存授权')
            for entry in data if isinstance(data, list) else []:
                try:
                    receipt = bridge_receipt(entry, cookies, account, started)
                except (ValueError, TypeError, AttributeError):
                    continue
                if receipt:
                    return receipt
        raise TimeoutError('官网尚未完成聊天认证，原授权已保留')

    async def close(self):
        try:
            if self.page:
                await self.command('tabs', op='close', page=self.page)
        finally:
            await self.client.aclose()
