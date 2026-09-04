"""Owner-scoped immediate master switch, without saving model or conversation settings."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from app.services.account_reply_control import AccountReplyControl


class SetAccountReply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool
    revision: StrictInt = Field(ge=0)


def create_account_reply_control_router(get_current_user, db):
    router = APIRouter(prefix='/chat/reply-control')
    service = AccountReplyControl(db)

    @router.get('/{cookie_id}')
    def status(cookie_id: str, user=Depends(get_current_user)):
        try:
            return service.state(user['user_id'], cookie_id)
        except PermissionError as error:
            raise HTTPException(404, str(error)) from error

    @router.put('/{cookie_id}')
    def update(cookie_id: str, request: SetAccountReply, user=Depends(get_current_user)):
        try:
            return service.set_enabled(user['user_id'], cookie_id, request.enabled, request.revision)
        except PermissionError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    return router
