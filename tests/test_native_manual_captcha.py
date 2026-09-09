"""Offline lifecycle checks; no real website, account or CAPTCHA interaction."""
import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch

from utils import manual_captcha as manual
from utils.captcha_remote_control import captcha_controller


class NativeManualCaptchaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        manual._manual_tasks.clear()
        manual._cancelled_attempts.clear()
        self.page = Mock(is_closed=Mock(return_value=False),
                         goto=AsyncMock(), bring_to_front=AsyncMock())
        self.context = Mock(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=self.page),
                            cookies=AsyncMock(return_value=[{'name': 'unb', 'value': '123'}]),
                            close=AsyncMock())
        self.browser = Mock(new_context=AsyncMock(return_value=self.context),
                            is_connected=Mock(return_value=True), close=AsyncMock())
        self.playwright = Mock(stop=AsyncMock())
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch('playwright.async_api.async_playwright',
            return_value=Mock(start=AsyncMock(return_value=self.playwright))))
        self.launch = self.stack.enter_context(patch.object(manual.browser_limit, 'launch_browser',
            new=AsyncMock(return_value=self.browser)))
        self.fetch = self.stack.enter_context(patch.object(manual, '_fetch_live_verification_url',
            new=AsyncMock(return_value='https://example.invalid/punish')))
        self.stack.enter_context(patch.object(manual, '_wait_for_captcha_present', new=AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(captcha_controller, 'check_completion', new=AsyncMock(return_value=True)))
        self.create = self.stack.enter_context(patch.object(captcha_controller, 'create_session', new=AsyncMock()))
        self.refresh = self.stack.enter_context(patch.object(captcha_controller, 'auto_refresh_screenshot', new=AsyncMock()))
        self.real_sleep = asyncio.sleep
        async def tick(*args):
            await self.real_sleep(0)
        self.stack.enter_context(patch.object(manual.asyncio, 'sleep', side_effect=tick))

    async def run_session(self, **kwargs):
        return await manual.open_manual_session('a', 'unb=123', headless=False,
            attempt_id='attempt', owner=7, **kwargs)

    def assert_clean(self):
        self.context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()
        self.playwright.stop.assert_awaited_once()
        self.assertNotIn('a', manual._manual_tasks)
        self.assertNotIn('a', captcha_controller.active_sessions)

    async def test_window_stays_open_until_verified_save_no_remote_input(self):
        async def finalize(result):
            self.context.close.assert_not_awaited()
            self.assertTrue(result['success'])
            self.assertFalse(await captcha_controller.handle_mouse_event('a', 'move', 20, 30))
            self.page.mouse.move.assert_not_called()
        result = await self.run_session(finalize=finalize)
        self.assertTrue(result['success'])
        self.assertFalse(self.launch.call_args.args[1]['headless'])
        self.page.bring_to_front.assert_awaited_once()
        self.create.assert_not_awaited()
        self.refresh.assert_not_awaited()
        self.fetch.assert_awaited_once()
        self.assert_clean()

    async def test_save_failure_closes_owned_browser_without_claiming_success(self):
        with self.assertRaisesRegex(RuntimeError, 'save failed'):
            await self.run_session(finalize=AsyncMock(side_effect=RuntimeError('save failed')))
        self.assert_clean()

    async def test_user_closes_native_window_before_completion(self):
        async def navigate(*args, **kwargs):
            callback = next(call.args[1] for call in self.page.on.call_args_list if call.args[0] == 'close')
            callback()
        self.page.goto.side_effect = navigate
        save = AsyncMock()
        result = await asyncio.create_task(self.run_session(finalize=save))
        self.assertFalse(result['success'])
        save.assert_not_awaited()
        self.assert_clean()

    async def test_modal_cancel_during_launch_and_old_close_is_isolated(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def navigate(*args, **kwargs):
            entered.set()
            await release.wait()
        self.page.goto.side_effect = navigate
        save = AsyncMock()
        task = asyncio.create_task(self.run_session(finalize=save))
        await entered.wait()
        manual.cancel_manual_session('a', 'old-attempt', 7)
        manual.cancel_manual_session('a', 'attempt', 8)
        await self.real_sleep(0)
        self.assertFalse(task.done())
        duplicate = await self.run_session()
        self.assertFalse(duplicate['success'])
        self.launch.assert_awaited_once()
        manual.cancel_manual_session('a', 'attempt', 7)
        self.assertFalse((await task)['success'])
        save.assert_not_awaited()
        self.assert_clean()

    async def test_cancel_before_start_never_opens_browser(self):
        manual.cancel_manual_session('a', 'attempt', 7)
        self.assertFalse((await self.run_session())['success'])
        self.launch.assert_not_awaited()
        self.fetch.assert_not_awaited()

    async def test_repeated_close_cannot_interrupt_browser_cleanup(self):
        ready, closing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def navigate(*args, **kwargs):
            ready.set()
            await asyncio.Event().wait()
        async def close_context():
            closing.set()
            await finish.wait()
        self.page.goto.side_effect = navigate
        self.context.close.side_effect = close_context
        task = asyncio.create_task(self.run_session())
        await ready.wait()
        manual.cancel_manual_session('a', 'attempt', 7)
        await closing.wait()
        manual.cancel_manual_session('a', 'attempt', 7)
        await self.real_sleep(0)
        self.assertFalse(task.done())
        finish.set()
        self.assertFalse((await task)['success'])
        self.assert_clean()

    async def test_overall_deadline_cancels_pending_browser_and_never_saves(self):
        self.page.goto.side_effect = lambda *a, **k: None
        async def stuck(*args, **kwargs):
            await asyncio.Event().wait()
        self.page.goto.side_effect = stuck
        save = AsyncMock()
        async with asyncio.timeout(0.02):
            result = await self.run_session(finalize=save)
        self.assertFalse(result['success'])
        save.assert_not_awaited()
        self.assert_clean()


class ManualCancelOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_route_checks_account_owner(self):
        from app import reply_server
        db = Mock()
        db.get_all_cookies.return_value = {}
        with patch.object(reply_server, 'db_manager', db), patch.object(manual, 'cancel_manual_session') as cancel:
            with self.assertRaises(Exception) as raised:
                await reply_server.cancel_manual_captcha('a', 'e3e3bfa9-f320-4ef9-a58b-1f6474f10822', {'user_id': 8})
            self.assertEqual(raised.exception.status_code, 404)
            cancel.assert_not_called()
