"""Resume only the takeover observed before a fully confirmed workbench reply."""
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
    # Capture BEFORE any network operation; a later/newer takeover must not be cleared.
    ticket = service.state(owner_id, instance.cookie_id, cid)
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
    result = dict(parts=count, messageId=ids[-1] if ids else '', handoff_auto_resume='not_pending')
    if not ticket or not ticket['pending']:
        return result
    if str(ticket['buyer_id']).split('@', 1)[0] != toid:
        result['handoff_auto_resume'] = 'changed'
        return result
    try:
        service.resume(owner_id, instance.cookie_id, cid, ticket['revision'])
        clear_timed_pause(cid, instance.cookie_id, ids)
    except (ValueError, PermissionError):
        result['handoff_auto_resume'] = 'changed'
    except Exception:
        # Delivery already succeeded. Never report "send failed" and invite a duplicate.
        result['handoff_auto_resume'] = 'failed'
    else:
        result.update(handoff_auto_resume='resumed', handoff_resumed_revision=ticket['revision'])
    return result
