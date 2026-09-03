"""Authenticated knowledge editing and offline retrieval preview."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.ai_knowledge import KnowledgeService


class KnowledgeEntry(BaseModel):
    scope: Literal["shared", "account", "item"]
    cookie_id: str = Field(default="", max_length=128)
    item_id: str = Field(default="", max_length=128)
    topic: str = Field(min_length=1, max_length=80)
    keywords: str = Field(default="", max_length=300)
    content: str = Field(min_length=1, max_length=2000)
    enabled: bool = True


class KnowledgePreview(BaseModel):
    cookie_id: str = Field(min_length=1, max_length=128)
    item_id: str = Field(default="", max_length=128)
    message: str = Field(min_length=1, max_length=2000)


def create_ai_knowledge_router(get_current_user, db):
    router = APIRouter(prefix="/ai-knowledge")
    service = KnowledgeService(db)

    def run(action):
        try:
            return action()
        except PermissionError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @router.get("")
    def list_entries(user=Depends(get_current_user)):
        return {"entries": service.list_entries(user["user_id"])}

    @router.post("")
    def save_entry(entry: KnowledgeEntry, user=Depends(get_current_user)):
        return {"entry": run(lambda: service.save(user["user_id"], entry.model_dump()))}

    @router.post("/preview")
    def preview(query: KnowledgePreview, user=Depends(get_current_user)):
        entries = run(lambda: service.retrieve(user["user_id"], query.cookie_id, query.item_id, query.message))
        return {"entries": entries, "model_called": False}

    return router
