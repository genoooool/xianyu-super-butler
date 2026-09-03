"""Desktop-only routes: normal login for UI; launch secret for native polling."""

import secrets
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPBearer
from pydantic import BaseModel


class NotificationPreference(BaseModel):
    enabled: bool


def create_desktop_notifications_router(hub, desktop_token, verify_token):
    router = APIRouter(prefix="/desktop/notifications")
    security = HTTPBearer(auto_error=False)

    def session(credentials=Depends(security)):
        user = verify_token(credentials)
        if not user:
            raise HTTPException(401, "请先登录工作台")
        return credentials.credentials, user["user_id"]

    def require_desktop():
        if not desktop_token:
            raise HTTPException(404, "仅桌面客户端可用")

    @router.get("/status")
    def status(auth=Depends(session)):
        return hub.status(auth[0]) if desktop_token else {"available": False, "active": False}

    @router.post("/session")
    def configure(preference: NotificationPreference, auth=Depends(session)):
        require_desktop()
        return hub.configure(*auth, preference.enabled)

    @router.post("/test")
    def test(auth=Depends(session)):
        require_desktop()
        if not hub.test(auth[0]):
            raise HTTPException(409, "请先开启提醒，或稍候再测试")
        return {"queued": True}

    @router.get("/poll")
    def poll(request: Request, after: int = Query(0, ge=0)):
        require_desktop()
        supplied = request.headers.get("X-Xianyu-Desktop-Token", "")
        if not secrets.compare_digest(supplied, desktop_token):
            raise HTTPException(403, "仅桌面启动器可读取提醒")

        def validate(token, owner):
            from fastapi.security import HTTPAuthorizationCredentials
            user = verify_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
            return bool(user and user["user_id"] == owner)

        return hub.poll(after, validate)

    return router
