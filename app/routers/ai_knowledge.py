"""Authenticated knowledge editing and offline retrieval preview."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.services.ai_knowledge import KnowledgeService
from app.services.knowledge_documents import MAX_CONTENT_CHARS, MAX_UPLOAD_BYTES, preview_document


class KnowledgeEntry(BaseModel):
    scope: Literal["shared", "account", "item"]
    cookie_id: str = Field(default="", max_length=128)
    item_id: str = Field(default="", max_length=128)
    topic: str = Field(min_length=1, max_length=80)
    keywords: str = Field(default="", max_length=300)
    content: str = Field(default="", max_length=MAX_CONTENT_CHARS)
    enabled: bool = True
    entry_type: Literal["knowledge", "qa"] = "knowledge"
    match_mode: Literal["exact", "contains", "hybrid"] = "hybrid"
    image_ids: list[str] = Field(default_factory=list, max_length=4)


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
        except FileExistsError as error:
            raise HTTPException(409, str(error)) from error
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

    @router.post("/documents/preview")
    async def document_preview(request: Request, filename: str = Query(min_length=1, max_length=180),
                               user=Depends(get_current_user)):
        # Bound the stream before buffering; no multipart spool or disk writes.
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "文件不能超过256KB")
            raw.extend(chunk)
        return run(lambda: preview_document(filename, bytes(raw)))

    @router.post("/documents/import")
    def document_import(entry: KnowledgeEntry, user=Depends(get_current_user)):
        if entry.entry_type != "knowledge":
            raise HTTPException(400, "文档导入只能建立知识资料")
        return {"entry": run(lambda: service.save(user["user_id"], entry.model_dump(), create_only=True))}

    @router.post("/images")
    async def upload_reply_image(request: Request, user=Depends(get_current_user)):
        from app.services.reply_assets import ReplyAssets, MAX_IMAGE_BYTES
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_IMAGE_BYTES:
                raise HTTPException(413, "图片不能超过5MB")
            raw.extend(chunk)
        return run(lambda: ReplyAssets(db).save(user["user_id"], bytes(raw)))

    @router.get("/images/{asset_id}")
    def read_reply_image(asset_id: str, user=Depends(get_current_user)):
        from app.services.reply_assets import ReplyAssets
        asset = run(lambda: ReplyAssets(db).get(user["user_id"], asset_id))
        return Response(asset["data"], media_type=asset["mime"], headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    return router
