"""人工接入暂停不得跨账号串号。

回归的是这个故障：AutoReplyPauseManager 是全局单例，而 paused_chats 只用
chat_id 做键。chat_id 标识的是一个会话，当用户自己的两个账号正好是同一个会话
的两端时（测试自动回复最常见的做法），双方拿到的是同一个 chat_id。于是 A 账号
手动发一条消息，就把 B 账号对该会话的自动回复一起停掉，表现为「关键词明明配好
了却完全不回」，而唯一线索是一行 info 级日志。

实测：酷灵(2217097925130) 在后台手动发言 → 耶比(2214101806408) 收到「测试」，
命中精确关键词，却被判 chat_paused 跳过。
"""

import ast
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from app.services.human_handoff import HumanHandoffs, initialize_schema

ACC_A = '2217097925130'
ACC_B = '2214101806408'
SHARED_CHAT = '65456990156'


def make_manager(pause_minutes=10):
    """Execute the real manager against an isolated in-memory database."""
    source = Path(__file__).resolve().parents[1] / 'XianyuAutoAsync.py'
    node = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'AutoReplyPauseManager')
    env = dict(time=time, logger=Mock())
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), env)
    manager = env['AutoReplyPauseManager']()
    db = SimpleNamespace(conn=sqlite3.connect(':memory:'), lock=threading.RLock(),
                         get_cookie_pause_duration=Mock(return_value=pause_minutes))
    db.conn.executescript('CREATE TABLE users(id INTEGER PRIMARY KEY); INSERT INTO users VALUES(1); CREATE TABLE cookies(id TEXT PRIMARY KEY,user_id INTEGER);')
    db.conn.executemany('INSERT INTO cookies VALUES(?,1)', [(ACC_A,), (ACC_B,)])
    initialize_schema(db.conn.cursor()); db.conn.commit()
    manager.test_service = HumanHandoffs(db)
    patcher = patch.dict('sys.modules', {'app.db_manager': SimpleNamespace(db_manager=db)})
    patcher.start()
    def stop():
        patcher.stop(); db.conn.close()
    return manager, SimpleNamespace(stop=stop)


class CrossAccountIsolationTests(unittest.TestCase):
    def setUp(self):
        self.manager, self._patcher = make_manager()

    def tearDown(self):
        self._patcher.stop()

    def test_pause_does_not_leak_to_other_account(self):
        """A 手动发言，不得暂停 B 在同一 chat_id 上的自动回复。"""
        self.manager.pause_chat(SHARED_CHAT, ACC_A)

        self.assertTrue(self.manager.is_chat_paused(SHARED_CHAT, ACC_A))
        self.assertFalse(
            self.manager.is_chat_paused(SHARED_CHAT, ACC_B),
            "另一个账号的自动回复被误暂停了"
        )

    def test_manual_pause_has_no_countdown(self):
        self.manager.pause_chat(SHARED_CHAT, ACC_A)

        self.assertEqual(self.manager.get_remaining_pause_time(SHARED_CHAT, ACC_A), 0)
        self.assertEqual(self.manager.get_remaining_pause_time(SHARED_CHAT, ACC_B), 0)

    def test_both_accounts_can_pause_independently(self):
        self.manager.pause_chat(SHARED_CHAT, ACC_A)
        self.manager.pause_chat(SHARED_CHAT, ACC_B)

        self.assertTrue(self.manager.is_chat_paused(SHARED_CHAT, ACC_A))
        self.assertTrue(self.manager.is_chat_paused(SHARED_CHAT, ACC_B))
        self.assertEqual(self.manager.test_service.db.conn.execute('SELECT COUNT(*) FROM chat_human_handoffs WHERE pending=1').fetchone()[0], 2)

    def test_different_chats_same_account_isolated(self):
        self.manager.pause_chat(SHARED_CHAT, ACC_A)

        self.assertFalse(self.manager.is_chat_paused('99999999', ACC_A))

    def test_explicit_resume_clears_only_target_account_conversation(self):
        self.manager.pause_chat(SHARED_CHAT, ACC_A)
        self.manager.pause_chat(SHARED_CHAT, ACC_B)
        self.manager.pause_chat('another', ACC_A)
        state = self.manager.test_service.control(1, ACC_A, SHARED_CHAT)
        self.manager.test_service.set_enabled(1, ACC_A, SHARED_CHAT, True, state['revision'])
        self.manager.resume_chat(SHARED_CHAT + '@goofish', ACC_A)
        self.assertFalse(self.manager.is_chat_paused(SHARED_CHAT, ACC_A))
        self.assertTrue(self.manager.is_chat_paused(SHARED_CHAT, ACC_B))
        self.assertTrue(self.manager.is_chat_paused('another', ACC_A))


class PausePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.manager, self._patcher = make_manager()

    def tearDown(self):
        self._patcher.stop()

    def test_elapsed_time_does_not_release_pause(self):
        self.manager.pause_chat(SHARED_CHAT, ACC_A)
        with patch('time.time', return_value=time.time() + 86400):
            self.assertTrue(self.manager.is_chat_paused(SHARED_CHAT, ACC_A))

    def test_cleanup_never_reopens_manual_conversations(self):
        self.manager.pause_chat(SHARED_CHAT, ACC_A)
        self.manager.pause_chat(SHARED_CHAT, ACC_B)
        self.manager.cleanup_expired_pauses()
        self.assertTrue(self.manager.is_chat_paused(SHARED_CHAT, ACC_A))
        self.assertTrue(self.manager.is_chat_paused(SHARED_CHAT, ACC_B))


class LegacyDurationTests(unittest.TestCase):
    def test_old_zero_minute_setting_cannot_bypass_new_manual_control(self):
        """旧账号时长值保留在库中，但不再影响明确的人工接入规则。"""
        manager, patcher = make_manager(pause_minutes=0)
        try:
            manager.pause_chat(SHARED_CHAT, ACC_A)
            self.assertTrue(manager.is_chat_paused(SHARED_CHAT, ACC_A))
            self.assertEqual(manager.paused_chats, {})
        finally:
            patcher.stop()


if __name__ == "__main__":
    unittest.main()
