"""Offline regression for persisted verification holds and owner-safe completion."""
import asyncio
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch, AsyncMock
from types import SimpleNamespace

from tests.test_platform_session import extract_methods
from utils.risk_control import RiskControlRegistry, RiskControlBlocked


class VerificationHoldTests(unittest.TestCase):
    def setUp(self):
        self.db = extract_methods('app/db_manager.py', 'DBManager', {
            'get_account_verification_required', 'require_account_verification',
            'compare_and_update_cookie'}, {'logger': Mock()})()
        self.db.lock = threading.RLock()
        self.db.conn = sqlite3.connect(':memory:')
        self.db.conn.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE cookies (id TEXT PRIMARY KEY, value TEXT, user_id INTEGER, note TEXT);
            CREATE TABLE user_settings (user_id INTEGER, key TEXT, value TEXT,
                description TEXT, updated_at TEXT, UNIQUE(user_id,key));
            CREATE TABLE child (account TEXT REFERENCES cookies(id) ON DELETE CASCADE, value TEXT);
            INSERT INTO cookies VALUES ('a','old',7,'keep'), ('b','other',8,'keep-b');
            INSERT INTO child VALUES ('a','business-data');
        ''')
        self.addCleanup(self.db.conn.close)

    def registry(self):
        return RiskControlRegistry(load_verification=self.db.get_account_verification_required)

    def test_restart_time_and_unrelated_success_do_not_release_hold(self):
        self.assertTrue(self.db.require_account_verification('a', 'old', 7))
        for _ in range(3):
            registry = self.registry()
            guard = registry.get('a')
            with patch('utils.risk_control.time.monotonic', return_value=10**12):
                guard.reset()
                self.assertTrue(guard.is_blocked)
                self.assertTrue(guard.snapshot()['verification_required'])
                with self.assertRaises(RiskControlBlocked):
                    asyncio.run(guard.acquire())
            self.assertFalse(registry.get('b').is_blocked)

    def test_only_current_owner_and_credential_can_save_and_resume(self):
        self.assertFalse(self.db.require_account_verification('a', 'old', 8))
        self.assertTrue(self.db.require_account_verification('a', 'old', 7))
        self.assertFalse(self.db.compare_and_update_cookie('a', 'old', 'bad', 8, clear_verification=True))
        self.assertFalse(self.db.compare_and_update_cookie('a', 'stale', 'bad', 7, clear_verification=True))
        self.assertTrue(self.registry().get('a').is_blocked)
        self.assertTrue(self.db.compare_and_update_cookie('a', 'old', 'verified', 7, clear_verification=True))
        self.assertFalse(self.registry().get('a').is_blocked)
        self.assertFalse(self.db.require_account_verification('a', 'old', 7))
        self.assertEqual(self.db.conn.execute('SELECT * FROM child').fetchall(), [('a','business-data')])
        self.assertEqual(self.db.conn.execute('SELECT * FROM cookies ORDER BY id').fetchall(),
                         [('a','verified',7,'keep'), ('b','other',8,'keep-b')])

    def test_normal_cookie_rotation_does_not_clear_verification(self):
        self.db.require_account_verification('a', 'old', 7)
        self.assertTrue(self.db.compare_and_update_cookie('a', 'old', 'rotated', 7))
        self.assertTrue(self.registry().get('a').is_blocked)

    def test_owner_change_does_not_inherit_another_owners_hold(self):
        self.db.require_account_verification('a', 'old', 7)
        self.db.conn.execute("UPDATE cookies SET user_id=8 WHERE id='a'")
        self.assertFalse(self.registry().get('a').is_blocked)

    def test_state_read_failure_stops_requests(self):
        registry = RiskControlRegistry(load_verification=Mock(side_effect=RuntimeError('offline db error')))
        self.assertTrue(registry.get('a').is_blocked)


class ManualVerificationReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def attempt(self, *, status='success', stored=True, other_account=False):
        from app import reply_server
        from utils import risk_control
        from utils.platform_session import RenewalResult
        registry = RiskControlRegistry()
        registry.get('a').require_verification()
        db = Mock()
        db.get_all_cookies.return_value = {'a': 'unb=123; cookie2=old'}
        db.compare_and_update_cookie.return_value = stored
        instance = SimpleNamespace(device_id='existing-device', needs_relogin=True, relogin_reason='old')
        manager = SimpleNamespace(instances={'a': instance}, cookies={})
        session = Mock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
        session.__aenter__.return_value = session
        session.same_account.return_value = True
        session.verify_token = AsyncMock(return_value=RenewalResult(status,
            'unb=123; cookie2=verified; havana_lgc2_77=long', 'verified-token'))
        result = {'success': True, 'message': 'completed', 'session_id': 'test',
                  'cookies_str': f'unb={456 if other_account else 123}; x5sec=manual'}
        with (patch.object(reply_server, 'db_manager', db),
              patch('app.db_manager.db_manager', db),
              patch.object(reply_server.cookie_manager, 'manager', manager),
              patch.object(risk_control, 'registry', registry),
              patch('utils.manual_captcha.open_manual_session', new=AsyncMock(return_value=result)),
              patch('utils.platform_session.PlatformSession', return_value=session)):
            try:
                await reply_server.start_manual_captcha('a', 300, {'user_id': 7, 'username': 'test'})
                error = None
            except Exception as exc:
                error = exc
        return error, db, session, instance, registry.get('a')

    async def test_completed_page_does_not_release_without_real_im_success(self):
        error, db, session, _, guard = await self.attempt(status='verification_required')
        self.assertEqual(error.status_code, 409)
        session.verify_token.assert_awaited_once()
        db.compare_and_update_cookie.assert_not_called()
        self.assertTrue(guard.is_blocked)

    async def test_verified_result_uses_atomic_save_and_reuses_token(self):
        error, db, session, instance, guard = await self.attempt()
        self.assertIsNone(error)
        session.verify_token.assert_awaited_once()
        db.compare_and_update_cookie.assert_called_once_with('a', 'unb=123; cookie2=old',
            'unb=123; cookie2=verified; havana_lgc2_77=long', 7, clear_verification=True)
        db.save_cookie.assert_not_called()
        self.assertEqual(instance.current_token, 'verified-token')
        self.assertFalse(instance.needs_relogin)
        self.assertFalse(guard.is_blocked)

    async def test_late_or_wrong_account_verification_cannot_resume(self):
        for kwargs in ({'stored':False}, {'other_account':True}):
            error, db, _, _, guard = await self.attempt(**kwargs)
            self.assertEqual(error.status_code, 409)
            self.assertTrue(guard.is_blocked)
            db.save_cookie.assert_not_called()


class VerifiedQRHoldTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_saved_verified_login_resumes_affected_account(self):
        from app import reply_server
        from utils import risk_control
        for saved in (False, True):
            registry = RiskControlRegistry()
            registry.get('a').require_verification()
            registry.get('b').require_verification()
            db = Mock()
            db.get_all_cookies.return_value = {'a': 'unb=123; cookie2=old'}
            db.compare_and_update_cookie.return_value = saved
            with (patch.object(reply_server, 'db_manager', db),
                  patch.object(reply_server.cookie_manager, 'manager', None),
                  patch.object(risk_control, 'registry', registry)):
                try:
                    await reply_server.process_qr_login_cookies('unb=123; cookie2=new', '123',
                        {'user_id': 7, 'username': 'test'}, verified_login=True)
                    error = None
                except RuntimeError as exc:
                    error = exc
            self.assertEqual(error is None, saved)
            self.assertEqual(registry.get('a').is_blocked, not saved)
            self.assertTrue(registry.get('b').is_blocked)
            db.compare_and_update_cookie.assert_called_once_with('a', 'unb=123; cookie2=old',
                'unb=123; cookie2=new', 7, clear_verification=True)

    async def test_status_restores_hold_without_request_or_stale_event(self):
        from app import reply_server
        from utils import risk_control
        db = Mock()
        db.get_all_cookies.return_value = {'a': 'not-read-by-status'}
        registry = RiskControlRegistry(load_verification=lambda _: True)
        with (patch.object(reply_server, 'db_manager', db),
              patch('app.db_manager.db_manager', db),
              patch.object(reply_server.cookie_manager, 'manager', None),
              patch.object(risk_control, 'registry', registry)):
            result = reply_server.get_risk_control_status({'user_id': 7})
        self.assertTrue(result['accounts'][0]['verification_required'])
        self.assertEqual(result['accounts'][0]['remaining_seconds'], 0)
        db.get_risk_control_logs.assert_not_called()
