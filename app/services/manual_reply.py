"""Manual workbench replies leave the conversation off until an explicit switch-on."""
from app.services.human_handoff import HumanHandoffs, chat_key
from app.services.reply_delivery import require_receipt, send_parts


def receipt_message_id(receipt):
    body = receipt.get('body')
    if not isinstance(body, dict):
        return ''
    for values in (body, body.get('data')):
        if isinstance(values, dict):
            value = values.get('messageId') or values.get('msgId')
            if isinstance(value, (str, int)):
                return str(value)
    return ''


async def send_manual_reply(instance, db, owner_id, cid, toid, text, images, clear_timed_pause):
    cid = chat_key(cid)
    toid = str(toid).strip().split('@', 1)[0]
    service = HumanHandoffs(db)
    # Persist BEFORE any upload/send: an in-flight AI answer cannot interrupt manual work.
    # Storage failure stops the send; after delivery never write back an old state snapshot.
    service.pause_manual(owner_id, instance.cookie_id, cid, buyer_id=toid)
    receipts = []
    if images:
        count = await send_parts(instance, db, owner_id, cid, toid, text, images,
                                 explicit_receipt=True, on_receipt=receipts.append)
    else:
        response = await instance.send_im_text(cid, toid, text)
        require_receipt(response, explicit_success=True)
        receipts.append(response)
        count = 1

    ids = [value for receipt in receipts if (value := receipt_message_id(receipt))]
    return dict(parts=count, messageId=ids[-1] if ids else '', handoff_auto_resume='disabled')
