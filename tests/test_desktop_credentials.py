import unittest
import ctypes as C
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from app.routers.desktop_credentials import SavedLogin, create_desktop_credentials_router
from app.services.desktop_credentials import (
    KeychainError,
    WindowsCredentialManager,
    _CredentialPointer,
    _CredentialW,
    _FileTime,
)


class FakeCredentialApi:
    def __init__(self):
        self.records = {}
        self.error = WindowsCredentialManager.ERROR_NOT_FOUND
        self.allocations = []

    def CredReadW(self, target, credential_type, _flags, output):
        record = self.records.get(target)
        if record is None:
            self.error = WindowsCredentialManager.ERROR_NOT_FOUND
            return 0
        username, raw = record
        target_buffer = C.create_unicode_buffer(target)
        username_buffer = C.create_unicode_buffer(username)
        blob = (C.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = _CredentialW(
            0, credential_type, C.cast(target_buffer, C.c_wchar_p), None,
            _FileTime(), len(raw), C.cast(blob, C.POINTER(C.c_ubyte)),
            WindowsCredentialManager.CRED_PERSIST_LOCAL_MACHINE,
            0, None, None, C.cast(username_buffer, C.c_wchar_p),
        )
        pointer = C.pointer(credential)
        self.allocations = [target_buffer, username_buffer, blob, credential, pointer]
        C.cast(output, C.POINTER(_CredentialPointer))[0] = pointer
        self.error = 0
        return 1

    def CredWriteW(self, value, _flags):
        credential = C.cast(value, C.POINTER(_CredentialW)).contents
        raw = C.string_at(credential.blob, credential.blob_size)
        self.records[credential.target_name] = (credential.username, raw)
        self.error = 0
        return 1

    def CredDeleteW(self, target, _credential_type, _flags):
        if target not in self.records:
            self.error = WindowsCredentialManager.ERROR_NOT_FOUND
            return 0
        del self.records[target]
        self.error = 0
        return 1

    def CredFree(self, _value):
        self.allocations = []


class WindowsCredentialManagerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeCredentialApi()
        self.store = WindowsCredentialManager(
            "test.xianyu.credentials",
            advapi=self.api,
            last_error=lambda: self.api.error,
        )

    def test_create_read_update_and_forget_use_one_system_record(self):
        self.assertIsNone(self.store.read())
        self.store.save("管理员", "首次密码")
        self.assertEqual(self.store.read(), {"username": "管理员", "password": "首次密码"})
        self.store.save("管理员", "更新后的密码")
        self.assertEqual(self.store.read()["password"], "更新后的密码")
        self.store.forget()
        self.store.forget()
        self.assertIsNone(self.store.read())

    def test_oversize_secret_fails_before_native_write(self):
        with self.assertRaises(KeychainError):
            self.store.save("admin", "密" * 1281)
        self.assertEqual(self.api.records, {})


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

    def test_windows_exposes_saved_login_through_system_store(self):
        factory = Mock()
        keychain = Mock()
        keychain.read.return_value = {'username': 'test', 'password': 'saved'}
        factory.return_value = keychain
        app = FastAPI()
        with patch('app.routers.desktop_credentials.sys', SimpleNamespace(platform='win32')):
            db = SimpleNamespace(verify_user_password=lambda username, password: True)
            app.include_router(create_desktop_credentials_router('bootstrap', 'local_session', lambda: {'username': 'test'}, db, store_factory=factory))
        with TestClient(app) as client:
            client.cookies.set('local_session', 'bootstrap')
            self.assertEqual(client.get('/desktop/credentials').json(), {
                'available': True, 'saved': True, 'username': 'test', 'password': 'saved'
            })
            self.assertEqual(client.post('/desktop/credentials', json={'username': 'test', 'password': 'test'}).status_code, 200)
            self.assertEqual(client.delete('/desktop/credentials').status_code, 200)
        factory.assert_called_once_with()
        keychain.save.assert_called_once_with('test', 'test')
        keychain.forget.assert_called_once_with()
