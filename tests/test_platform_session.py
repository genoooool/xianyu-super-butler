import asyncio
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs

import httpx

from utils.platform_session import (
    PlatformSession, RenewalResult, TOKEN_URL, marshal_cookies, parse_cookies, silent_mode,
)


FUTURE = str(int((time.time() + 86400 * 30) * 1000))
BASE = 'unb=account-1; cookie2=old-session; _m_h5_tk=old-sign_1'
LONG = BASE + '; havana_lgc_exp=' + FUTURE + '; havana_lgc=long-secret'
SUCCESS = {'ret': ['SUCCESS::调用成功'], 'data': {'accessToken': 'fresh-access'}}
LOGIN = {'content': {'data': {'resultCode': 100, 'processFinished': True}}}
ROOT = Path(__file__).resolve().parents[1]


class SessionProtocolTests(unittest.IsolatedAsyncioTestCase):
    def client(self, cookies, responses):
        self.requests = []
        def handle(request):
            self.requests.append(request)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return PlatformSession(cookies, 'device-1', transport=httpx.MockTransport(handle))

    async def test_silent_login_rotates_then_proves_im_authentication(self):
        async with self.client(LONG, [
            httpx.Response(200, json=LOGIN, headers={'set-cookie': 'cookie2=new-session; Domain=.goofish.com; Path=/'}),
            httpx.Response(200, json=SUCCESS),
        ]) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'success')
        self.assertEqual(parse_cookies(result.cookies)['cookie2'], 'new-session')
        self.assertEqual(result.token, 'fresh-access')
        self.assertEqual(self.requests[0].url.params['ltl'], 'true')
        self.assertEqual(str(self.requests[1].url).split('?')[0], TOKEN_URL)
        self.assertIn('cookie2=new-session', self.requests[1].headers['cookie'])

    async def test_signature_expiry_retries_with_new_signature_once(self):
        async with self.client(LONG, [
            httpx.Response(200, json=LOGIN),
            httpx.Response(200, json={'ret': ['FAIL_SYS_TOKEN_EXOIRED::令牌过期']}, headers={'set-cookie': '_m_h5_tk=new-sign_99; Domain=.goofish.com; Path=/'}),
            httpx.Response(200, json=SUCCESS),
        ]) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'success')
        request = self.requests[-1]
        body = parse_qs(request.content.decode())['data'][0]
        expected = hashlib.md5(f"new-sign&{request.url.params['t']}&34839810&{body}".encode()).hexdigest()
        self.assertEqual(request.url.params['sign'], expected)
        self.assertEqual(len(self.requests), 3)

    async def test_signature_retry_exhaustion_is_bounded(self):
        expired = httpx.Response(200, json={'ret': ['FAIL_SYS_TOKEN_EXOIRED::令牌过期']})
        async with self.client(LONG, [httpx.Response(200, json=LOGIN), expired, expired]) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'transient')
        self.assertEqual(len(self.requests), 3)

    async def test_missing_long_token_does_not_probe_or_pretend_success(self):
        async with self.client(BASE, []) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'unavailable')
        self.assertFalse(self.requests)
        self.assertFalse(result.cookies)

    async def test_official_cooldown_is_respected(self):
        async with self.client(LONG + '; sdkSilent=' + FUTURE, []) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'cooldown')
        self.assertFalse(self.requests)

    async def test_backup_mode_uses_official_parameters(self):
        async with self.client(BASE + '; cookie3_bak_exp=' + FUTURE, [httpx.Response(200, json=LOGIN), httpx.Response(200, json=SUCCESS)]) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'success')
        self.assertEqual(self.requests[0].url.params['c2r'], 'true')
        self.assertEqual(self.requests[0].url.params['skipSessionFilter'], 'true')
        self.assertNotIn('ltl', self.requests[0].url.params)

    async def test_login_acknowledgement_is_insufficient(self):
        async with self.client(LONG, [httpx.Response(200, json=LOGIN), httpx.Response(200, json={'ret': ['FAIL_SYS_SESSION_EXPIRED::Session过期']})]) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'expired')
        self.assertFalse(result.cookies)

    async def test_network_failure_preserves_credential_and_does_not_require_scan(self):
        async with self.client(LONG, [httpx.ReadTimeout('network')]) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'transient')
        self.assertFalse(result.cookies)

    async def test_ambiguous_silent_reply_is_not_an_expiry_claim(self):
        async with self.client(LONG, [httpx.Response(200, json={})]) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'transient')

    async def test_challenge_requires_user_action_without_extra_requests(self):
        async with self.client(LONG, [httpx.Response(200, json={'content': {'data': {'iframeRedirect': True}}})]) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'verification_required')
        self.assertEqual(len(self.requests), 1)

    async def test_account_switch_is_never_accepted(self):
        async with self.client(LONG, [httpx.Response(200, json=LOGIN, headers={'set-cookie': 'unb=account-2; Domain=.goofish.com; Path=/'})]) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'account_mismatch')
        self.assertFalse(result.cookies)
        self.assertEqual(len(self.requests), 1)

    async def test_enable_switch_keeps_new_long_cookie_and_checks_token(self):
        async with self.client(BASE, [
            httpx.Response(200, json={'returnValue': {'canOpenLongLogin': True, 'hasLongTokenLogin': False}}),
            httpx.Response(200, json={'success': True}, headers={'set-cookie': f'havana_lgc_exp={FUTURE}; Domain=.goofish.com; Path=/'}),
            httpx.Response(200, json=SUCCESS),
        ]) as client:
            result = await client.enable_keep_login()
        self.assertEqual(result.status, 'success')
        self.assertEqual(self.requests[1].content, b'status=0')
        self.assertEqual(silent_mode(parse_cookies(result.cookies)), 'long_login')

    async def test_enabled_switch_without_local_long_cookie_is_not_renewal_success(self):
        async with self.client(BASE, [httpx.Response(200, json={'returnValue': {'hasLongTokenLogin': True}}), httpx.Response(200, json=SUCCESS)]) as client:
            result = await client.enable_keep_login()
        self.assertEqual(result.status, 'missing_long_token')
        self.assertTrue(result.token)

    async def test_platform_ineligible_account_is_not_forced(self):
        async with self.client(BASE, [httpx.Response(200, json={'returnValue': {'canOpenLongLogin': False, 'hasLongTokenLogin': False}})]) as client:
            result = await client.enable_keep_login()
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(len(self.requests), 1)

    async def test_qr_keep_login_confirmation_finishes_before_success(self):
        payload = {'content': {'data': {'dialogAction': 'keepLoginConfirm', 'feedbackPayload': {'dynamicUrl': {'dialogConfirmLoginUrl': '/newlogin/keepLogin.do?ticket=fake'}}}}}
        async with self.client(BASE, [httpx.Response(200, json=LOGIN, headers={'set-cookie': f'havana_lgc_exp={FUTURE}; Domain=.goofish.com; Path=/'})]) as client:
            result = await client.confirm_keep_login(payload)
            self.assertIn('havana_lgc_exp', client.cookies())
        self.assertEqual(result, LOGIN)

    async def test_confirmation_never_sends_credentials_to_untrusted_destination(self):
        for url in ('https://evil.test/newlogin/x', 'https://passport.goofish.com.evil.test/newlogin/x', 'http://passport.goofish.com/newlogin/x', 'https://passport.goofish.com/ac/account/delete.do'):
            payload = {'content': {'data': {'dialogAction': 'keepLoginConfirm', 'feedbackPayload': {'dynamicUrl': {'dialogConfirmLoginUrl': url}}}}}
            async with self.client(BASE, []) as client:
                with self.assertRaises(ValueError):
                    await client.confirm_keep_login(payload)
            self.assertFalse(self.requests)

    async def test_redirects_are_not_followed(self):
        async with self.client(LONG, [httpx.Response(302, headers={'location': 'https://evil.test/'})]) as client:
            result = await client.renew()
        self.assertEqual(result.status, 'transient')
        self.assertEqual(len(self.requests), 1)

    def test_result_repr_does_not_contain_credentials(self):
        self.assertEqual(repr(RenewalResult('success', 'secret-cookie', 'secret-token')), "RenewalResult(status='success')")


def extract_methods(filename, class_name, names, namespace):
    tree = ast.parse((ROOT / filename).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    body = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for method in body:
        method.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='Subject', bases=[], keywords=[], body=body, decorator_list=[])], type_ignores=[]))
    exec(compile(module, filename, 'exec'), namespace)
    return namespace['Subject']


class AtomicPersistenceTests(unittest.TestCase):
    def setUp(self):
        subject = extract_methods('app/db_manager.py', 'DBManager', {'compare_and_update_cookie'}, {'logger': Mock()})
        self.db = subject()
        self.db.lock = threading.Lock()
        self.db.conn = sqlite3.connect(':memory:')
        self.db.conn.execute('CREATE TABLE cookies (id TEXT PRIMARY KEY, value TEXT, user_id INTEGER, note TEXT)')
        self.db.conn.execute("INSERT INTO cookies VALUES ('account-1', 'old', 7, 'preserved')")
        self.db.conn.commit()

    def tearDown(self):
        self.db.conn.close()

    def test_only_matching_owner_and_generation_can_rotate(self):
        self.assertFalse(self.db.compare_and_update_cookie('account-1', 'old', 'new', 8))
        self.assertTrue(self.db.compare_and_update_cookie('account-1', 'old', 'new', 7))
        self.assertFalse(self.db.compare_and_update_cookie('account-1', 'old', 'stale', 7))
        self.assertEqual(self.db.conn.execute('SELECT value,user_id,note FROM cookies').fetchone(), ('new', 7, 'preserved'))

    def test_deleted_account_is_not_resurrected(self):
        self.assertFalse(self.db.compare_and_update_cookie('missing', 'old', 'new', 7))
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM cookies').fetchone()[0], 1)


class RenewalIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def subject(self, names):
        return extract_methods('XianyuAutoAsync.py', 'XianyuLive', names, {'asyncio': asyncio, 'logger': Mock(), 'time': time})()

    async def test_concurrent_renewals_serialize(self):
        subject = self.subject({'refresh_token'})
        subject.cookie_refresh_lock = asyncio.Lock()
        running = 0
        peak = 0
        async def refresh(_):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            running -= 1
            return 'token'
        subject._refresh_token_impl = refresh
        self.assertEqual(await asyncio.gather(subject.refresh_token(), subject.refresh_token()), ['token', 'token'])
        self.assertEqual(peak, 1)

    async def test_periodic_check_reconnects_only_after_verified_token(self):
        subject = self.subject({'_execute_cookie_refresh'})
        subject.cookie_id = 'account-1'
        subject.ws = SimpleNamespace(closed=False, close=AsyncMock())
        subject.refresh_token = AsyncMock(return_value=None)
        await subject._execute_cookie_refresh(123)
        subject.ws.close.assert_not_awaited()
        self.assertEqual(subject.last_cookie_refresh_time, 123)
        subject.refresh_token.return_value = 'verified'
        await subject._execute_cookie_refresh(456)
        subject.ws.close.assert_awaited_once()
        self.assertTrue(subject.connection_restart_flag)

    async def run_expired_flow(self, renewal_status):
        import sys
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def context(value):
            yield value
        response = SimpleNamespace(status=200, headers={}, json=AsyncMock(return_value={'ret': ['FAIL_SYS_SESSION_EXPIRED']}))
        post = Mock(return_value=context(response))
        client = SimpleNamespace(post=post)
        db = SimpleNamespace(get_cookie_details=Mock(return_value={'value': BASE, 'user_id': 7}))
        guard = SimpleNamespace(is_blocked=False, reset=Mock())
        risk = SimpleNamespace(registry=SimpleNamespace(get=Mock(return_value=guard)))
        namespace = {
            'asyncio': asyncio, 'time': time, 'json': json, 'logger': Mock(),
            'trans_cookies': parse_cookies, 'generate_sign': lambda *_: 'signature',
            'API_ENDPOINTS': {'token': TOKEN_URL},
            'aiohttp': SimpleNamespace(ClientSession=lambda: context(client), ClientTimeout=lambda **_: None),
        }
        subject = extract_methods('XianyuAutoAsync.py', 'XianyuLive', {'_refresh_token_impl'}, namespace)()
        subject.cookie_id = 'account-1'
        subject.myid = 'account-1'
        subject.user_id = 7
        subject.device_id = 'device-1'
        subject.cookies_str = BASE.replace('old-session', 'stale-session')
        subject.cookies = parse_cookies(subject.cookies_str)
        subject.max_captcha_verification_count = 3
        subject.last_message_received_time = 0
        subject.message_cookie_refresh_cooldown = 300
        subject.needs_relogin = False
        subject.current_token = 'restored-token'
        subject._need_captcha_verification = Mock(return_value=False)
        subject._renew_platform_login = AsyncMock(return_value=renewal_status)
        subject._try_password_login_refresh = AsyncMock(return_value=False)
        subject.send_token_refresh_notification = AsyncMock()
        subject._safe_str = str
        with patch.dict(sys.modules, {'app.db_manager': SimpleNamespace(db_manager=db), 'utils.risk_control': risk}):
            result = await subject._refresh_token_impl()
        self.assertIn('cookie2=old-session', post.call_args.kwargs['headers']['cookie'])
        subject._renew_platform_login.assert_awaited_once()
        return subject, result

    async def test_real_session_expiry_tries_silent_recovery_before_password(self):
        subject, token = await self.run_expired_flow('success')
        self.assertEqual(token, 'restored-token')
        subject._try_password_login_refresh.assert_not_awaited()
        self.assertFalse(subject.needs_relogin)

    async def test_recovery_network_failure_does_not_fall_through_to_password(self):
        subject, token = await self.run_expired_flow('transient')
        self.assertIsNone(token)
        subject._try_password_login_refresh.assert_not_awaited()
        self.assertFalse(subject.needs_relogin)

    async def test_no_recoverable_credential_finally_requests_scan(self):
        subject, token = await self.run_expired_flow('unavailable')
        self.assertIsNone(token)
        subject._try_password_login_refresh.assert_awaited_once()
        self.assertTrue(subject.needs_relogin)

    async def test_qr_monitor_waits_for_keep_login_confirmation(self):
        from utils.qr_login import QRLoginManager, QRLoginSession
        manager = QRLoginManager()
        qr = QRLoginSession('keep-login-test')
        manager.sessions[qr.session_id] = qr
        scanned = {'content': {'data': {'qrCodeStatus': 'CONFIRMED', 'dialogAction': 'keepLoginConfirm'}}}
        manager._poll_qrcode_status = AsyncMock(return_value=SimpleNamespace(cookies={'unb': 'account-1', 'cookie2': 'old'}, json=lambda: scanned))
        release = asyncio.Event()
        async def confirm(_):
            await release.wait()
            return LOGIN
        platform = Mock()
        platform.__aenter__ = AsyncMock(return_value=platform)
        platform.__aexit__ = AsyncMock(return_value=None)
        platform.confirm_keep_login = AsyncMock(side_effect=confirm)
        platform.cookies.return_value = parse_cookies(LONG)
        manager._discard_session_later = AsyncMock()
        with patch('utils.platform_session.PlatformSession', return_value=platform):
            task = asyncio.create_task(manager._monitor_qr_status(qr.session_id))
            for _ in range(10):
                await asyncio.sleep(0)
                if platform.confirm_keep_login.await_count:
                    break
            self.assertNotEqual(qr.status, 'success')
            release.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(qr.status, 'success')
        self.assertIn('havana_lgc_exp', qr.cookies)


if __name__ == '__main__':
    unittest.main()
