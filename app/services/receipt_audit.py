"""Content-free receipt evidence. Logging never changes delivery decisions or retries."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re

from loguru import logger

from app.services.reply_delivery import require_receipt
from app.services.shipping_validation import shipping_response_result

SEND_PATH = '/r/MessageSend/sendByReceiverScope'
_scope = ContextVar('delivery_receipt_scope', default=None)


def identifier(value):
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = str(value)
        if re.fullmatch(r'[A-Za-z0-9_.:@-]{1,128}', value):
            return value
    return None


def status_code(value):
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        if re.fullmatch(r'-?\d{1,9}', str(value)):
            return value
    return None


def request_identifier(value):
    # generate_mid() and platform responses include a space before the numeric suffix.
    if isinstance(value, str) and re.fullmatch(r'\d{1,32} \d{1,6}', value):
        return value
    return identifier(value)


@contextmanager
def receipt_scope(order_id, flow, part):
    token = _scope.set(dict(order_id=identifier(order_id), flow=flow, part=part))
    try:
        yield
    finally:
        _scope.reset(token)


def im_fields(response):
    """Whitelist only protocol fields; never serialize content, credentials or arbitrary errors."""
    response = response if isinstance(response, dict) else {}
    headers = response.get('headers')
    headers = headers if isinstance(headers, dict) else {}
    body = response.get('body')
    fields = dict(response_mid=request_identifier(headers.get('mid')), header_code=status_code(headers.get('code')),
                  top_level_code=status_code(response.get('code')),
                  body_type=type(body).__name__)
    if isinstance(body, dict):
        fields.update(body_code=status_code(body.get('code')), success=body.get('success') if isinstance(body.get('success'), bool) else None,
                      reason_present=bool(body.get('reason')), error_present=bool(body.get('error')))
        inner = body.get('data') if isinstance(body.get('data'), dict) else {}
        fields['message_id'] = identifier(body.get('messageId') or body.get('msgId') or inner.get('messageId') or inner.get('msgId'))
    try:
        require_receipt(response, explicit_success=True)
        fields['assessment'] = 'success_by_current_rule'
    except Exception:
        rejected = any(fields[key] is not None and str(fields[key]) not in {'200', '0'}
                       for key in ('header_code', 'top_level_code'))
        rejected = rejected or any(layer.get('reason') or layer.get('error') or layer.get('success') is False
                                   for layer in (response, headers))
        if isinstance(body, dict):
            rejected = rejected or bool(body.get('reason') or body.get('error')) or body.get('success') is False
            rejected = rejected or (fields['body_code'] is not None and str(fields['body_code']) not in {'200', '0'})
        fields['assessment'] = 'rejected' if rejected else 'unknown'
    return fields


def _emit(document):
    # A broken log sink must never turn an acknowledged send into a retryable failure.
    logger.info('RECEIPT_AUDIT {}', json.dumps(document, ensure_ascii=False, separators=(',', ':')))


def record_im(cookie_id, request_mid, event, response=None, error=None):
    try:
        if event not in {'started', 'response', 'unknown'}:
            return
        document = dict(kind='im_send', event=event, cookie_id=identifier(cookie_id), request_mid=request_identifier(request_mid))
        scope = _scope.get()
        if scope:
            document.update(order_id=scope['order_id'],
                            flow=scope['flow'] if scope['flow'] in {'auto_delivery', 'manual_delivery'} else None,
                            part=scope['part'] if isinstance(scope['part'], int) else None)
        if event == 'response':
            document.update(im_fields(response))
            document['matched'] = document['request_mid'] is not None and document['request_mid'] == document['response_mid']
            if not document['matched']:
                document['assessment'] = 'unknown'
        elif event == 'unknown':
            document.update(assessment='unknown', error_type=type(error).__name__ if error is not None else None)
        _emit(document)
    except Exception:
        pass


def record_shipping(cookie_id, order_id, attempt, *, response=None, http_status=None, error=None):
    try:
        document = dict(kind='platform_shipping', cookie_id=identifier(cookie_id), order_id=identifier(order_id),
                        attempt=attempt, http_status=status_code(http_status), assessment='unknown')
        if error is not None:
            document['error_type'] = type(error).__name__
        elif isinstance(response, dict):
            result = shipping_response_result(response, http_status)
            document['ret_codes'] = result['ret_codes']
            document['assessment'] = result['assessment']
        _emit(document)
    except Exception:
        pass
