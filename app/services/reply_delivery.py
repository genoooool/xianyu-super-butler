"""Typed text/image delivery; a generated text marker is never executable."""
import base64
import json
import uuid

from app.services.reply_assets import ReplyAssets


class ReplyDeliveryError(RuntimeError):
    def __init__(self, sent_count, cause):
        self.sent_count = sent_count
        super().__init__(f"已确认发送{sent_count}部分，其余未发送或结果未确认；请先检查聊天记录，不要直接重复发送。({type(cause).__name__})")


def require_receipt(response, *, explicit_success=False):
    if not isinstance(response, dict):
        raise RuntimeError("未收到发送回执")
    headers = response.get('headers', {})
    if not isinstance(headers, dict):
        raise RuntimeError("发送回执无效")
    body = response.get('body')
    if not isinstance(body, dict):
        raise RuntimeError("发送回执无效")
    # Real IM responses put code at the TOP level; older adapters put it in headers.
    # Recognize both, but never let one positive field hide a rejection elsewhere.
    for layer in (response, headers, body):
        if layer.get('reason') or layer.get('error') or layer.get('success') is False:
            raise RuntimeError("平台拒绝发送")
        if layer.get('code') is not None and str(layer['code']) not in {'200', '0'}:
            raise RuntimeError("平台拒绝发送")
    if explicit_success and not any(str(layer.get('code')) in {'200', '0'} for layer in (response, headers)):
        raise RuntimeError("未收到明确成功的发送回执")


async def send_parts(instance, db, owner_id, cid, toid, text, images, check=lambda: True,
                     *, explicit_receipt=False, on_receipt=None):
    assets = ReplyAssets(db)
    assets.validate(owner_id, images)
    if not isinstance(text, str) or len(text) > 2000 or (not text and not images) or '__IMAGE_SEND__' in text:
        raise ValueError("无效的固定回复")
    sent = 0

    def guard():
        if not check():
            raise RuntimeError("回复已暂停、规则已更新或会话已改变")

    try:
        guard()
        # Upload every attachment before sending text, so a failed upload cannot silently lose pictures.
        uploaded = []
        if images:
            from utils.image_uploader import ImageUploader
            async with ImageUploader(instance.cookies_str) as uploader:
                for asset_id in images:
                    guard()
                    asset = assets.get(owner_id, asset_id)
                    url = await uploader.upload_reply_bytes(asset['data'])
                    guard()
                    uploaded.append(dict(url=url, width=asset['width'], height=asset['height'], type=0))
        if text:
            guard()
            response = await instance.send_im_text(cid, toid, text)
            require_receipt(response, explicit_success=explicit_receipt)
            sent += 1
            if on_receipt:
                on_receipt(response)
        if uploaded:
            guard()
            content = base64.b64encode(json.dumps(dict(contentType=2, image=dict(pics=uploaded)), ensure_ascii=False).encode()).decode()
            response = await instance._send_im_request('/r/MessageSend/sendByReceiverScope', [
                dict(uuid=uuid.uuid4().hex, cid=cid if '@goofish' in cid else cid+'@goofish', conversationType=1,
                     content=dict(contentType=101, custom=dict(type=1, data=content)), redPointPolicy=0,
                     extension=dict(extJson='{}'), ctx=dict(appVersion='1.0', platform='web'), mtags={}, msgReadStatusSetting=1),
                dict(actualReceivers=[toid if '@goofish' in toid else toid+'@goofish', str(instance.myid)+'@goofish'])])
            require_receipt(response, explicit_success=explicit_receipt)
            sent += 1
            if on_receipt:
                on_receipt(response)
        return sent
    except Exception as error:
        raise ReplyDeliveryError(sent, error) from error
