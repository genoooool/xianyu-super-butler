"""Observe first-party IM authentication in the same user-operated browser.

The official message page requests this token via its own MTop client:
https://g.alicdn.com/idle-pc/xy-site/0.0.174/js/p_layout.js
We do not generate a challenge URL, forward it between sessions, or solve it.
"""
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from utils.platform_session import parse_cookies, marshal_cookies, TOKEN_URL

MESSAGE_URL = 'https://www.goofish.com/im'
IM_APP_KEY = '444e9908a51d1cb236a27862abc769c9'


@dataclass(frozen=True)
class BrowserIMReceipt:
    account: str
    device_id: str = field(repr=False)
    cookies: str = field(repr=False)
    token: str = field(repr=False)


def _token_payload(text):
    try:
        return json.loads(text)
    except ValueError:
        # MTop can return either JSON or a JSONP callback, never execute it.
        match = re.fullmatch(r'\s*[A-Za-z_$][\w$]*\s*\((.*)\)\s*;?\s*', text, re.S)
        return json.loads(match[1]) if match else None


def _official(url):
    parts = urlsplit(url)
    host = parts.hostname or ''
    return parts.scheme == 'https' and (host == 'goofish.com' or host.endswith('.goofish.com'))


async def read_browser_im_receipt(response, context, account):
    """Accept only a successful official IM response for the unchanged account."""
    parts, expected = urlsplit(response.url), urlsplit(TOKEN_URL)
    if (parts.scheme, parts.netloc, parts.path.rstrip('/')) != (
            expected.scheme, expected.netloc, expected.path.rstrip('/')):
        return None
    if response.status != 200 or not account:
        return None
    request = response.request
    if not _official(request.frame.url):
        return None
    headers = await request.all_headers()
    if parse_cookies(headers.get('cookie', '')).get('unb') != account:
        return None
    params = parse_qs(parts.query)
    params.update(parse_qs(request.post_data or ''))
    data = json.loads((params.get('data') or ['{}'])[0])
    device_id = data.get('deviceId') if isinstance(data, dict) else None
    if (not isinstance(device_id, str) or not 1 <= len(device_id) <= 256
            or data.get('appKey') != IM_APP_KEY):
        return None
    payload = _token_payload(await response.text())
    if not isinstance(payload, dict):
        return None
    ret, data = payload.get('ret'), payload.get('data')
    if not isinstance(ret, list) or not any(str(code).split('::', 1)[0] == 'SUCCESS' for code in ret):
        return None
    token = data.get('accessToken') if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        return None
    cookies = {}
    for item in await context.cookies():
        domain = item.get('domain', '').lstrip('.')
        if (domain == 'goofish.com' or domain.endswith('.goofish.com')) and (
                item.get('expires', -1) == -1 or item['expires'] > time.time()):
            if item['name'] == 'unb' and item['value'] != account:
                return None
            cookies[item['name']] = item['value']
    if cookies.get('unb') != account:
        return None
    return BrowserIMReceipt(account, device_id, marshal_cookies(cookies), token)


async def wait_for_browser_im(page, context, account, timeout):
    """One official navigation; wait for website authentication, not slider disappearance."""
    ready = asyncio.get_running_loop().create_future()
    tasks = set()
    async def observe(response):
        if ready.done():
            return
        try:
            receipt = await read_browser_im_receipt(response, context, account)
            if receipt and not ready.done():
                ready.set_result(receipt)
        except (ValueError, TypeError, AttributeError):
            # A partial/malformed page response is not authentication proof.
            return
        except Exception:
            # Navigation or a closed frame can invalidate a pending response.
            return
    def on_response(response):
        task = asyncio.create_task(observe(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    page.on('response', on_response)
    try:
        async with asyncio.timeout(timeout):
            try:
                await page.goto(MESSAGE_URL, wait_until='domcontentloaded', timeout=30000)
            except Exception:
                if page.is_closed():
                    raise
                # Keep the same window if its initial navigation timed out.
            await page.bring_to_front()
            return await ready
    finally:
        page.remove_listener('response', on_response)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
