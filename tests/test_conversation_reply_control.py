"""Offline conversation switch contract: scope, persistence, CAS, and last-mile guards."""
import asyncio
import sqlite3
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_human_handoff as handoff_tests
from test_delivery_receipts import Live
from app.services.human_handoff import HumanHandoffs
from app.services.automatic_reply_guard import check_reply_send, reply_guard
from app.services.outgoing_echoes import OutgoingEchoes


class ControlStateTests(handoff_tests.Fixture):
    def test_new_chat_defaults_open_without_inserting_or_changing_accounts(self):
        before = self.db.conn.execute('SELECT * FROM cookies').fetchall()
        self.assertEqual(self.service.control(1, 'a', 'chat@goofish'),
                         dict(cookie_id='a', chat_id='chat', enabled=True, revision=0, reason=''))
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM chat_human_handoffs').fetchone()[0], 0)
        self.assertEqual(self.db.conn.execute('SELECT * FROM cookies').fetchall(), before)

    def test_manual_off_is_store_chat_and_owner_scoped_not_a_fake_ai_handoff(self):
        off = self.service.set_enabled(1, 'a', 'chat@goofish', False, 0, buyer_id='buyer')
        self.assertFalse(off['enabled'])
        self.assertEqual(self.service.pending(1), [])
        self.assertTrue(self.service.control(1, 'b', 'chat')['enabled'])
        self.assertTrue(self.service.control(1, 'a', 'another')['enabled'])
        with self.assertRaises(PermissionError): self.service.control(2, 'a', 'chat')
        with self.assertRaises(PermissionError): self.service.set_enabled(2, 'a', 'chat', True, 1)

    def test_manual_off_survives_time_and_real_database_reopen(self):
        self.service.set_enabled(1, 'a', 'chat', False, 0)
        self.now += 86400
        self.assertFalse(self.service.control(1, 'a', 'chat')['enabled'])
        path = Path(tempfile.mkdtemp(prefix='conversation-control-test-')) / 'restart.sqlite3'
        target = sqlite3.connect(path); self.db.conn.backup(target); target.close()
        restarted = SimpleNamespace(conn=sqlite3.connect(path), lock=threading.RLock())
        try:
            self.assertFalse(HumanHandoffs(restarted).control(1, 'a', 'chat')['enabled'])
        finally: restarted.conn.close()

    def test_explicit_on_clears_ai_handoff_and_invalidates_old_generation_and_messages(self):
        ticket = self.begin()
        state = self.service.set_enabled(1, 'a', 'chat', True, ticket['revision'])
        self.assertTrue(state['enabled'])
        self.assertEqual(self.service.pending(1), [])
        self.assertFalse(self.service.can_reply(1, 'a', 'chat', 0, 1000001))
        self.assertFalse(self.service.can_reply(1, 'a', 'chat', state['revision'], 1000000))
        self.assertTrue(self.service.can_reply(1, 'a', 'chat', state['revision'], 1000001))

    def test_stale_or_duplicate_toggle_never_overwrites_new_decision(self):
        self.service.set_enabled(1, 'a', 'chat', False, 0)
        with self.assertRaises(ValueError): self.service.set_enabled(1, 'a', 'chat', True, 0)
        self.service.set_enabled(1, 'a', 'chat', True, 1)
        with self.assertRaises(ValueError): self.service.set_enabled(1, 'a', 'chat', False, 1)
        self.assertTrue(self.service.control(1, 'a', 'chat')['enabled'])
        self.assertFalse(self.service.can_reply(1, 'a', 'chat', 0, 1000001))

    def test_manual_send_supersedes_unsent_ai_ticket_and_remains_off(self):
        ticket = self.begin()
        state = self.service.pause_manual(1, 'a', 'chat', buyer_id='buyer')
        self.assertFalse(state['enabled'])
        self.assertFalse(self.service.current(ticket))
        self.assertEqual(self.service.pending(1), [])
        self.assertEqual(self.service.pause_manual(1, 'a', 'chat'), state)


class ControlRouteTests(handoff_tests.HandoffRouteTests):
    def test_get_put_auth_strict_types_cas_and_no_platform_calls(self):
        path = '/chat/handoffs/a/chat'
        auth = {'Authorization': 'Bearer one'}
        foreign = {'Authorization': 'Bearer two'}
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers=foreign).status_code, 404)
        self.assertEqual(self.client.put(path, json={'enabled': False, 'revision': 0}, headers=foreign).status_code, 404)
        self.assertEqual(self.client.get(path, headers=auth).json()['enabled'], True)
        for data in ({'enabled': 'false', 'revision': 0}, {'enabled': False, 'revision': True}, {'enabled': False, 'revision': -1}):
            self.assertEqual(self.client.put(path, json=data, headers=auth).status_code, 422)
        off = self.client.put(path, json={'enabled': False, 'revision': 0}, headers=auth)
        self.assertEqual(off.status_code, 200)
        self.clear.assert_not_called()
        self.assertEqual(self.client.put(path, json={'enabled': True, 'revision': 0}, headers=auth).status_code, 409)
        self.assertEqual(self.client.put(path, json={'enabled': True, 'revision': 1}, headers=auth).json()['enabled'], True)
        self.clear.assert_called_once_with('chat', 'a')
        self.instance.send_im_text.assert_not_called()


class ActualSwitchCallerTests(handoff_tests.ActualReplyCallerTests):
    def test_off_blocks_qa_keyword_and_ai_before_selection_without_other_chat_effect(self):
        self.qa()
        self.service.set_enabled(1, 'a', 'chat', False, 0)
        self.run_caller('多少钱')
        self.model.assert_not_called(); self.instance.get_keyword_reply.assert_not_called()
        self.instance.send_im_text.assert_not_called()
        self.run_caller('多少钱', chat_id='another')
        self.instance.send_im_text.assert_awaited_once_with('another', 'buyer', '10元10个，按照此回答。')

    def test_switch_off_during_classifier_discards_saved_answer(self):
        rule = self.qa()
        def classify(*_):
            self.service.set_enabled(1, 'a', 'chat', False, 0)
            return '{"status":"match","id":%s}' % rule['id']
        self.model.side_effect = classify
        self.run_caller()
        self.instance.send_im_text.assert_not_called()
        self.assertFalse(self.service.control(1, 'a', 'chat')['enabled'])

    def test_switch_off_on_during_model_cannot_send_or_reclose_chat(self):
        self.qa()
        def classify(*_):
            self.service.set_enabled(1, 'a', 'chat', False, 0)
            self.service.set_enabled(1, 'a', 'chat', True, 1)
            return '{"status":"unclear","id":null}'
        self.model.side_effect = classify
        self.run_caller()
        self.instance.send_im_text.assert_not_called()
        self.assertTrue(self.service.control(1, 'a', 'chat')['enabled'])


class FinalSendGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_rechecked_after_waiting_for_transport_lock(self):
        live = Live(); live.cookie_id = 'a'; live._im_pending = {}; live._im_request_lock = asyncio.Lock()
        live.ws = SimpleNamespace(closed=False, send=AsyncMock())
        allowed = True
        await live._im_request_lock.acquire()
        with reply_guard('a', 'chat', lambda: allowed):
            pending = asyncio.create_task(live._send_im_request('/r/MessageSend/sendByReceiverScope', [{'cid': 'chat@goofish'}]))
        await asyncio.sleep(0)
        allowed = False
        live._im_request_lock.release()
        with self.assertRaisesRegex(RuntimeError, '未提交发送'): await pending
        live.ws.send.assert_not_called()
        self.assertEqual(live._im_pending, {})

    async def test_guard_is_task_and_destination_scoped_and_resets(self):
        async def blocked():
            with reply_guard('a', 'chat', lambda: False):
                await asyncio.sleep(0)
                with self.assertRaises(RuntimeError): check_reply_send('a', 'chat')
        async def shipping():
            await asyncio.sleep(0)
            check_reply_send('a', 'chat')  # No automatic-chat guard in independent shipping task.
        await asyncio.gather(blocked(), shipping())
        with reply_guard('a', 'chat', lambda: True):
            with self.assertRaises(RuntimeError): check_reply_send('b', 'chat')
            with self.assertRaises(RuntimeError): check_reply_send('a', 'other')
            check_reply_send('a', 'chat@goofish')
        check_reply_send('a', 'chat')


class ExactEchoTests(unittest.TestCase):
    def test_only_correlated_successful_message_id_suppresses_same_store_chat_echo(self):
        echoes = OutgoingEchoes()
        echoes.register('a', 'request', 'chat@goofish')
        echoes.resolve('b', {'headers': {'mid': 'request'}, 'code': 200, 'body': {'messageId': 'id'}})
        self.assertFalse(echoes.contains('a', 'chat', 'id'))
        echoes.resolve('a', {'headers': {'mid': 'request'}, 'code': 200, 'body': {'messageId': 'id'}})
        self.assertTrue(echoes.contains('a', 'chat', 'id'))
        self.assertFalse(echoes.contains('b', 'chat', 'id'))
        self.assertFalse(echoes.contains('a', 'other', 'id'))
        self.assertFalse(echoes.contains('a', 'chat', 'request'))

    def test_unknown_or_rejected_response_not_treated_as_own_send_and_cache_bounded(self):
        now = [1000]
        echoes = OutgoingEchoes(clock=lambda: now[0])
        for code in (403, None):
            echoes.register('a', str(code), 'chat')
            echoes.resolve('a', {'headers': {'mid': str(code)}, 'code': code, 'body': {'messageId': str(code)}})
            self.assertFalse(echoes.contains('a', 'chat', str(code)))
        for i in range(2050): echoes.register('a', i, 'chat')
        self.assertLessEqual(len(echoes.requests), 2048)
        now[0] += 121
        self.assertFalse(echoes.contains('a', 'chat', '0'))
        self.assertEqual(len(echoes.requests), 0)


class EarlySelfPushTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_manager_preserves_on_for_early_own_push_and_turns_off_for_phone(self):
        from test_auto_reply_pause_isolation import make_manager, ACC_A, ACC_B, SHARED_CHAT
        manager, cleanup = make_manager()
        echoes = OutgoingEchoes()
        try:
            with patch('app.services.outgoing_echoes.outgoing_echoes', echoes):
                echoes.register(ACC_A, 'request', SHARED_CHAT)
                observation = asyncio.create_task(manager.observe_self_message(SHARED_CHAT, ACC_A, message_id='own'))
                await asyncio.sleep(0)
                self.assertTrue(manager.is_chat_paused(SHARED_CHAT, ACC_A))
                self.assertFalse(manager.is_chat_paused(SHARED_CHAT, ACC_B))
                echoes.resolve(ACC_A, {'headers': {'mid': 'request'}, 'code': 200, 'body': {'messageId': 'own'}})
                await observation
                self.assertTrue(manager.test_service.control(1, ACC_A, SHARED_CHAT)['enabled'])
                await manager.observe_self_message(SHARED_CHAT, ACC_A, message_id='phone')
                self.assertFalse(manager.test_service.control(1, ACC_A, SHARED_CHAT)['enabled'])
                self.assertTrue(manager.test_service.control(1, ACC_B, SHARED_CHAT)['enabled'])
        finally:
            cleanup.stop()

    async def test_push_before_receipt_waits_for_exact_id_without_cross_chat_hold(self):
        echoes = OutgoingEchoes()
        echoes.register('a', 'request', 'chat')
        observation = asyncio.create_task(echoes.match_self_push('a', 'chat', 'server-id'))
        await asyncio.sleep(0)
        self.assertTrue(echoes.unresolved('a', 'chat'))
        self.assertFalse(echoes.unresolved('b', 'chat'))
        self.assertFalse(echoes.unresolved('a', 'another'))
        waiting_buyer = asyncio.create_task(echoes.wait_settled('a', 'chat'))
        await asyncio.sleep(0)
        self.assertFalse(waiting_buyer.done())
        echoes.resolve('a', {'headers': {'mid': 'request'}, 'code': 200, 'body': {'messageId': 'server-id'}})
        # The hold is released before the sending coroutine continues to its next image.
        self.assertFalse(echoes.unresolved('a', 'chat'))
        self.assertTrue(await observation)
        await waiting_buyer

    async def test_unmatched_phone_reply_rejected_and_missing_receipt_never_suppressed(self):
        for code, mid in ((200, 'different-id'), (403, 'phone-id')):
            echoes = OutgoingEchoes(); echoes.register('a', 'request', 'chat')
            observation = asyncio.create_task(echoes.match_self_push('a', 'chat', 'phone-id'))
            await asyncio.sleep(0)
            echoes.resolve('a', {'headers': {'mid': 'request'}, 'code': code, 'body': {'messageId': mid}})
            self.assertFalse(await observation)
            self.assertFalse(echoes.unresolved('a', 'chat'))
        echoes = OutgoingEchoes(); echoes.register('a', 'request', 'chat')
        self.assertFalse(await echoes.match_self_push('a', 'chat', 'unknown', timeout=.01))
        self.assertEqual(echoes.observations, {})


if __name__ == '__main__': unittest.main()
