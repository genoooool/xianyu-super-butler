import asyncio
import threading
import time
import unittest
import httpx
from unittest.mock import AsyncMock, Mock, patch

from utils import browser_limit, desktop_qr_login
from utils.platform_session import RenewalResult, marshal_cookies
from utils.qr_login import QRLoginManager, QRLoginSession


class DesktopQRTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, long=True):
        cookies = {'unb': 'account-1', 'cookie2': 'session'}
        if long:
            cookies.update(havana_lgc_exp=str(int((time.time() + 86400) * 1000)), havana_lgc2_77='long')
        context = Mock(cookies=AsyncMock(return_value=[
            {'domain': '.goofish.com', 'name': k, 'value': v, 'expires': -1}
            for k, v in cookies.items()
        ]))
        platform = Mock()
        platform.__aenter__ = AsyncMock(return_value=platform)
        platform.__aexit__ = AsyncMock(return_value=None)
        platform.same_account.return_value = True
        platform.verify_token = AsyncMock(return_value=RenewalResult('success', marshal_cookies(cookies)))
        platform.renew = AsyncMock(return_value=RenewalResult('expired'))
        page = Mock()
        avatar = Mock(hover=AsyncMock())
        page.locator.return_value.first = avatar
        switch = Mock(click=AsyncMock())
        page.get_by_text.return_value.locator.return_value = switch
        return page, context, platform, switch

    async def test_enables_actual_switch_and_verifies_long_cookie(self):
        page, context, platform, switch = self.fixture()
        with (patch.object(desktop_qr_login, 'login_settings', new=AsyncMock(side_effect=[
                {'canOpenLongLogin': True, 'hasLongTokenLogin': False},
                {'canOpenLongLogin': True, 'hasLongTokenLogin': True}])),
              patch.object(desktop_qr_login, 'PlatformSession', return_value=platform)):
            cookies, enabled = await desktop_qr_login.collect_verified_login(page, context, 'account-1')
        self.assertTrue(enabled)
        self.assertEqual(cookies['unb'], 'account-1')
        page.get_by_text.return_value.locator.assert_called_once_with('[class*="navBoxWrap"]')
        switch.click.assert_awaited_once()
        platform.verify_token.assert_awaited_once()

    async def test_already_enabled_does_not_toggle_off(self):
        page, context, platform, switch = self.fixture()
        with (patch.object(desktop_qr_login, 'login_settings', new=AsyncMock(return_value={'hasLongTokenLogin': True})),
              patch.object(desktop_qr_login, 'PlatformSession', return_value=platform)):
            _, enabled = await desktop_qr_login.collect_verified_login(page, context, 'account-1')
        self.assertTrue(enabled)
        switch.click.assert_not_awaited()

    async def test_short_session_is_not_reported_as_long(self):
        page, context, platform, switch = self.fixture(long=False)
        with (patch.object(desktop_qr_login, 'login_settings', new=AsyncMock(return_value={'hasLongTokenLogin': True})),
              patch.object(desktop_qr_login, 'PlatformSession', return_value=platform)):
            _, enabled = await desktop_qr_login.collect_verified_login(page, context, 'account-1')
        self.assertFalse(enabled)

    async def test_saved_long_login_can_renew_an_expired_session(self):
        page, context, platform, _ = self.fixture()
        success = platform.verify_token.return_value
        platform.verify_token.return_value = RenewalResult('expired')
        platform.renew.return_value = success
        with (patch.object(desktop_qr_login, 'login_settings', new=AsyncMock(return_value={'hasLongTokenLogin': True})),
              patch.object(desktop_qr_login, 'PlatformSession', return_value=platform)):
            _, enabled = await desktop_qr_login.collect_verified_login(page, context, 'account-1')
        self.assertTrue(enabled)
        platform.renew.assert_awaited_once()

    async def test_account_change_or_invalid_im_never_returns_candidate(self):
        page, context, platform, _ = self.fixture()
        with (patch.object(desktop_qr_login, 'login_settings', new=AsyncMock(return_value={'hasLongTokenLogin': True})),
              patch.object(desktop_qr_login, 'PlatformSession', return_value=platform)):
            with self.assertRaisesRegex(ValueError, 'account changed'):
                await desktop_qr_login.collect_verified_login(page, context, 'different-account')
            platform.verify_token.return_value = RenewalResult('expired')
            with self.assertRaisesRegex(ValueError, 'im_expired'):
                await desktop_qr_login.collect_verified_login(page, context, 'account-1')

    async def test_pending_im_verification_preserves_scanned_browser_until_cancel(self):
        page, context, _, _ = self.fixture()
        page.frames = []
        page.is_closed.return_value = False
        page.goto = page.bring_to_front = AsyncMock()
        modal = Mock(is_visible=AsyncMock(return_value=False))
        avatar = Mock(is_visible=AsyncMock(return_value=True))
        page.locator.side_effect = lambda selector: modal if 'login-modal-wrap' in selector else Mock(first=avatar)
        page.get_by_text.return_value.filter.return_value.click = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        browser = Mock(new_context=AsyncMock(return_value=context), close=AsyncMock())
        playwright = Mock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
        called = asyncio.Event()
        async def pending(*_):
            called.set()
            raise desktop_qr_login.LoginPending('im_verification_required')
        session = QRLoginSession('pending-test')
        with (patch('playwright.async_api.async_playwright', return_value=playwright),
              patch.object(browser_limit, 'launch_browser', new=AsyncMock(return_value=browser)),
              patch.object(desktop_qr_login, 'collect_verified_login', side_effect=pending) as collect):
            task = asyncio.create_task(desktop_qr_login.run_desktop_login(session, asyncio.Event()))
            await asyncio.wait_for(called.wait(), 1)
            await asyncio.sleep(0.55)
            self.assertFalse(task.done())
            self.assertEqual(session.status, 'processing')
            self.assertEqual(session.unb, 'account-1')
            self.assertFalse(session.browser_verified)
            browser.close.assert_not_awaited()
            collect.assert_awaited_once()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        browser.close.assert_awaited_once()

    async def test_network_error_keeps_scan_recoverable(self):
        page, context, platform, _ = self.fixture()
        platform.verify_token.side_effect = httpx.ReadTimeout('request timeout')
        with (patch.object(desktop_qr_login, 'login_settings', new=AsyncMock(return_value={'hasLongTokenLogin': True})),
              patch.object(desktop_qr_login, 'PlatformSession', return_value=platform)):
            with self.assertRaisesRegex(desktop_qr_login.LoginPending, 'im_transient'):
                await desktop_qr_login.collect_verified_login(page, context, 'account-1')

    def test_only_unexpired_first_party_cookies_are_collected(self):
        result = desktop_qr_login.first_party_cookies([
            {'domain': '.goofish.com', 'name': 'unb', 'value': 'ok', 'expires': -1},
            {'domain': 'evilgoofish.com', 'name': 'unb', 'value': 'bad', 'expires': -1},
            {'domain': '.goofish.com', 'name': 'expired', 'value': 'bad', 'expires': 1},
        ])
        self.assertEqual(result, {'unb': 'ok'})

    async def test_desktop_routes_to_full_website_without_raw_api(self):
        manager = QRLoginManager()
        with (patch.dict('os.environ', {'XIANYU_DESKTOP': '1'}),
              patch.object(manager, '_generate_desktop_qr_code', new=AsyncMock(return_value={'success': True})) as website,
              patch.object(manager, '_get_mh5tk', new=AsyncMock()) as raw):
            self.assertTrue((await manager.generate_qr_code())['success'])
        website.assert_awaited_once()
        raw.assert_not_awaited()

    async def test_cancelled_slot_wait_does_not_leak_later_acquisition(self):
        semaphore = threading.Semaphore(0)
        playwright = Mock()
        with patch.object(browser_limit, '_get_semaphore', return_value=semaphore):
            task = asyncio.create_task(browser_limit.launch_browser(playwright))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            semaphore.release()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if semaphore.acquire(blocking=False):
                    break
            else:
                self.fail('Cancelled browser acquisition consumed its slot')
        playwright.chromium.launch.assert_not_called()

    async def test_loading_returns_handle_and_close_waits_for_browser_cleanup(self):
        manager = QRLoginManager()
        started, closed = asyncio.Event(), asyncio.Event()
        async def browser_flow(session, ready):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                closed.set()
        with patch.object(desktop_qr_login, 'run_desktop_login', side_effect=browser_flow):
            result = await manager._generate_desktop_qr_code(user_id=7)
            self.assertEqual(result['status'], 'loading')
            self.assertNotIn('qr_code_url', result)
            await started.wait()
            answers = await asyncio.gather(*[
                manager.cancel_session(result['session_id'], 7) for _ in range(2)
            ])
        self.assertTrue(closed.is_set())
        self.assertTrue(all(answer['success'] for answer in answers))
        self.assertNotIn(result['session_id'], manager._session_tasks)
        self.assertNotIn(result['session_id'], manager.sessions)
        self.assertTrue((await manager.cancel_session(result['session_id'], 7))['success'])

    async def test_immediate_close_prevents_browser_launch(self):
        manager = QRLoginManager()
        with patch.object(desktop_qr_login, 'run_desktop_login', new=AsyncMock()) as run:
            result = await manager._generate_desktop_qr_code(user_id=7)
            await manager.cancel_session(result['session_id'], 7)
        run.assert_not_awaited()
        self.assertNotIn(result['session_id'], manager.sessions)

    async def test_cancel_does_not_close_other_users_or_other_sessions(self):
        manager = QRLoginManager()
        async def browser_flow(session, ready):
            session.qr_code_url = 'data:image/png;base64,test'
            session.status = 'waiting'
            ready.set()
            await asyncio.Event().wait()
        with patch.object(desktop_qr_login, 'run_desktop_login', side_effect=browser_flow):
            first = await manager._generate_desktop_qr_code(user_id=7)
            second = await manager._generate_desktop_qr_code(user_id=8)
            answer = await manager.cancel_session(first['session_id'], 8)
            self.assertEqual(answer['status'], 'forbidden')
            self.assertIn(first['session_id'], manager._session_tasks)
            await manager.cancel_session(first['session_id'], 7)
            self.assertIn(second['session_id'], manager._session_tasks)
            await manager.cancel_session(second['session_id'], 8)

    async def test_close_after_authentication_preserves_verified_result(self):
        manager = QRLoginManager()
        ready_to_close = asyncio.Event()
        cleanup_done = asyncio.Event()
        async def browser_flow(session, ready):
            session.status = 'success'
            session.cookies = {'unb': 'account-1', 'cookie2': 'test'}
            session.unb = 'account-1'
            ready.set()
            ready_to_close.set()
            await asyncio.sleep(0.01)
            cleanup_done.set()
        with patch.object(desktop_qr_login, 'run_desktop_login', side_effect=browser_flow):
            result = await manager._generate_desktop_qr_code(user_id=7)
            await ready_to_close.wait()
            self.assertEqual((await manager.cancel_session(result['session_id'], 7))['status'], 'success')
        self.assertTrue(cleanup_done.is_set())
        self.assertEqual(manager.get_session_cookies(result['session_id'])['unb'], 'account-1')


if __name__ == '__main__':
    unittest.main()
