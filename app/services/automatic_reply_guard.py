"""Task-local final send gate. Manual delivery and order shipping have no guard."""
from contextlib import contextmanager
from contextvars import ContextVar

_guard = ContextVar('automatic_reply_send_guard', default=None)


def set_reply_guard(cookie_id, chat_id, check):
    return _guard.set((str(cookie_id), str(chat_id).split('@', 1)[0], check))


def reset_reply_guard(token):
    _guard.reset(token)


@contextmanager
def reply_guard(cookie_id, chat_id, check):
    token = set_reply_guard(cookie_id, chat_id, check)
    try:
        yield
    finally:
        reset_reply_guard(token)


def check_reply_send(cookie_id, chat_id):
    guard = _guard.get()
    if guard is not None:
        account, chat, check = guard
        if (str(cookie_id), str(chat_id).split('@', 1)[0]) != (account, chat) or not check():
            raise RuntimeError('当前会话自动回复已关闭或状态已改变，未提交发送')
