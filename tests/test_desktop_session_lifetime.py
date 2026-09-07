"""Exercise desktop session expiry without importing live account/database state."""

import ast
import http.cookiejar
import secrets
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest import mock

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

from app.desktop_updates import UpdateBusy, UpdateGate


class DesktopSessionLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.started = self.now
        self.namespace, self.client = self.make_client('first-launch-secret')
        self.addCleanup(self.client.close)
        self.user = {'user_id': 1, 'username': 'owner', 'timestamp': self.started}
        self.namespace['SESSION_TOKENS']['login-token'] = self.user.copy()

    def make_client(self, launch_secret):
        source = Path(__file__).resolve().parents[1] / 'app' / 'reply_server.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        names = {'verify_token', 'require_auth', 'get_current_user',
                 'require_desktop_access_cookie', 'desktop_bootstrap', 'logout'}
        handlers = [node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in names]
        for handler in handlers:
            handler.decorator_list = []
        namespace = {
            'Optional': Optional, 'Dict': Dict, 'Any': Any,
            'HTTPAuthorizationCredentials': HTTPAuthorizationCredentials,
            'Depends': Depends, 'Query': Query, 'HTTPException': HTTPException,
            'HTMLResponse': HTMLResponse, 'JSONResponse': JSONResponse,
            'security': HTTPBearer(auto_error=False), 'secrets': secrets,
            'time': SimpleNamespace(time=lambda: self.now),
            'SESSION_TOKENS': {}, 'TOKEN_EXPIRE_TIME': 24 * 60 * 60,
            'DESKTOP_ACCESS_TOKEN': launch_secret,
            'DESKTOP_ACCESS_COOKIE': 'xianyu_desktop_access',
            'UpdateBusy': UpdateBusy, 'update_gate': UpdateGate(),
            'desktop_notifications': mock.Mock(),
        }
        exec(compile(ast.Module(body=handlers, type_ignores=[]), str(source), 'exec'), namespace)
        app = FastAPI()
        app.middleware('http')(namespace['require_desktop_access_cookie'])
        app.get('/desktop/bootstrap')(namespace['desktop_bootstrap'])
        app.post('/logout')(namespace['logout'])

        @app.get('/protected')
        def protected(user=Depends(namespace['get_current_user'])):
            return {'user_id': user['user_id']}

        return namespace, TestClient(app, base_url='http://127.0.0.1')

    def bootstrap(self):
        response = self.client.get('/desktop/bootstrap', params={'token': 'first-launch-secret'})
        self.assertEqual(200, response.status_code)
        return response

    def request(self, client=None, token='login-token'):
        with mock.patch.object(http.cookiejar.time, 'time', return_value=self.now):
            return (client or self.client).get('/protected', headers={'Authorization': f'Bearer {token}'})

    def test_desktop_session_survives_more_than_a_day_and_a_month(self):
        self.bootstrap()
        self.assertEqual(200, self.request().status_code)
        for days in (2, 31, 365):
            with self.subTest(days=days):
                self.now = self.started + days * 24 * 60 * 60
                response = self.request()
                self.assertEqual(200, response.status_code)
                self.assertEqual({'user_id': 1}, response.json())

    def test_valid_desktop_cookie_does_not_leave_login_with_a_day_limit(self):
        self.bootstrap()
        self.now += 31 * 24 * 60 * 60
        self.client.cookies.set('xianyu_desktop_access', 'first-launch-secret', domain='127.0.0.1', path='/')
        self.assertEqual(200, self.request().status_code)

    def test_bootstrap_uses_a_session_cookie_with_existing_protections(self):
        response = self.bootstrap()
        cookie, = self.client.cookies.jar
        self.assertIsNone(cookie.expires)
        self.assertTrue(cookie.discard)
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertIn('SameSite=strict', response.headers['set-cookie'])
        self.assertEqual('/', cookie.path)

    def test_missing_wrong_or_unknown_credentials_still_fail(self):
        self.assertEqual(403, self.request().status_code)
        self.client.cookies.set('xianyu_desktop_access', 'wrong-launch-secret', domain='127.0.0.1', path='/')
        self.assertEqual(403, self.request().status_code)
        self.bootstrap()
        self.assertEqual(401, self.request(token='unknown-login').status_code)

    def test_logout_revokes_the_long_running_login(self):
        self.bootstrap()
        self.now += 31 * 24 * 60 * 60
        self.client.post('/logout', headers={'Authorization': 'Bearer login-token'})
        self.assertEqual(401, self.request().status_code)
        self.namespace['desktop_notifications'].configure.assert_called_once_with('login-token', 1, False)

    def test_restart_rejects_both_old_launch_cookie_and_old_login(self):
        self.bootstrap()
        namespace, restarted = self.make_client('new-launch-secret')
        self.addCleanup(restarted.close)
        restarted.cookies.update(self.client.cookies)
        self.assertEqual(403, self.request(restarted).status_code)
        response = restarted.get('/desktop/bootstrap', params={'token': 'new-launch-secret'})
        self.assertEqual(200, response.status_code)
        self.assertEqual(401, self.request(restarted).status_code)
        self.assertEqual({}, namespace['SESSION_TOKENS'])

    def test_web_deployment_keeps_existing_login_expiration(self):
        namespace, web = self.make_client('')
        self.addCleanup(web.close)
        namespace['SESSION_TOKENS']['login-token'] = self.user.copy()
        self.assertEqual(200, self.request(web).status_code)
        self.now += 24 * 60 * 60 + 1
        self.assertEqual(401, self.request(web).status_code)
        self.assertNotIn('login-token', namespace['SESSION_TOKENS'])


if __name__ == '__main__':
    unittest.main()
