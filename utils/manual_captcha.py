"""User-operated verification with one owned browser per account.

Desktop builds show the real page; server deployments stream the page. The
browser stays alive through the caller's authentication and conditional save.
No automatic CAPTCHA solver or automatic retry is used by this flow.
"""

import asyncio
import os
import time
from collections import deque
from typing import Any, Awaitable, Callable, Dict, Optional

from loguru import logger
from utils import browser_limit

LOGIN_URL = "https://www.goofish.com/"

# 人工操作需要时间，但也不能无限占用浏览器
DEFAULT_TIMEOUT = 300

# 等待滑块真正出现的最长时间（秒）。惩罚页加载本身就要几秒，
# 等不到说明 x5secdata 已失效或该账号当前已不在风控状态。
CAPTCHA_PRESENT_TIMEOUT = 20

# One owner-scoped attempt per account. Cancellation also covers a close that
# arrives just before the start request; an old close never cancels a new attempt.
_manual_tasks = {}
_cancelled_attempts = deque(maxlen=128)


def native_verification_enabled() -> bool:
    return os.getenv('XIANYU_DESKTOP', '').lower() in {'1', 'true', 'yes'}


def has_manual_session(cookie_id: str) -> bool:
    return cookie_id in _manual_tasks


def cancel_manual_session(cookie_id: str, attempt_id: str, owner: int) -> None:
    key = (cookie_id, attempt_id, owner)
    _cancelled_attempts.append((key, time.monotonic()))
    active = _manual_tasks.get(cookie_id)
    if active and active[0] == key and not active[1].cancelling():
        active[1].cancel()


def _attempt_cancelled(key) -> bool:
    return any(saved == key and time.monotonic() - at < 600 for saved, at in _cancelled_attempts)


def get_verification_url(cookie_id: str) -> Optional[str]:
    """从风控日志里取该账号最近一次滑块惩罚的 URL。

    惩罚 URL（``...punish?x5secdata=...&x5step=2&action=captcha``）是 Token
    刷新响应里 ``data.url`` 返回的，写进了 ``risk_control_logs.event_description``。
    只有导航到这个 URL 才会真正弹出滑块 —— 导航首页（原实现的 LOGIN_URL）不会。

    x5secdata 有有效期（约 1 小时），过期后导航会跳到空白页。所以这里只是
    兜底：会话内若触发实时 Token 刷新拿到新 URL 更好，拿不到才用它。
    """
    import re
    from app.db_manager import db_manager

    try:
        with db_manager.lock:
            cur = db_manager.conn.cursor()
            cur.execute(
                "SELECT event_description FROM risk_control_logs "
                "WHERE cookie_id=? AND event_type='slider_captcha' "
                "ORDER BY id DESC LIMIT 1",
                (cookie_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.debug(f"【{cookie_id}】读取惩罚 URL 失败: {exc}")
        return None

    if not row or not row[0]:
        return None
    m = re.search(r"URL: (\S+)", row[0])
    url = m.group(1) if m else None
    if url and ("punish" in url or "action=captcha" in url):
        logger.info(f"【{cookie_id}】从风控日志取到惩罚 URL")
        return url
    return None


async def _fetch_live_verification_url(cookie_id: str, cookies_str: str) -> Optional[str]:
    """主动触发一次 Token 刷新，从响应里拿新鲜的惩罚 URL。

    账号处于风控时，``mtop.taobao.idlemessage.pc.login.token`` 接口会在
    ``data.url`` 返回滑块惩罚页地址；账号正常时返回 accessToken，没有该字段。

    这里只读 URL、**不调用** XianyuLive 的自动解滑块流程（那条路径实测失败），
    避免在人工处理前又被自动重试一遍。
    """
    import json
    import time
    import aiohttp
    from app.config import API_ENDPOINTS
    from utils.xianyu_utils import trans_cookies, generate_sign, generate_device_id

    try:
        cd = trans_cookies(cookies_str)
        if "_m_h5_tk" not in cd:
            return None
        token = cd["_m_h5_tk"].split("_")[0]
        if not token:
            return None

        ts = str(int(time.time() * 1000))
        params = {
            "jsv": "2.7.2", "appKey": "34839810", "t": ts, "sign": "", "v": "1.0",
            "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
            "timeout": "20000", "api": "mtop.taobao.idlemessage.pc.login.token",
            "sessionOption": "AutoLoginOnly",
            "dangerouslySetWindvaneParams": "%5Bobject%20Object%5D",
            "smToken": "token", "queryToken": "sm", "sm": "sm",
            "spm_cnt": "a21ybx.im.0.0", "spm_pre": "a21ybx.home.sidebar.1.4c053da6vYwnmf",
            "log_id": "4c053da6vYwnmf",
        }
        device_id = generate_device_id(cd.get("unb", ""))
        data_val = json.dumps(
            {"appKey": "444e9908a51d1cb236a27862abc769c9", "deviceId": device_id}
        )
        params["sign"] = generate_sign(params["t"], token, data_val)

        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Safari/537.36"
            ),
            "referer": "https://www.goofish.com/",
            "origin": "https://www.goofish.com",
            "cookie": cookies_str,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_ENDPOINTS.get("token"),
                params=params,
                data={"data": data_val},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                payload = json.loads(text)
                ret = payload.get("ret", [])
                if any("SUCCESS" in str(r) for r in ret):
                    # 成功返回 accessToken —— 账号当前不在风控，无惩罚页
                    return None
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                url = data.get("url")
                if url and ("punish" in url or "action=captcha" in url):
                    logger.info(f"【{cookie_id}】实时刷新拿到惩罚 URL")
                    return url
        return None
    except Exception as exc:
        logger.debug(f"【{cookie_id}】实时获取惩罚 URL 失败: {exc}")
        return None


async def open_manual_session(
    cookie_id: str,
    cookies_str: str,
    timeout: int = DEFAULT_TIMEOUT,
    headless: bool = True,
    *,
    attempt_id: str = '',
    owner: int = 0,
    finalize: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """打开一个人工验证会话，等待人在浏览器里完成滑块。

    Args:
        timeout: 等待人工操作的秒数，超时后放弃并关闭浏览器。
        headless: 默认无头 —— 页面通过远程通道推给用户，不需要本地窗口。
        finalize: 页面完成后认证并保存授权；成功或失败后统一关闭本次浏览器。

    Returns:
        ``{"success": bool, "cookies_str": str, "message": str, "session_id": str}``。
        成功时 ``cookies_str`` 是完成验证后的新 Cookie，调用方应保存。
    """
    from playwright.async_api import async_playwright

    from utils.captcha_remote_control import captcha_controller

    session_id = str(cookie_id)
    result: Dict[str, Any] = {
        "success": False,
        "cookies_str": cookies_str,
        "message": "",
        "session_id": session_id,
    }

    key = (cookie_id, attempt_id, owner)
    if _attempt_cancelled(key):
        return {**result, 'message': '已取消人工验证，原授权和等待状态已保留'}
    if cookie_id in _manual_tasks:
        return {**result, 'message': '该账号已有人工验证窗口，请先完成或关闭它'}
    task = asyncio.current_task()
    _manual_tasks[cookie_id] = (key, task)
    playwright = None
    browser = None
    context = None
    refresh_task = None
    finalizing = False
    closing = False
    def browser_closed(*_):
        if not closing and not task.cancelling():
            task.cancel()

    try:
        playwright = await async_playwright().start()
        browser = await browser_limit.launch_browser(
            playwright,
            {'headless': headless,
             # 只用完整版 Chromium。headless=True 默认会去找单独下载的
             # chromium_headless_shell，缺失时报 Executable doesn't exist，
             # 人工验证页面就会卡在「正在服务器上打开验证页面」。
             'channel': 'chromium',
             },
            "人工验证码",
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
        )

        if cookies_str:
            await context.add_cookies(_to_playwright_cookies(cookies_str))

        page = await context.new_page()
        page.on('close', browser_closed)
        browser.on('disconnected', browser_closed)
        if not headless:
            await page.bring_to_front()

        # 惩罚页 URL 优先：只有导航到它才会弹出滑块。
        # 原实现导航到闲鱼首页，首页没有滑块 → check_completion 立刻误判
        # “已完成” → 浏览器秒关，用户连上控制页时会话已不存在。
        #
        # 先触发一次实时 Token 刷新拿最新 URL（账号正在风控才返回）；拿不到
        # 再退回 DB 里最近一次惩罚 URL（可能已过期，等滑块时会发现）。
        verification_url = await _fetch_live_verification_url(cookie_id, cookies_str)
        if not verification_url:
            verification_url = get_verification_url(cookie_id)
        if not verification_url:
            result["message"] = "未找到该账号的滑块惩罚 URL，无法开启人工验证"
            logger.warning(f"【{cookie_id}】{result['message']}")
            return result

        try:
            await page.goto(verification_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            logger.warning(f"【{cookie_id}】导航惩罚页失败: {exc}")
        # 给验证组件一点渲染时间，否则截图可能是空白
        await asyncio.sleep(2)

        # 等滑块真正出现再建会话 —— 找不到滑块说明 x5secdata 已失效，
        # 此时即使建了会话也拖不出结果，尽早告知用户更合适。
        captcha_appeared = await _wait_for_captcha_present(page, timeout=CAPTCHA_PRESENT_TIMEOUT)
        if not captcha_appeared:
            result["message"] = (
                "已导航到惩罚页但未检测到滑块（x5secdata 可能已过期），"
                "本次验证未完成，原授权已保留，请稍后手动重试"
            )
            logger.warning(f"【{cookie_id}】{result['message']}")
            return result

        if headless:
            await captcha_controller.create_session(session_id, page)
            refresh_task = asyncio.create_task(
                captcha_controller.auto_refresh_screenshot(session_id, interval=1.0)
            )
        else:
            # Native input goes directly to the page, with no screenshots or
            # forwarded mouse events competing with the user's drag.
            captcha_controller.active_sessions[session_id] = {
                'page': page, 'completed': False, 'native': True,
            }

        logger.warning(
            f"【{cookie_id}】已开启人工验证会话，请在 {timeout} 秒内于"
            f"{'工作台画面' if headless else '专用官网窗口'}完成人工验证"
        )

        # 关键：会话期间浏览器必须保持存活，等用户拖完才返回。
        # 原实现在这里死等 timeout，且 check_completion 在无滑块时误判完成
        # 直接秒关 —— 改为“滑块出现后，持续等待它消失（完成）或超时”。
        deadline = time.monotonic() + timeout
        completed = False
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            if page.is_closed() or not browser.is_connected():
                result['message'] = '验证窗口已关闭，原授权和等待状态已保留'
                return result
            try:
                if await captcha_controller.check_completion(session_id):
                    completed = True
                    break
            except Exception as exc:
                logger.debug(f"【{cookie_id}】检查验证完成状态失败: {exc}")

        if not completed:
            result["message"] = f"人工验证超时（{timeout} 秒内未完成）"
            logger.warning(f"【{cookie_id}】{result['message']}")
            return result

        new_cookies = _from_playwright_cookies(await context.cookies())
        if not new_cookies:
            result["message"] = "验证已完成但未取到 Cookie"
            return result

        result["success"] = True
        result["cookies_str"] = new_cookies
        result["message"] = "人工验证完成"
        if finalize is not None:
            finalizing = True
            await finalize(result)
            result['message'] = '验证已通过并保存，官网窗口已关闭，账号正在恢复连接'
        logger.info(f"【{cookie_id}】人工验证完成，已取得新 Cookie")
        return result
    except asyncio.CancelledError:
        result['success'] = False
        result['message'] = '已取消人工验证，原授权和等待状态已保留'
        return result
    except Exception as exc:
        if finalizing:
            raise
        result["message"] = f"人工验证页面未能完成（{type(exc).__name__}），原授权已保留"
        logger.error(f"【{cookie_id}】{result['message']}")
        return result
    finally:
        closing = True
        if refresh_task and not refresh_task.done():
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        try:
            from utils.captcha_remote_control import captcha_controller

            await captcha_controller.close_session(session_id)
        except Exception:
            pass
        for closer in (context, browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        if _manual_tasks.get(cookie_id) == (key, task):
            _manual_tasks.pop(cookie_id, None)


async def _wait_for_captcha_present(page, timeout: int = CAPTCHA_PRESENT_TIMEOUT) -> bool:
    """等页面里出现可见的滑块/验证码元素。

    原实现不区分“页面没有滑块”和“滑块验证完成” —— 首页没滑块，于是
    check_completion 误判完成。这里显式等滑块出现，避免把“没弹出滑块”
    当成“已完成”。
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # noqa: F401

    # 与 captcha_remote_control.check_completion 用同一组选择器，保证判断一致
    selectors = [
        "#nocaptcha",
        "#scratch-captcha-btn",
        ".scratch-captcha-container",
        ".scratch-captcha-slider",
        '[id*="captcha"]',
        ".nc-container",
    ]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # 先查主页面，再查 iframe（阿里滑块通常包在 iframe 里）
        for frame in page.frames:
            for selector in selectors:
                try:
                    element = await frame.query_selector(selector)
                    if element and await element.is_visible():
                        logger.info(f"检测到滑块元素: {selector} (frame: {frame.url[:80]})")
                        return True
                except Exception:
                    continue
        await asyncio.sleep(1)
    return False


def _to_playwright_cookies(cookies_str: str) -> list:
    """把 Cookie 字符串转成 Playwright 需要的结构。"""
    cookies = []
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".goofish.com",
            "path": "/",
        })
    return cookies


def _from_playwright_cookies(cookies: list) -> str:
    """把 Playwright 的 Cookie 列表拼回字符串。"""
    pairs = []
    for cookie in cookies or []:
        name = cookie.get("name")
        if not name:
            continue
        pairs.append(f"{name}={cookie.get('value', '')}")
    return "; ".join(pairs)
