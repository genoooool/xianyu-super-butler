"""Offline regression: durable human takeover, real caller, auth and send races."""
import ast
import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from app.services.human_handoff import (
    HANDOFF_REPLY, HandoffReply, HumanHandoffs, initialize_schema, message_timestamp, request_handoff,
)
from app.routers.human_handoff import create_human_handoff_router


class Fixture(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(conn=sqlite3.connect(':memory:', check_same_thread=False), lock=threading.RLock())
        self.db.conn.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY);
            INSERT INTO users VALUES(1),(2);
            CREATE TABLE cookies(id TEXT PRIMARY KEY,user_id INTEGER);
            INSERT INTO cookies VALUES('a',1),('b',1),('other',2);''')
        initialize_schema(self.db.conn.cursor()); self.db.conn.commit()
        self.now = 1000
        self.service = HumanHandoffs(self.db, clock=lambda: self.now)
        self.instance = SimpleNamespace(cookie_id='a', send_im_text=AsyncMock(return_value={'headers': {'code': 200}, 'body': {}}))

    def tearDown(self):
        self.db.conn.close()

    def begin(self, cookie='a', chat='chat', revision=0, message_ms=1):
        return self.service.begin(1, cookie, chat, revision, message_ms, 'unclear', 'buyer', '买家', 'item')

    async def transfer(self, **changes):
        data = dict(owner_id=1, chat_id='chat', buyer_id='buyer', buyer_name='买家', item_id='item',
                    reason='unclear', revision=0, message_ms=1, check=lambda: True)
        data.update(changes)
        return await request_handoff(self.instance, self.db, **data)


class HandoffStateTests(Fixture):
    def test_durable_once_and_scope_isolation(self):
        ticket = self.begin(chat='chat@goofish')
        self.assertEqual(ticket['chat_id'], 'chat')
        self.assertFalse(self.service.can_reply(1, 'a', 'chat', 1, 2))
        self.assertTrue(self.service.can_reply(1, 'a', 'other', 0, 2))
        self.assertTrue(self.service.can_reply(1, 'b', 'chat', 0, 2))
        self.assertIsNone(self.begin())
        path = Path(tempfile.mkdtemp(prefix='xianyu-handoff-db-test-')) / 'restart.sqlite3'
        persisted = sqlite3.connect(path)
        self.db.conn.backup(persisted); persisted.close()
        restarted = SimpleNamespace(conn=sqlite3.connect(path), lock=threading.RLock())
        try:
            service = HumanHandoffs(restarted)
            self.assertFalse(service.can_reply(1, 'a', 'chat', 1, int(time.time()*1000)))
            self.assertEqual(len(service.pending(1)), 1)
        finally:
            restarted.conn.close()

    def test_resume_versions_reject_stale_generation_old_messages_and_duplicates(self):
        ticket = self.begin()
        self.service.resume(1, 'a', 'chat', ticket['revision'])
        self.assertFalse(self.service.current(ticket))
        self.assertEqual(self.service.pending(1), [])
        self.assertFalse(self.service.can_reply(1, 'a', 'chat', 0, 1000001))
        self.assertFalse(self.service.can_reply(1, 'a', 'chat', 2, 0))
        self.assertFalse(self.service.can_reply(1, 'a', 'chat', 2, 1000000))
        self.assertTrue(self.service.can_reply(1, 'a', 'chat', 2, 1000001))
        with self.assertRaises(ValueError): self.service.resume(1, 'a', 'chat', 1)
        self.assertIsNone(self.begin(revision=0, message_ms=1000001))
        fresh = self.begin(revision=2, message_ms=1000001)
        self.assertEqual(fresh['revision'], 3)
        with self.assertRaises(ValueError): self.service.resume(1, 'a', 'chat', 1)

    def test_owner_transfer_and_foreign_owner_cannot_read_or_resume(self):
        ticket = self.begin()
        self.assertEqual(self.service.pending(2), [])
        with self.assertRaises(PermissionError): self.service.state(2, 'a', 'chat')
        with self.assertRaises(PermissionError): self.service.resume(2, 'a', 'chat', ticket['revision'])
        self.db.conn.execute("UPDATE cookies SET user_id=2 WHERE id='a'"); self.db.conn.commit()
        self.assertEqual(self.service.pending(1), [])
        self.assertEqual(self.service.pending(2), [])
        with self.assertRaises(PermissionError): self.service.current(ticket)

    def test_concurrent_claims_have_one_winner(self):
        winners = []
        def claim():
            result = self.begin()
            if result: winners.append(result)
        threads = [threading.Thread(target=claim) for _ in range(10)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(winners), 1)

    def test_invalid_timestamp_is_never_treated_as_new_after_resume(self):
        for value in [None, 'invalid', float('nan'), float('inf'), -1]:
            self.assertEqual(message_timestamp({'1': {'5': value}}), 0)
        self.assertEqual(message_timestamp({'1': {'5': '1000'}}), 1000)


class HandoffDeliveryTests(Fixture):
    def test_exact_text_once_and_pause_precedes_send(self):
        async def send(chat, buyer, text):
            self.assertTrue(self.service.state(1, 'a', 'chat')['pending'])
            self.assertEqual(text, HANDOFF_REPLY)
            return {'headers': {'code': 200}, 'body': {}}
        self.instance.send_im_text.side_effect = send
        with patch('app.desktop_notifications.desktop_notifications.publish_handoff') as notify:
            ticket = asyncio.run(self.transfer())
            again = asyncio.run(self.transfer())
        self.assertEqual(ticket['send_status'], 'confirmed')
        self.assertIsNone(again)
        self.instance.send_im_text.assert_awaited_once_with('chat', 'buyer', HANDOFF_REPLY)
        notify.assert_called_once()

    def test_failure_or_cancel_is_still_paused_and_never_retried(self):
        for error in [TimeoutError(), asyncio.CancelledError()]:
            with self.subTest(error=type(error).__name__):
                chat = type(error).__name__
                self.instance.send_im_text.side_effect = error
                try: asyncio.run(self.transfer(chat_id=chat))
                except asyncio.CancelledError: pass
                self.assertTrue(self.service.state(1, 'a', chat)['pending'])
                self.assertEqual(self.service.state(1, 'a', chat)['send_status'], 'unknown')
                self.assertIsNone(asyncio.run(self.transfer(chat_id=chat)))

    def test_changed_filter_prevents_send_but_preserves_pause(self):
        ticket = asyncio.run(self.transfer(check=Mock(side_effect=[True, False])))
        self.assertEqual(ticket['send_status'], 'withheld')
        self.instance.send_im_text.assert_not_called()

    def test_resume_during_send_does_not_overwrite_resumed_state(self):
        async def send(*args):
            self.service.resume(1, 'a', 'chat', 1)
            return {'body': {}}
        self.instance.send_im_text.side_effect = send
        asyncio.run(self.transfer())
        self.assertEqual(self.service.state(1, 'a', 'chat')['pending'], 0)
        self.assertEqual(self.service.state(1, 'a', 'chat')['revision'], 2)

    def test_notification_failure_does_not_block_fixed_reply_or_release_pause(self):
        with patch('app.desktop_notifications.desktop_notifications.publish_handoff', side_effect=RuntimeError):
            ticket = asyncio.run(self.transfer())
        self.assertEqual(ticket['send_status'], 'confirmed')
        self.assertTrue(ticket['pending'])

    def test_filtered_notification_keeps_local_handoff_and_fixed_reply(self):
        with patch('app.desktop_notifications.desktop_notifications.publish_handoff') as notify:
            ticket = asyncio.run(self.transfer(notify=False))
        self.assertEqual(ticket['send_status'], 'confirmed')
        self.assertTrue(ticket['pending'])
        notify.assert_not_called()


class HandoffRouteTests(Fixture):
    def setUp(self):
        super().setUp()
        def user(authorization: str = Header(default='')):
            if authorization not in {'Bearer one', 'Bearer two'}: raise HTTPException(401)
            return {'user_id': 1 if authorization == 'Bearer one' else 2}
        self.clear = Mock()
        app = FastAPI(); app.include_router(create_human_handoff_router(user, self.db, self.clear))
        self.client = TestClient(app)

    def test_auth_scope_compare_and_resume_without_platform_or_model(self):
        self.begin(); self.begin(cookie='b')
        path = '/chat/handoffs/a/chat/resume'
        self.assertEqual(self.client.get('/chat/handoffs').status_code, 401)
        self.assertEqual(self.client.post(path, json={'revision': 1}).status_code, 401)
        self.assertEqual(self.client.get('/chat/handoffs', headers={'Authorization': 'Bearer two'}).json()['entries'], [])
        self.assertEqual(self.client.post(path, json={'revision': 1}, headers={'Authorization': 'Bearer two'}).status_code, 404)
        auth = {'Authorization': 'Bearer one'}
        self.assertEqual(self.client.post(path, json={'revision': 2}, headers=auth).status_code, 409)
        self.clear.assert_not_called()
        self.assertEqual(self.client.post(path, json={'revision': 1}, headers=auth).status_code, 200)
        self.clear.assert_called_once_with('chat', 'a')
        self.assertEqual([h['cookie_id'] for h in self.service.pending(1)], ['b'])


class ActualReplyCallerTests(Fixture):
    def setUp(self):
        super().setUp()
        from app.services.ai_knowledge import initialize_schema as knowledge_schema, KnowledgeService
        self.db.conn.executescript("CREATE TABLE item_info(cookie_id TEXT,item_id TEXT); INSERT INTO item_info VALUES('a','item'),('b','item');")
        knowledge_schema(self.db.conn.cursor()); self.db.conn.commit()
        self.knowledge = KnowledgeService(self.db)
        self.db.matches_message_filter = Mock(return_value=False)
        self.db.get_ai_reply_settings = Mock(return_value={'ai_enabled': True})
        self.paused = False
        self.pause = SimpleNamespace(is_chat_paused=lambda *_: self.paused, get_remaining_pause_time=lambda *_: 10)
        source = Path(__file__).resolve().parents[1] / 'XianyuAutoAsync.py'
        method = next(n for n in ast.walk(ast.parse(source.read_text())) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_process_chat_message_reply')
        self.env = dict(asyncio=asyncio, time=time, logger=Mock(), AUTO_REPLY={'enabled': True, 'api': {'enabled': False}}, pause_manager=self.pause)
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), self.env)
        self.instance._add_reply_decision_log = Mock(return_value=1)
        self.instance._update_reply_decision_log = Mock()
        self.instance._safe_str = str
        self.instance.get_keyword_reply = AsyncMock(return_value=None)
        self.instance.get_ai_reply = AsyncMock(return_value=HandoffReply('no_knowledge'))
        self.instance.get_default_reply = AsyncMock(return_value=None)
        self.instance.send_msg = AsyncMock()
        self.instance.send_image_msg = AsyncMock()
        self.model = Mock(return_value='{"status":"unclear","id":null}')

    def qa(self):
        return self.knowledge.save(1, dict(scope='account', cookie_id='a', topic='询价', keywords='多少钱',
            content='10元10个，按照此回答。', entry_type='qa'))

    def run_caller(self, message='怎么理解', item_id='item', chat_id='chat', stamp=None):
        modules = {'app.db_manager': SimpleNamespace(db_manager=self.db),
                   'app.ai_reply_engine': SimpleNamespace(ai_reply_engine=SimpleNamespace(_generate_with_retry=self.model))}
        with patch.dict(sys.modules, modules):
            asyncio.run(self.env['_process_chat_message_reply'](self.instance, {'1': {'5': stamp or int(time.time()*1000)}},
                None, '买家', 'buyer', message, item_id, chat_id, 'now'))

    def test_ambiguous_qa_transfer_is_terminal_and_future_messages_are_silent(self):
        self.qa(); self.run_caller(); self.run_caller('多少钱')
        self.instance.send_im_text.assert_awaited_once_with('chat', 'buyer', HANDOFF_REPLY)
        self.instance.get_ai_reply.assert_not_called()
        self.instance.get_default_reply.assert_not_called()
        self.assertEqual(self.model.call_count, 1)

    def test_unknown_product_is_not_a_handoff_even_when_ai_enabled(self):
        self.qa()
        for enabled in (True, False):
            self.db.get_ai_reply_settings.return_value = {'ai_enabled': enabled}
            self.run_caller('你好', item_id='missing', chat_id=str(enabled))
        self.instance.send_im_text.assert_not_called()
        self.instance.get_default_reply.assert_not_called()
        self.instance.get_keyword_reply.assert_not_called()
        self.instance.get_ai_reply.assert_not_called()
        self.assertEqual(self.service.pending(1), [])
        self.model.assert_not_called()

    def test_other_store_product_and_greeting_do_not_trigger_handoff(self):
        self.db.conn.execute("INSERT INTO item_info VALUES('b','b-only')")
        self.knowledge.save(1, dict(scope='item', cookie_id='b', item_id='b-only',
            topic='问候', keywords='你好', content='B店原文', entry_type='qa'))
        self.db.get_ai_reply_settings.return_value = {'ai_enabled': False}
        self.run_caller('你好', item_id='b-only')
        self.instance.send_im_text.assert_not_called(); self.model.assert_not_called()
        self.assertEqual(self.service.pending(1), [])
        self.assertEqual(self.instance._add_reply_decision_log.call_args.kwargs['decision_reason'], 'qa_target_unavailable')

    def test_rule_load_failure_never_sends_handoff_or_default(self):
        with patch('app.services.fixed_replies.FixedReplies.choose', side_effect=RuntimeError('offline failure')):
            self.run_caller('你好')
        self.assertEqual(self.service.pending(1), [])
        self.instance.send_im_text.assert_not_called(); self.instance.get_default_reply.assert_not_called()
        self.assertEqual(self.instance._add_reply_decision_log.call_args.kwargs['decision_reason'], 'qa_rules_unavailable')

    def test_ai_off_conflicting_literal_qa_does_not_handoff(self):
        self.qa()
        self.knowledge.save(1, dict(scope='account', cookie_id='a', topic='第二报价', keywords='多少钱',
            content='第二原文', entry_type='qa'))
        self.db.get_ai_reply_settings.return_value = {'ai_enabled': False}
        self.run_caller('多少钱')
        self.instance.send_im_text.assert_not_called(); self.model.assert_not_called()
        self.assertEqual(self.service.pending(1), [])

    def test_ai_off_literal_greeting_still_sends_saved_keyword_answer(self):
        self.knowledge.save(1, dict(scope='item', cookie_id='a', item_id='item', topic='问候',
            keywords='你好', content='固定问候原文', entry_type='qa'))
        self.db.get_ai_reply_settings.return_value = {'ai_enabled': False}
        self.run_caller('你好')
        self.instance.send_im_text.assert_awaited_once_with('chat', 'buyer', '固定问候原文')
        self.model.assert_not_called(); self.assertEqual(self.service.pending(1), [])

    def test_ai_switched_off_during_classification_does_not_handoff(self):
        self.qa()
        def classify(*_):
            self.db.get_ai_reply_settings.return_value = {'ai_enabled': False}
            return '{"status":"unclear","id":null}'
        self.model.side_effect = classify
        self.run_caller()
        self.instance.send_im_text.assert_not_called(); self.assertEqual(self.service.pending(1), [])

    def test_known_qa_still_sends_original_and_no_handoff(self):
        self.qa(); self.run_caller('多少钱')
        self.instance.send_im_text.assert_awaited_once_with('chat', 'buyer', '10元10个，按照此回答。')
        self.assertEqual(self.service.pending(1), [])

    def test_no_knowledge_transfers_but_intentional_skip_does_not(self):
        self.run_caller()
        self.assertTrue(self.service.pending(1))
        self.instance.get_ai_reply.return_value = None
        self.run_caller(chat_id='skip')
        self.assertIsNone(self.service.state(1, 'a', 'skip'))
        self.instance.get_default_reply.assert_not_called()

    def test_manual_pause_global_off_and_ai_disabled_do_not_create_handoffs(self):
        self.paused = True; self.run_caller()
        self.paused = False; self.env['AUTO_REPLY']['enabled'] = False; self.run_caller()
        self.env['AUTO_REPLY']['enabled'] = True
        self.db.get_ai_reply_settings.return_value = {'ai_enabled': False}
        self.instance.get_ai_reply.return_value = None; self.run_caller()
        self.assertEqual(self.service.pending(1), [])
        self.instance.send_im_text.assert_not_called()

    def test_late_model_after_handoff_and_resume_cannot_send_or_repause(self):
        self.qa()
        def classify(*args):
            ticket = self.begin()
            self.service.resume(1, 'a', 'chat', ticket['revision'])
            return '{"status":"unclear","id":null}'
        self.model.side_effect = classify
        self.run_caller()
        self.instance.send_im_text.assert_not_called()
        self.assertEqual(self.service.pending(1), [])

    def test_old_message_after_resume_cannot_trigger_qa_or_model(self):
        self.qa(); ticket = self.begin(); self.service.resume(1, 'a', 'chat', ticket['revision'])
        self.run_caller('多少钱', stamp=999999)
        self.instance.send_im_text.assert_not_called(); self.model.assert_not_called()

    def test_new_message_after_manual_resume_can_use_original_qa(self):
        self.qa(); ticket = self.begin(); self.service.resume(1, 'a', 'chat', ticket['revision'])
        self.run_caller('多少钱', stamp=1000001)
        self.instance.send_im_text.assert_awaited_once_with('chat', 'buyer', '10元10个，按照此回答。')
        self.assertEqual(self.service.pending(1), [])

    def test_late_matched_qa_after_handoff_and_resume_is_discarded(self):
        rule = self.qa()
        def classify(*args):
            ticket = self.begin()
            self.service.resume(1, 'a', 'chat', ticket['revision'])
            return '{"status":"match","id":%s}' % rule['id']
        self.model.side_effect = classify
        self.run_caller()
        self.instance.send_im_text.assert_not_called()
        self.assertEqual(self.service.pending(1), [])


if __name__ == '__main__':
    unittest.main()
