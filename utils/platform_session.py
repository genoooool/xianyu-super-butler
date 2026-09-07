"""Goofish's first-party keep-login and silent-login flow.

Protocol references (checked 2026-09-07):
https://g.alicdn.com/idle-pc/xy-site/0.0.174/js/p_layout.js
https://o.alicdn.com/vip/goofish-auto-login/plugin.js
https://x.alicdn.com/vip/havana-nlogin/0.10.36/index.js

No business requests, browser automation, password fallback, or unbounded retries.
The caller owns persistence; a candidate is only returned after IM authentication.
"""

from dataclasses import dataclass, field
import hashlib
import json
import time
from urllib.parse import unquote, urljoin, urlsplit

import httpx


PASSPORT = "https://passport.goofish.com"
TOKEN_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
SETTINGS_PARAMS = {"fromSite": "77", "appName": "xianyu", "bizEntrance": "web"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Origin": "https://www.goofish.com",
    "Referer": "https://www.goofish.com/",
    "Accept": "application/json",
}


def parse_cookies(value):
    return dict(part.strip().split("=", 1) for part in value.split(";") if "=" in part)


def marshal_cookies(cookies):
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def future_cookie(cookies, name):
    try:
        return float(unquote(cookies.get(name, ""))) > time.time() * 1000
    except (ValueError, TypeError):
        return False


def silent_mode(cookies):
    if future_cookie(cookies, "sdkSilent"):
        return "cooldown"
    if future_cookie(cookies, "havana_lgc_exp"):
        return "long_login"
    if future_cookie(cookies, "cookie3_bak_exp"):
        return "backup"
    return "unavailable"


def login_data(payload):
    if not isinstance(payload, dict):
        return {}
    content = payload.get("content")
    if not isinstance(content, dict):
        outer = payload.get("data")
        content = outer.get("content") if isinstance(outer, dict) else None
    data = content.get("data") if isinstance(content, dict) else None
    return data if isinstance(data, dict) else {}


@dataclass
class RenewalResult:
    status: str
    cookies: str = field(default="", repr=False)
    token: str = field(default="", repr=False)


class PlatformSession:
    def __init__(self, cookies, device_id="", *, transport=None):
        self.original = parse_cookies(cookies)
        self.account = self.original.get("unb", "")
        self.device_id = device_id
        jar = httpx.Cookies()
        for key, value in self.original.items():
            jar.set(key, value, domain=".goofish.com", path="/")
        self.client = httpx.AsyncClient(
            cookies=jar, headers=HEADERS, timeout=20,
            follow_redirects=False, transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.client.aclose()

    def cookies(self):
        # Flatten only first-party cookies, matching the workbench's storage format.
        result = {}
        for cookie in self.client.cookies.jar:
            domain = cookie.domain.lstrip(".")
            if domain == "goofish.com" or domain.endswith(".goofish.com"):
                if not cookie.is_expired():
                    result[cookie.name] = cookie.value
        return result

    def same_account(self):
        return bool(self.account) and self.cookies().get("unb") == self.account

    async def post(self, url, **kwargs):
        response = await self.client.post(url, **kwargs)
        response.raise_for_status()
        return response.json()

    async def verify_token(self):
        for attempt in range(2):
            if not self.same_account():
                return RenewalResult("account_mismatch")
            timestamp = str(int(time.time() * 1000))
            data = json.dumps({"appKey": "444e9908a51d1cb236a27862abc769c9", "deviceId": self.device_id}, separators=(",", ":"))
            signing_token = self.cookies().get("_m_h5_tk", "").split("_")[0]
            sign = hashlib.md5(f"{signing_token}&{timestamp}&34839810&{data}".encode()).hexdigest()
            payload = await self.post(TOKEN_URL, params={
                "jsv": "2.7.2", "appKey": "34839810", "t": timestamp,
                "sign": sign, "v": "1.0", "type": "originaljson",
                "accountSite": "xianyu", "dataType": "json",
                "api": "mtop.taobao.idlemessage.pc.login.token", "sessionOption": "AutoLoginOnly",
            }, data={"data": data})
            if not self.same_account():
                return RenewalResult("account_mismatch")
            if not isinstance(payload, dict):
                return RenewalResult("transient")
            ret = payload.get("ret", [])
            codes = {str(item).split("::", 1)[0] for item in ret} if isinstance(ret, list) else set()
            response_data = payload.get("data")
            token = response_data.get("accessToken") if isinstance(response_data, dict) else None
            if "SUCCESS" in codes and isinstance(token, str) and token:
                return RenewalResult("success", marshal_cookies(self.cookies()), token)
            if "FAIL_SYS_SESSION_EXPIRED" in codes:
                return RenewalResult("expired")
            if any("VALIDATE" in code or "RGV587" in code for code in codes):
                return RenewalResult("verification_required")
            if not (attempt == 0 and codes.intersection({"FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EXPIRED", "FAIL_SYS_TOKEN_EMPTY"})):
                return RenewalResult("transient")
        return RenewalResult("transient")

    async def enable_keep_login(self):
        """Equivalent to enabling the official '保存登录信息' switch, then verify."""
        try:
            if not self.same_account():
                return RenewalResult("account_mismatch")
            payload = await self.post(PASSPORT + "/ac/account/queryLoginSettings.do", params=SETTINGS_PARAMS)
            settings = payload.get("returnValue", {}) if isinstance(payload, dict) else {}
            if not isinstance(settings, dict):
                return RenewalResult("transient")
            if settings.get("hasLongTokenLogin") is not True:
                if settings.get("canOpenLongLogin") is not True:
                    return RenewalResult("unavailable")
                payload = await self.post(PASSPORT + "/ac/account/setLoginSettings.do", params=SETTINGS_PARAMS, data={"status": "0"})
                if not isinstance(payload, dict) or payload.get("success") is not True:
                    return RenewalResult("unavailable")
            # A switch acknowledgement is not proof that a local renewable session exists.
            result = await self.verify_token()
            if result.status == "success" and silent_mode(self.cookies()) == "unavailable":
                result.status = "missing_long_token"
            return result
        except (httpx.HTTPError, ValueError, TypeError):
            return RenewalResult("transient")

    async def renew(self):
        mode = silent_mode(self.cookies())
        if mode in {"cooldown", "unavailable"}:
            return RenewalResult(mode)
        params = {"documentReferer": "https://www.goofish.com/", "appName": "xianyu", "appEntrance": "xianyu_sdkSilent", "fromSite": "0"}
        params.update({"ltl": "true"} if mode == "long_login" else {"skipSessionFilter": "true", "c2r": "true"})
        try:
            payload = await self.post(PASSPORT + "/newlogin/silentHasLogin.do", params=params)
            data = login_data(payload)
            if data.get("processFinished") is not True or data.get("resultCode") != 100:
                return RenewalResult("verification_required" if data.get("iframeRedirect") or data.get("dialogAction") else "transient")
            return await self.verify_token()
        except (httpx.HTTPError, ValueError, TypeError):
            return RenewalResult("transient")

    async def confirm_keep_login(self, payload):
        """Finish only the official keep-login confirmation returned after a QR scan."""
        data = login_data(payload)
        if data.get("dialogAction") not in {"keepLoginConfirm", "keepLoginConfirmDialog", "keepLoginSilentNoticeDialog"}:
            return payload
        feedback = data.get("feedbackPayload") or {}
        urls = feedback.get("dynamicUrl") if isinstance(feedback, dict) else None
        url = urls.get("dialogConfirmLoginUrl") if isinstance(urls, dict) else None
        if not isinstance(url, str):
            raise ValueError("Missing keep-login confirmation URL")
        url = urljoin(PASSPORT, url)
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc != "passport.goofish.com"
                or not parts.path.startswith("/newlogin/") or parts.fragment):
            raise ValueError("Unexpected keep-login confirmation URL")
        result = await self.post(url)
        if login_data(result).get("resultCode") != 100:
            raise ValueError("Keep-login confirmation incomplete")
        return result
