"""Opt-in local workbench credentials in macOS Keychain; no plaintext fallback.

Modern SecItem APIs run on FastAPI's worker thread, never the UI/event loop.
Only one app-specific generic-password item is touched. No shell arguments.
"""
import ctypes as C
import json
import plistlib
import threading

SERVICE = "com.genoooool.xianyuworkbench.login"


class KeychainError(RuntimeError):
    pass


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
