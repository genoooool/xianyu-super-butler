"""Normal admin login for UI; separate launch secret for native operations."""
import secrets
import sys
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, ConfigDict
from app.desktop_updates import DATA_COMPATIBILITY, UpdateBusy


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['check', 'install']
    version: str = Field(default='', max_length=64)


class NativeReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=32, max_length=32)
    phase: Literal['checking', 'available', 'current', 'unpublished', 'incompatible', 'downloading', 'installing', 'error']
    version: str = Field(default='', max_length=64)
    latest_version: str = Field(default='', max_length=64)
    notes: str = Field(default='', max_length=12000)
    error: str = Field(default='', max_length=500)
    progress: int = Field(default=0, ge=0, le=100)


class NativeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=32, max_length=32)


def create_desktop_updates_router(broker, gate, desktop_token, verify_token):
    router = APIRouter(prefix='/desktop/updates')
    security = HTTPBearer(auto_error=False)

    def validate(token):
        if not token:
            return False
        user = verify_token(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
        return bool(user and user.get('is_admin'))

    def session(credentials=Depends(security)):
        if not credentials or not validate(credentials.credentials):
            raise HTTPException(403, '请使用工作台管理员账号进行软件更新')
        return credentials.credentials

    def native(request: Request):
        if not desktop_token or not secrets.compare_digest(request.headers.get('X-Xianyu-Desktop-Token', ''), desktop_token):
            raise HTTPException(403, '仅本机启动器可操作')

    @router.get('/status')
    def status(token=Depends(session)):
        return {**broker.status(), 'available': bool(desktop_token and sys.platform == 'darwin')}

    @router.post('/action')
    def action(value: Action, token=Depends(session)):
        if not desktop_token or sys.platform != 'darwin':
            raise HTTPException(400, '应用内升级仅支持 macOS 桌面版，请从 Releases 下载')
        try:
            return broker.request(value.action, token, value.version)
        except UpdateBusy as error:
            raise HTTPException(409, str(error)) from error

    @router.get('/poll', dependencies=[Depends(native)])
    def poll():
        return {'command': broker.poll(validate)}

    @router.post('/report', dependencies=[Depends(native)])
    def report(value: NativeReport):
        try:
            return broker.report(value.id, value.model_dump(exclude={'id'}), validate)
        except UpdateBusy as error:
            raise HTTPException(409, str(error)) from error

    @router.post('/prepare', dependencies=[Depends(native)])
    def prepare(value: NativeRequest):
        if not broker.authorize(value.id, validate):
            raise HTTPException(409, '更新请求或登录已失效')
        try:
            gate.prepare()
        except UpdateBusy as error:
            raise HTTPException(409, str(error)) from error
        return {'ready': True, 'data_compatibility': DATA_COMPATIBILITY}

    @router.post('/release', dependencies=[Depends(native)])
    def release():
        gate.release()
        return {'released': True}

    return router
