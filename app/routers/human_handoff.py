"""Owner-only local takeover controls; no platform traffic or AI enablement."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.human_handoff import HumanHandoffs


class ResumeHandoff(BaseModel):
    revision: int = Field(ge=1)


def create_human_handoff_router(get_current_user, db, clear_timed_pause):
    router = APIRouter(prefix='/chat/handoffs')
    service = HumanHandoffs(db)

    @router.get('')
    def pending(user=Depends(get_current_user)):
        return {'entries': service.pending(user['user_id'])}

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
