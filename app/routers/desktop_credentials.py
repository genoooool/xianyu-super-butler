"""Desktop-bootstrap-only autofill; saving additionally requires verified login."""
import os
import secrets
import sys
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.services.desktop_credentials import KeychainError, MacKeychain


class SavedLogin(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=4096, repr=False)


def create_desktop_credentials_router(token, cookie_name, get_current_user, db, *, store_factory=None):
    router = APIRouter(prefix="/desktop/credentials")
    available = bool(token) and sys.platform == "darwin" and os.getenv("XIANYU_DESKTOP_SMOKE") != "1"

    @lru_cache(maxsize=1)
    def store():
        return (store_factory or MacKeychain)()

    def desktop(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not token or not secrets.compare_digest(request.cookies.get(cookie_name, ""), token):
            raise HTTPException(403, "桌面会话未初始化")
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "不允许跨来源访问登录信息")

    def operate(action):
        if not available:
            raise HTTPException(409, "当前环境不支持系统钥匙串保存")
        try:
            return action(store())
        except KeychainError as error:
            raise HTTPException(503, str(error), headers={"Cache-Control": "no-store"}) from error
        except Exception as error:
            raise HTTPException(503, "系统钥匙串不可用，登录信息未完成保存", headers={"Cache-Control": "no-store"}) from error

    @router.get("")
    def read(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not token:
            return {"available": False, "saved": False}
        desktop(request, response)
        if not available:
            return {"available": False, "saved": False}
        saved = operate(lambda keychain: keychain.read())
        return {"available": True, "saved": bool(saved), **(saved or {})}

    @router.post("", dependencies=[Depends(desktop)])
    def save(value: SavedLogin, user=Depends(get_current_user)):
        # Never persist a failed password or credentials for a different user.
        if value.username != user["username"] or not db.verify_user_password(value.username, value.password):
            raise HTTPException(400, "登录信息验证失败，未保存", headers={"Cache-Control": "no-store"})
        operate(lambda keychain: keychain.save(value.username, value.password))
        return {"saved": True}

    @router.delete("", dependencies=[Depends(desktop)])
    def forget():
        operate(lambda keychain: keychain.forget())
        return {"saved": False}

    return router
