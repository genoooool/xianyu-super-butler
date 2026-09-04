"""Opt-in local workbench credentials in the operating-system credential store.

Native APIs run on FastAPI's worker thread, never the UI/event loop. Only one
app-specific generic-password item is touched. There is no plaintext fallback.
"""
import ctypes as C
import json
import plistlib
import sys
import threading

SERVICE = "com.genoooool.xianyuworkbench.login"


class KeychainError(RuntimeError):
    pass


class _FileTime(C.Structure):
    _fields_ = [("low", C.c_uint32), ("high", C.c_uint32)]


class _CredentialAttributeW(C.Structure):
    pass


class _CredentialW(C.Structure):
    _fields_ = [
        ("flags", C.c_uint32),
        ("type", C.c_uint32),
        ("target_name", C.c_wchar_p),
        ("comment", C.c_wchar_p),
        ("last_written", _FileTime),
        ("blob_size", C.c_uint32),
        ("blob", C.POINTER(C.c_ubyte)),
        ("persist", C.c_uint32),
        ("attribute_count", C.c_uint32),
        ("attributes", C.POINTER(_CredentialAttributeW)),
        ("target_alias", C.c_wchar_p),
        ("username", C.c_wchar_p),
    ]


_CredentialPointer = C.POINTER(_CredentialW)


class WindowsCredentialManager:
    """Store one generic credential in the current user's Credential Manager."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168
    MAX_BLOB_BYTES = 2560

    def __init__(self, service=SERVICE, *, advapi=None, last_error=None):
        self.service = service
        self.lock = threading.Lock()
        if advapi is None:
            loader = getattr(C, "WinDLL", None)
            if loader is None:
                raise KeychainError("Windows 凭据管理器不可用")
            advapi = loader("Advapi32.dll", use_last_error=True)
        self.advapi = advapi
        self.last_error = last_error or getattr(C, "get_last_error", lambda: 0)
        self._signature("CredReadW", [C.c_wchar_p, C.c_uint32, C.c_uint32, C.POINTER(_CredentialPointer)], C.c_int)
        self._signature("CredWriteW", [C.POINTER(_CredentialW), C.c_uint32], C.c_int)
        self._signature("CredDeleteW", [C.c_wchar_p, C.c_uint32, C.c_uint32], C.c_int)
        self._signature("CredFree", [C.c_void_p], None)

    def _signature(self, name, args, result):
        function = getattr(self.advapi, name)
        # Python fakes used by platform-independent tests do not expose ctypes
        # function attributes. Native WinDLL functions do.
        try:
            function.argtypes = args
            function.restype = result
        except AttributeError:
            pass

    def _error(self, action, *, allow_missing=False):
        code = int(self.last_error() or 0)
        if allow_missing and code == self.ERROR_NOT_FOUND:
            return False
        raise KeychainError(f"Windows 凭据管理器未完成{action}（系统状态 {code}）")

    def read(self):
        with self.lock:
            pointer = _CredentialPointer()
            if not self.advapi.CredReadW(self.service, self.CRED_TYPE_GENERIC, 0, C.byref(pointer)):
                if self._error("读取", allow_missing=True) is False:
                    return None
            if not pointer:
                raise KeychainError("Windows 凭据管理器返回了无效记录")
            try:
                credential = pointer.contents
                if credential.type != self.CRED_TYPE_GENERIC or credential.blob_size > self.MAX_BLOB_BYTES:
                    raise KeychainError("已保存的登录信息格式异常，请取消保存后重新登录")
                raw = C.string_at(credential.blob, credential.blob_size) if credential.blob_size else b""
                try:
                    password = raw.decode("utf-16-le")
                except UnicodeError as error:
                    raise KeychainError("已保存的登录信息格式异常，请取消保存后重新登录") from error
                username = credential.username or ""
                if not username or not password:
                    raise KeychainError("已保存的登录信息格式异常，请取消保存后重新登录")
                return {"username": username, "password": password}
            finally:
                self.advapi.CredFree(pointer)

    def save(self, username, password):
        blob_bytes = password.encode("utf-16-le")
        if not blob_bytes or len(blob_bytes) > self.MAX_BLOB_BYTES:
            raise KeychainError("密码长度超出 Windows 凭据管理器限制，登录信息未保存")
        blob = (C.c_ubyte * len(blob_bytes)).from_buffer_copy(blob_bytes)
        credential = _CredentialW(
            0,
            self.CRED_TYPE_GENERIC,
            self.service,
            "闲鱼工作台 - 记住登录",
            _FileTime(),
            len(blob_bytes),
            C.cast(blob, C.POINTER(C.c_ubyte)),
            self.CRED_PERSIST_LOCAL_MACHINE,
            0,
            None,
            None,
            username,
        )
        with self.lock:
            if not self.advapi.CredWriteW(C.byref(credential), 0):
                self._error("保存")

    def forget(self):
        with self.lock:
            if not self.advapi.CredDeleteW(self.service, self.CRED_TYPE_GENERIC, 0):
                self._error("清除", allow_missing=True)


def create_system_credential_store(service=SERVICE):
    if sys.platform == "darwin":
        return MacKeychain(service)
    if sys.platform == "win32":
        return WindowsCredentialManager(service)
    raise KeychainError("当前系统不支持安全保存登录信息")


class MacKeychain:
    def __init__(self, service=SERVICE):
        self.service = service
        self.lock = threading.Lock()
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.sec = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        signatures = {
            "CFDataCreate": ([C.c_void_p, C.c_void_p, C.c_long], C.c_void_p),
            "CFDataGetLength": ([C.c_void_p], C.c_long),
            "CFDataGetBytePtr": ([C.c_void_p], C.c_void_p),
            "CFRelease": ([C.c_void_p], None),
            "CFPropertyListCreateWithData": ([C.c_void_p, C.c_void_p, C.c_ulong, C.c_void_p, C.c_void_p], C.c_void_p),
            "CFStringGetCString": ([C.c_void_p, C.c_void_p, C.c_long, C.c_uint32], C.c_bool),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.cf, name); function.argtypes = args; function.restype = result
        for name, args in {"SecItemCopyMatching": [C.c_void_p, C.POINTER(C.c_void_p)],
                           "SecItemAdd": [C.c_void_p, C.c_void_p], "SecItemUpdate": [C.c_void_p, C.c_void_p],
                           "SecItemDelete": [C.c_void_p]}.items():
            function = getattr(self.sec, name); function.argtypes = args; function.restype = C.c_int32

    def constant(self, name):
        pointer = C.c_void_p.in_dll(self.sec, name)
        buffer = C.create_string_buffer(1024)
        if not self.cf.CFStringGetCString(pointer, buffer, len(buffer), 0x08000100):
            raise KeychainError("系统钥匙串常量不可用")
        return buffer.value.decode("utf-8")

    def query(self):
        return {self.constant("kSecClass"): self.constant("kSecClassGenericPassword"),
                self.constant("kSecAttrService"): self.service,
                self.constant("kSecAttrAccount"): "local-workbench",
                self.constant("kSecAttrSynchronizable"): False}

    def dictionary(self, value):
        raw = plistlib.dumps(value)
        data = self.cf.CFDataCreate(None, raw, len(raw))
        try:
            result = self.cf.CFPropertyListCreateWithData(None, data, 0, None, None)
            if not result:
                raise KeychainError("系统钥匙串请求创建失败")
            return result
        finally:
            self.cf.CFRelease(data)

    @staticmethod
    def check(status):
        if status:
            # Status only: never include credentials, native buffers or payloads.
            raise KeychainError(f"钥匙串未完成操作（系统状态 {status}），请允许系统访问后重试")

    def read(self):
        with self.lock:
            query = self.dictionary({**self.query(), self.constant("kSecReturnData"): True,
                                     self.constant("kSecMatchLimit"): self.constant("kSecMatchLimitOne")})
            result = C.c_void_p()
            try:
                status = self.sec.SecItemCopyMatching(query, C.byref(result))
                if status == -25300:
                    return None
                self.check(status)
                length = self.cf.CFDataGetLength(result)
                if length > 32768:
                    raise KeychainError("已保存的登录信息格式异常，请取消保存后重新登录")
                try:
                    value = json.loads(C.string_at(self.cf.CFDataGetBytePtr(result), length))
                    if not isinstance(value, dict) or not all(isinstance(value.get(key), str) for key in ("username", "password")):
                        raise ValueError()
                    return {"username": value["username"], "password": value["password"]}
                except (ValueError, UnicodeError) as error:
                    raise KeychainError("已保存的登录信息格式异常，请取消保存后重新登录") from error
            finally:
                if result.value:
                    self.cf.CFRelease(result)
                self.cf.CFRelease(query)

    def save(self, username, password):
        with self.lock:
            value = {self.constant("kSecValueData"): json.dumps({"username": username, "password": password}).encode("utf-8")}
            query, attrs = self.dictionary(self.query()), self.dictionary(value)
            try:
                status = self.sec.SecItemUpdate(query, attrs)
                if status == -25300:
                    addition = self.dictionary({**self.query(), **value,
                                                self.constant("kSecAttrLabel"): "闲鱼工作台 - 记住登录"})
                    try:
                        status = self.sec.SecItemAdd(addition, None)
                    finally:
                        self.cf.CFRelease(addition)
                self.check(status)
            finally:
                self.cf.CFRelease(query); self.cf.CFRelease(attrs)

    def forget(self):
        with self.lock:
            query = self.dictionary(self.query())
            try:
                status = self.sec.SecItemDelete(query)
                if status != -25300:
                    self.check(status)
            finally:
                self.cf.CFRelease(query)
