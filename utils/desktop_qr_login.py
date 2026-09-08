"""Desktop QR login through the full official website in an isolated browser.

The raw QR API can authenticate without granting access to keep-login. Let the
website finish its own login flow, enable its real switch, then prove IM auth.
No personal browser profile, business requests, or credential logging.
"""

import asyncio
import base64
from io import BytesIO
import time
from urllib.parse import urlsplit

import qrcode
from loguru import logger

from utils import browser_limit
from utils.platform_session import (
    PlatformSession, future_cookie, marshal_cookies, parse_cookies,
)
from utils.xianyu_utils import generate_device_id


HOME = 'https://www.goofish.com/'
SETTINGS = ('https://passport.goofish.com/ac/account/queryLoginSettings.do'
            '?fromSite=77&appName=xianyu&bizEntrance=web')


def first_party_cookies(items):
    result = {}
    for item in items:
        domain = item['domain'].lstrip('.')
        if (domain == 'goofish.com' or domain.endswith('.goofish.com')) and (
            item.get('expires', -1) == -1 or item['expires'] > time.time()
        ):
            result[item['name']] = item['value']
    return result


async def login_settings(page):
    payload = await page.evaluate('''async url => {
        const response = await fetch(url, {method: 'POST', credentials: 'include',
            headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: ''});
        if (!response.ok) throw new Error('Login settings unavailable');
        return await response.json();
    }''', SETTINGS)
    settings = payload.get('returnValue') if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get('success') is not True or not isinstance(settings, dict):
        raise ValueError('Login settings unavailable')
    return settings


async def collect_verified_login(page, context, account):
    """Return a candidate only after matching identity and real IM authentication."""
    settings = await login_settings(page)
    if settings.get('hasLongTokenLogin') is not True and settings.get('canOpenLongLogin') is True:
        # The text label is not the control; clicking it does not enable saving.
        await page.locator('a[href="https://www.goofish.com/personal"]').first.hover()
        await page.get_by_text('保存登录信息', exact=True).locator('[class*="navBoxWrap"]').click()
        for _ in range(10):
            settings = await login_settings(page)
            if settings.get('hasLongTokenLogin') is True:
                break
            await asyncio.sleep(0.3)
    cookies = first_party_cookies(await context.cookies())
    if not account or cookies.get('unb') != account:
        raise ValueError('Login account changed')
    async with PlatformSession(marshal_cookies(cookies), generate_device_id(account)) as platform:
        verified = await platform.verify_token()
        if verified.status == 'expired':
            verified = await platform.renew()
        if verified.status != 'success' or not platform.same_account():
            raise ValueError(f'IM authentication incomplete: {verified.status}')
        cookies = parse_cookies(verified.cookies)
    long_login = (settings.get('hasLongTokenLogin') is True
                  and future_cookie(cookies, 'havana_lgc_exp')
                  and bool(cookies.get('havana_lgc2_77')))
    return cookies, long_login


async def run_desktop_login(session, ready):
    """Own the browser from QR generation through completion, close on every exit."""
    if session.cancel_requested:
        ready.set()
        return
    from playwright.async_api import async_playwright

    async def on_response(response):
        if session.cancel_requested:
            return
        parts = urlsplit(response.url)
        if parts.hostname != 'passport.goofish.com':
            return
        try:
            if parts.path == '/newlogin/qrcode/generate.do':
                payload = await response.json()
                if session.cancel_requested:
                    return
                code = payload.get('content', {}).get('data', {}).get('codeContent')
                if not isinstance(code, str) or not code:
                    return
                image = BytesIO()
                qrcode.make(code).save(image, format='PNG')
                session.qr_code_url = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode('ascii')
                session.status = 'waiting'
                session.message = '请在闲鱼 App 扫码；官网窗口将自动保存登录信息。'
                ready.set()
            elif parts.path == '/newlogin/qrcode/query.do':
                payload = await response.json()
                if session.cancel_requested:
                    return
                data = payload.get('content', {}).get('data', {})
                if data.get('qrCodeStatus') in {'SCANED', 'SCANNED'}:
                    session.status = 'scanned'
                elif data.get('qrCodeStatus') == 'CONFIRMED':
                    session.status = 'processing'
                    session.message = '已扫码，正在等待官网完成登录；如官网提示验证，请在该窗口完成。'
                    session.extend_for_verification()
        except Exception as exc:
            logger.debug(f'官网扫码响应暂不可用: {type(exc).__name__}')

    browser = None
    try:
        async with async_playwright() as playwright:
            try:
                browser = await browser_limit.launch_browser(
                    playwright, {'headless': False, 'channel': 'chromium'}, '官网扫码登录')
                context = await browser.new_context(viewport={'width': 1280, 'height': 820})
                page = await context.new_page()
                page.set_default_timeout(10000)
                page.on('response', on_response)
                await page.goto(HOME, wait_until='domcontentloaded', timeout=20000)
                modal = page.locator('[class*="login-modal-wrap"]')
                if not await modal.is_visible():
                    try:
                        await page.get_by_text('登录', exact=True).filter(visible=True).click()
                    except Exception:
                        if not await modal.is_visible():
                            raise
                await page.bring_to_front()
                session.created_time = time.time()
                while not session.is_expired():
                    if page.is_closed():
                        session.status = 'cancelled'
                        session.message = '官网窗口已关闭，请重新打开扫码登录。'
                        return
                    cookies = first_party_cookies(await context.cookies())
                    if cookies.get('unb') and not await modal.is_visible():
                        # Retain the scanned credential before inspecting the switch.
                        session.cookies = cookies
                        session.unb = cookies['unb']
                        session.status = 'processing'
                        session.message = '已登录，正在保存登录信息并验证聊天连接，无需重复扫码。'
                        await page.locator('a[href="https://www.goofish.com/personal"]').first.wait_for(state='visible')
                        # Login navigation and the save-settings response can
                        # briefly overlap. Reuse this scan on transient errors.
                        for attempt in range(3):
                            try:
                                session.cookies, session.long_login_enabled = await collect_verified_login(
                                    page, context, session.unb)
                                break
                            except Exception:
                                if attempt == 2:
                                    raise
                                await asyncio.sleep(1)
                        session.browser_verified = True
                        session.status = 'success'
                        session.created_time = time.time()  # Preserve the result for the consumer.
                        return
                    await asyncio.sleep(0.5)
                session.status = 'expired'
                session.message = '官网扫码等待已结束，请重新打开扫码登录。'
            finally:
                if browser is not None:
                    await browser.close()
    except asyncio.CancelledError:
        session.status = 'cancelled'
        raise
    except Exception as exc:
        session.status = 'error'
        session.message = '官网授权未完成，请检查官网连接后重试。'
        logger.warning(f'官网扫码未完成: {type(exc).__name__}')
    finally:
        ready.set()
