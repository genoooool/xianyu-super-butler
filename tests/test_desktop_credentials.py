import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from app.routers.desktop_credentials import SavedLogin, create_desktop_credentials_router
from app.services.desktop_credentials import KeychainError


class DesktopCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.keychain = Mock(); self.keychain.read.return_value = None
        db = SimpleNamespace(verify_user_password=lambda username, password: username == 'admin' and password == 'valid')
        def user(authorization: str = Header(default='')):
            if authorization != 'Bearer session': raise HTTPException(401)
            return {'user_id': 1, 'username': 'admin'}
        app = FastAPI()
        with patch('app.routers.desktop_credentials.sys.platform', 'darwin'), patch.dict('os.environ', XIANYU_DESKTOP_SMOKE='0'):
            app.include_router(create_desktop_credentials_router('bootstrap', 'local_session', user, db, store_factory=lambda: self.keychain))
        self.client = TestClient(app)
        self.headers = {'Authorization': 'Bearer session'}

    def tearDown(self): self.client.close()

    def test_read_requires_bootstrap_and_is_not_cacheable(self):
        self.assertEqual(self.client.get('/desktop/credentials').status_code, 403)
        self.keychain.read.assert_not_called()
        self.client.cookies.set('local_session', 'bootstrap')
        self.keychain.read.return_value = {'username': 'admin', 'password': 'valid'}
        response = self.client.get('/desktop/credentials')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertTrue(response.json()['saved'])
        self.assertEqual(self.client.get('/desktop/credentials', headers={'Origin': 'https://evil.invalid'}).status_code, 403)

    def test_save_requires_login_and_correct_password_bound_to_user(self):
        self.client.cookies.set('local_session', 'bootstrap')
        body = dict(username='admin', password='valid')
        self.assertEqual(self.client.post('/desktop/credentials', json=body).status_code, 401)
        for changes in [dict(password='wrong'), dict(username='other')]:
            self.assertEqual(self.client.post('/desktop/credentials', json={**body, **changes}, headers=self.headers).status_code, 400)
        self.keychain.save.assert_not_called()
        self.assertEqual(self.client.post('/desktop/credentials', json=body, headers=self.headers).status_code, 200)
        self.keychain.save.assert_called_once_with('admin', 'valid')
        self.assertNotIn('valid', repr(SavedLogin(**body)))

    def test_forget_failure_is_not_reported_as_success(self):
        self.client.cookies.set('local_session', 'bootstrap')
        self.keychain.forget.side_effect = KeychainError('系统拒绝')
        response = self.client.delete('/desktop/credentials')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.keychain.forget.side_effect = None
        self.assertFalse(self.client.delete('/desktop/credentials').json()['saved'])

    def test_smoke_profile_never_touches_keychain(self):
        factory = Mock()
        app = FastAPI()
        with patch.dict('os.environ', XIANYU_DESKTOP_SMOKE='1'):
            app.include_router(create_desktop_credentials_router('bootstrap', 'local_session', lambda: {}, None, store_factory=factory))
        with TestClient(app) as client:
            client.cookies.set('local_session', 'bootstrap')
            self.assertFalse(client.get('/desktop/credentials').json()['available'])
            self.assertEqual(client.delete('/desktop/credentials').status_code, 409)
        factory.assert_not_called()

    def test_windows_hides_saved_login_and_never_initializes_a_store(self):
        factory = Mock()
        app = FastAPI()
        with patch('app.routers.desktop_credentials.sys', SimpleNamespace(platform='win32')):
            db = SimpleNamespace(verify_user_password=lambda username, password: True)
            app.include_router(create_desktop_credentials_router('bootstrap', 'local_session', lambda: {'username': 'test'}, db, store_factory=factory))
        with TestClient(app) as client:
            client.cookies.set('local_session', 'bootstrap')
            self.assertEqual(client.get('/desktop/credentials').json(), {'available': False, 'saved': False})
            self.assertEqual(client.post('/desktop/credentials', json={'username': 'test', 'password': 'test'}).status_code, 409)
            self.assertEqual(client.delete('/desktop/credentials').status_code, 409)
        factory.assert_not_called()
