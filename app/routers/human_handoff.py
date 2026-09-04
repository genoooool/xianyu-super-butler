"""Owner-only local takeover controls; no platform traffic or AI enablement."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictInt

from app.services.human_handoff import HumanHandoffs


class ResumeHandoff(BaseModel):
    revision: int = Field(ge=1)


class SetConversationReply(BaseModel):
    enabled: StrictBool
    revision: StrictInt = Field(ge=0)
    buyer_id: str = Field(default='', max_length=128)
    buyer_name: str = Field(default='', max_length=120)
    item_id: str = Field(default='', max_length=128)


def create_human_handoff_router(get_current_user, db, clear_timed_pause):
    router = APIRouter(prefix='/chat/handoffs')
    service = HumanHandoffs(db)

    @router.get('')
    def pending(user=Depends(get_current_user)):
        return {'entries': service.pending(user['user_id'])}

    @router.get('/{cookie_id}/{chat_id}')
    def control(cookie_id: str, chat_id: str, user=Depends(get_current_user)):
        try:
            return service.control(user['user_id'], cookie_id, chat_id)
        except PermissionError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @router.put('/{cookie_id}/{chat_id}')
    def set_control(cookie_id: str, chat_id: str, request: SetConversationReply, user=Depends(get_current_user)):
        try:
            state = service.set_enabled(user['user_id'], cookie_id, chat_id, request.enabled, request.revision,
                                        buyer_id=request.buyer_id, buyer_name=request.buyer_name, item_id=request.item_id)
            if request.enabled:
                clear_timed_pause(chat_id, cookie_id)
            return state
        except PermissionError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post('/{cookie_id}/{chat_id}/resume')
    def resume(cookie_id: str, chat_id: str, request: ResumeHandoff, user=Depends(get_current_user)):
        try:
            service.resume(user['user_id'], cookie_id, chat_id, request.revision)
        except PermissionError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        clear_timed_pause(chat_id, cookie_id)
        return {'success': True, 'message': '已解除人工暂停，只处理此后收到的新消息；原有自动回复开关不变'}

    return router
