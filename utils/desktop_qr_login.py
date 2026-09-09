"""Desktop QR login through the full official website in an isolated browser.

The raw QR API can authenticate without granting access to keep-login. Let the
website finish its own login flow, enable its real switch, then prove IM auth.
No personal browser profile, business requests, or credential logging.
"""

import asyncio
import base64
from io import BytesIO
import re
import time
from urllib.parse import urlsplit

import qrcode
import httpx
from loguru import logger

from utils import browser_limit
from utils.platform_session import (
    PlatformSession, future_cookie, marshal_cookies, parse_cookies,
)
from utils.xianyu_utils import generate_device_id


HOME = 'https://www.goofish.com/'
SETTINGS = ('https://passport.goofish.com/ac/account/queryLoginSettings.do'
            '?fromSite=77&appName=xianyu&bizEntrance=web')


class LoginPending(ValueError):
    """A safe, fixed reason code; keep the scanned browser available to the user."""


async def confirm_keep_login_prompt(page):
    """Accept only the official keep-login choice, never a security challenge."""
    for frame in page.frames:
        parts = urlsplit(frame.url)
        if parts.scheme != 'https' or parts.hostname not in {'www.goofish.com', 'passport.goofish.com'}:
            continue
        decline = frame.get_by_text(re.compile(r'^不保持(?:登录)?(?:[（(].*)?$')).filter(visible=True)
        if not await decline.count():
            continue
        # The dialog has a heading as well as a button. Target the positive
        # action only, never the countdown/default "do not keep" choice.
        accept = frame.get_by_role('button', name=re.compile(r'^保持(?:登录)?$')).filter(visible=True)
        if not await accept.count():
            accept = frame.locator('a, input[type="button"], input[type="submit"], [class*="button"], [class*="btn"]').filter(
                has_text=re.compile(r'^保持(?:登录)?$')).filter(visible=True)
        if await accept.count() == 1:
            await accept.click(timeout=1500)
            return True
    return False


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
            headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: '',
            signal: AbortSignal.timeout(5000)});
        if (!response.ok) throw new Error('Login settings unavailable');
        return await response.json();
    }''', SETTINGS)
    settings = payload.get('returnValue') if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get('success') is not True or not isinstance(settings, dict):
        raise LoginPending('settings_unavailable')
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
        try:
            verified = await platform.verify_token()
            if verified.status == 'expired':
                verified = await platform.renew()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise LoginPending('im_transient') from exc
        if verified.status != 'success' or not platform.same_account():
            raise LoginPending(f'im_{verified.status}')
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
    from playwright.async_api import Error as BrowserError
    keep_login_pending = False

    async def on_response(response):
        nonlocal keep_login_pending
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
                if data.get('dialogAction') in {'keepLoginConfirm', 'keepLoginConfirmDialog', 'keepLoginSilentNoticeDialog'}:
                    keep_login_pending = True
                    session.message = '已扫码，正在确认官网的“保持登录”，无需重复扫码。'
            elif parts.path.startswith('/newlogin/'):
                payload = await response.json()
                data = payload.get('content', {}).get('data', {})
                if data.get('processFinished') is True and data.get('resultCode') == 100:
                    keep_login_pending = False
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
                last_credentials = None
                attempts = 0
                next_attempt = 0
                while not session.is_expired():
                    if page.is_closed():
                        session.status = 'cancelled'
                        session.message = '官网窗口已关闭，请重新打开扫码登录。'
                        return
                    try:
                        if await confirm_keep_login_prompt(page):
                            keep_login_pending = False
                            session.extend_for_verification()
                            session.message = '已选择保持登录，正在等待官网保存授权。'
                            logger.info('官网保持登录确认已点击')
                            await asyncio.sleep(0.5)
                            continue
                    except BrowserError:
                        # Frames can be replaced while the official login navigates.
                        await asyncio.sleep(0.2)
                        continue
                    cookies = first_party_cookies(await context.cookies())
                    avatar = page.locator('a[href="https://www.goofish.com/personal"]').first
                    if (cookies.get('unb') and not keep_login_pending
                            and not await modal.is_visible() and await avatar.is_visible()):
                        # Retain the scanned credential before inspecting the switch.
                        session.cookies = cookies
                        if session.unb and session.unb != cookies['unb']:
                            raise ValueError('Login account changed')
                        session.unb = cookies['unb']
                        credentials = tuple(cookies.get(key) for key in (
                            'unb', 'cookie2', 'sgcookie', '_m_h5_tk', '_m_h5_tk_enc',
                            'havana_lgc2_77', 'havana_lgc_exp', 'x5sec'))
                        if credentials != last_credentials:
                            last_credentials, attempts, next_attempt = credentials, 0, 0
                        if attempts >= 3 or time.monotonic() < next_attempt:
                            await asyncio.sleep(0.5)
                            continue
                        session.status = 'processing'
                        session.message = '已登录，正在保存登录信息并验证聊天连接，无需重复扫码。'
                        attempts += 1
                        try:
                            session.cookies, session.long_login_enabled = await collect_verified_login(
                                page, context, session.unb)
                        except (LoginPending, BrowserError) as exc:
                            reason = str(exc) if isinstance(exc, LoginPending) else 'browser_transition'
                            logger.warning(f'官网已扫码，保留窗口等待登录完成: {reason}')
                            session.extend_for_verification()
                            session.message = '已保留本次扫码，请在官网完成保持登录或安全验证；也可刷新官网继续，无需重新扫码。'
                            # A challenge gets no retries with unchanged credentials.
                            if reason.startswith('im_') and reason != 'im_transient':
                                attempts = 3
                            next_attempt = time.monotonic() + 5 * attempts
                            await asyncio.sleep(0.5)
                            continue
                        session.browser_verified = True
                        session.status = 'success'
                        session.created_time = time.time()  # Preserve the result for the consumer.
                        return
                    await asyncio.sleep(0.2)
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
