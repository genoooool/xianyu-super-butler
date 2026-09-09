"""Startup waits for account authorization instead of reopening verification browsers."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.test_platform_session import BASE, extract_methods, parse_cookies
from utils import risk_control


class AuthorizationPauseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Mock()
        self.subject = extract_methods('XianyuAutoAsync.py', 'XianyuLive',
            {'_authorization_paused', '_defer_platform_verification'},
            {'asyncio': asyncio, 'db_manager': self.db, 'trans_cookies': parse_cookies,
             'logger': Mock()})()
        self.subject.cookie_id = self.subject.myid = 'account-1'
        self.subject.user_id = 7
        self.subject.cookies_str = BASE
        self.subject.current_token = None
        self.subject.needs_relogin = True
        self.subject.relogin_reason = 'expired'
        self.registry = risk_control.RiskControlRegistry()
        self.patch = patch.object(risk_control, 'registry', self.registry)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_expired_cookie_waits_and_accepts_only_new_same_owner_authorization(self):
        new = BASE.replace('old-session', 'new-session')
        for details in (None, {'user_id': 7, 'value': BASE},
                        {'user_id': 8, 'value': new},
                        {'user_id': 7, 'value': new.replace('account-1', 'account-2')}):
            self.db.get_cookie_details.return_value = details
            self.assertTrue(await self.subject._authorization_paused())
            self.assertEqual(self.subject.cookies_str, BASE)
        self.db.get_cookie_details.return_value = {'user_id': 7, 'value': new}
        self.assertFalse(await self.subject._authorization_paused())
        self.assertFalse(self.subject.needs_relogin)
        self.assertEqual(self.subject.cookies_str, new)
        self.assertEqual(self.subject.relogin_reason, '')

    async def test_challenge_blocks_only_affected_account_and_preserves_long_cookie(self):
        self.subject.needs_relogin = False
        self.subject.cookies_str += '; havana_lgc2_77=long; havana_lgc_exp=9999999999999'
        before = self.subject.cookies_str
        await self.subject._defer_platform_verification({'data': {'url': 'https://h5api.m.goofish.com/punish?fake=1'}})
        self.assertTrue(await self.subject._authorization_paused())
        self.assertEqual(self.registry.get('account-1').consecutive_hits, 1)
        self.assertFalse(self.registry.get('account-2').is_blocked)
        self.assertFalse(self.subject.needs_relogin)
        self.assertEqual(self.subject.cookies_str, before)
        self.db.save_cookie.assert_not_called()
        self.assertEqual(self.subject.last_token_refresh_status, 'verification_required')
        self.registry.get('account-1').resolve_verification()
        self.assertFalse(await self.subject._authorization_paused())

    async def test_late_challenge_cannot_hold_replaced_credential(self):
        self.subject.needs_relogin = False
        self.db.require_account_verification.return_value = False
        await self.subject._defer_platform_verification()
        self.assertFalse(await self.subject._authorization_paused())
        self.assertEqual(self.subject.last_token_refresh_status, 'superseded')

    async def test_main_waits_without_connecting_or_password_restart(self):
        subject = extract_methods('XianyuAutoAsync.py', 'XianyuLive', {'main'},
                                  {'asyncio': asyncio, 'logger': Mock(),
                                   'ConnectionState': SimpleNamespace(CLOSED='closed')})()
        subject.cookie_id = 'account-1'
        subject.create_session = AsyncMock()
        subject._authorization_paused = AsyncMock(return_value=True)
        subject._create_websocket_connection = AsyncMock(side_effect=AssertionError('No connection'))
        subject._try_password_login_refresh = AsyncMock(side_effect=AssertionError('No password login'))
        subject._restart_instance = AsyncMock(side_effect=AssertionError('No restart'))
        subject._cancel_background_tasks = AsyncMock()
        subject._cancel_order_status_refreshes = AsyncMock()
        subject.close_session = AsyncMock()
        subject._unregister_instance = Mock()
        subject.background_tasks = set()
        subject._set_connection_state = Mock()
        subject.session = None
        subject.ws = None
        subject.current_token = None
        subject.heartbeat_task = subject.token_refresh_task = None
        subject.cleanup_task = subject.cookie_refresh_task = None
        subject._safe_str = str
        checks = 0
        async def pause(_):
            nonlocal checks
            checks += 1
            if checks == 3:
                raise asyncio.CancelledError()
        subject._interruptible_sleep = AsyncMock(side_effect=pause)
        import sys
        with patch.dict(sys.modules, {'app.cookie_manager': SimpleNamespace(manager=None)}):
            try:
                await subject.main()
            except asyncio.CancelledError:
                pass
        self.assertEqual(checks, 3)
        subject._create_websocket_connection.assert_not_awaited()
        subject._try_password_login_refresh.assert_not_awaited()
        subject._restart_instance.assert_not_awaited()
