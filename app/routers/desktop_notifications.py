"""Desktop-only routes: normal login for UI; launch secret for native polling."""

import json
import secrets
import sys
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field


class NotificationPreference(BaseModel):
    enabled: bool
    sound: bool = False
    save: bool = False


class NotificationClick(BaseModel):
    target: str = Field(default='', max_length=64)


def create_desktop_notifications_router(hub, desktop_token, verify_token, settings_store=None):
    router = APIRouter(prefix="/desktop/notifications")
    security = HTTPBearer(auto_error=False)
    preference_key = "desktop_notification_preferences"

    def saved_preference(user_id):
        if not settings_store:
            return None
        saved = settings_store.get_user_setting(user_id, preference_key)
        try:
            value = json.loads(saved["value"]) if saved else None
            if isinstance(value, dict) and all(type(value.get(key)) is bool for key in ("enabled", "sound")):
                return {key: value[key] for key in ("enabled", "sound")}
        except (ValueError, TypeError, KeyError):
            pass
        return None

    def session(credentials=Depends(security)):
        user = verify_token(credentials)
        if not user:
            raise HTTPException(401, "请先登录工作台")
        return credentials.credentials, user["user_id"]

    def require_desktop():
        if not desktop_token:
            raise HTTPException(404, "仅桌面客户端可用")

    def require_native(request):
        require_desktop()
        supplied = request.headers.get("X-Xianyu-Desktop-Token", "")
        if not secrets.compare_digest(supplied, desktop_token):
            raise HTTPException(403, "仅桌面启动器可读取提醒")

    def validate(token, owner):
        from fastapi.security import HTTPAuthorizationCredentials
        user = verify_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
        return bool(user and user["user_id"] == owner)

    @router.post('/activate')
    def activate(click: NotificationClick, request: Request):
        require_native(request)
        return {'queued': hub.activate(click.target, validate)}

    @router.post('/activation')
    def activation(auth=Depends(session)):
        require_desktop()
        def owns_account(cookie_id):
            return bool(settings_store and cookie_id in settings_store.get_all_cookies(auth[1]))
        return {'navigation': hub.take_activation(*auth, owns_account)}

    @router.get("/status")
    def status(auth=Depends(session)):
        result = hub.status(auth[0]) if desktop_token else {"available": False, "active": False}
        return {**result, "sound_available": bool(desktop_token and sys.platform == "darwin"),
                "preference": saved_preference(auth[1]) if desktop_token else None}

    @router.post("/session")
    def configure(preference: NotificationPreference, auth=Depends(session)):
        require_desktop()
        # Persist only an explicit toggle, not startup registration/heartbeats.
        # Reuse the existing per-user settings table; no schema migration.
        if preference.save and settings_store:
            value = json.dumps({"enabled": preference.enabled, "sound": preference.sound})
            if not settings_store.set_user_setting(auth[1], preference_key, value, "本机消息提醒偏好"):
                raise HTTPException(500, "提醒设置未保存")
        return hub.configure(*auth, preference.enabled, preference.sound)

    @router.post("/test")
    def test(auth=Depends(session)):
        require_desktop()
        if not hub.test(auth[0]):
            raise HTTPException(409, "请先开启提醒，或稍候再测试")
        return {"queued": True}

    @router.get("/poll")
    def poll(request: Request, after: int = Query(0, ge=0)):
        require_native(request)
        return hub.poll(after, validate)

    return router
