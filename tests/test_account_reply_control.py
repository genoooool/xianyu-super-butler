"""Offline store master switch, real reply caller, settings and platform-card regressions."""
import ast
import asyncio
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from app.routers.account_reply_control import create_account_reply_control_router
from app.routers.human_handoff import create_human_handoff_router
from app.services.account_reply_control import AccountReplyControl
from app.services.platform_events import is_platform_seller_card
from test_human_handoff import Fixture, ActualReplyCallerTests

ROOT = Path(__file__).resolve().parents[1]


class AccountStateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.accounts = AccountReplyControl(self.db, clock=lambda: self.now)

    def test_store_off_on_preserves_manual_and_handoff_rows_other_stores_and_settings(self):
        self.service.set_enabled(1, 'a', 'manual', False, 0)
        self.begin()
        rows = self.db.conn.execute('SELECT * FROM chat_human_handoffs').fetchall()
        self.db.conn.execute("UPDATE ai_reply_settings SET api_key='offline-secret',model_name='keep-me' WHERE cookie_id='a'")
        off = self.accounts.set_enabled(1, 'a', False, 0)
        self.assertEqual(off['revision'], 1)
        self.assertFalse(self.accounts.allows(1, 'a', 1, 1000001))
        self.assertTrue(self.accounts.state(1, 'b')['enabled'])
        on = self.accounts.set_enabled(1, 'a', True, 1)
        self.assertEqual(on['revision'], 2)
        self.assertEqual(self.db.conn.execute('SELECT * FROM chat_human_handoffs').fetchall(), rows)
        self.assertFalse(self.service.control(1, 'a', 'manual')['enabled'])
        self.assertTrue(self.service.control(1, 'a', 'ordinary')['enabled'])
        self.assertFalse(self.accounts.allows(1, 'a', 0, on['changed_ms'] + 1))
        self.assertFalse(self.accounts.allows(1, 'a', 2, on['changed_ms']))
        self.assertTrue(self.accounts.allows(1, 'a', 2, on['changed_ms'] + 1))
        self.assertEqual(self.db.conn.execute("SELECT api_key,model_name FROM ai_reply_settings WHERE cookie_id='a'").fetchone(), ('offline-secret','keep-me'))
        self.assertEqual(self.accounts.set_enabled(1, 'a', True, 2), on)
        with self.assertRaises(ValueError): self.accounts.set_enabled(1, 'a', False, 0)
        with self.assertRaises(PermissionError): self.accounts.set_enabled(2, 'a', False, 2)

    def test_bad_generation_fails_closed_and_toggle_rolls_back_as_one_transaction(self):
        self.db.conn.execute("CREATE TRIGGER reject_control BEFORE INSERT ON user_settings BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(Exception): self.accounts.set_enabled(1, 'a', False, 0)
        self.assertTrue(self.accounts.state(1, 'a')['enabled'])
        self.db.conn.execute('DROP TRIGGER reject_control')
        self.db.conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(1,?,?)", (self.accounts.key('a'), '{}'))
        with self.assertRaises(ValueError): self.accounts.allows(1, 'a', 0, 1000001)

    def test_real_model_save_omitted_flag_preserves_master_and_legacy_flag_invalidates_generation(self):
        tree = ast.parse((ROOT / 'app/db_manager.py').read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'save_ai_reply_settings')
        env = {'logger': Mock()}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'db_manager.py', 'exec'), env)
        save = MethodType(env['save_ai_reply_settings'], self.db)
        self.accounts.set_enabled(1, 'a', False, 0)
        self.assertTrue(save('a', {'model_name': 'saved', 'ai_enabled': None}))
        self.assertEqual(self.accounts.state(1, 'a')['revision'], 1)
        self.assertFalse(self.accounts.state(1, 'a')['enabled'])
        self.assertTrue(save('a', {'ai_enabled': True}))
        self.assertEqual(self.accounts.state(1, 'a')['revision'], 2)
        self.assertTrue(self.accounts.state(1, 'a')['enabled'])
        self.assertTrue(save('a', {'model_name': 'second'}))
        self.assertTrue(self.accounts.state(1, 'a')['enabled'])
        self.assertEqual(self.accounts.state(1, 'a')['revision'], 2)


class AccountRouteTests(Fixture):
    def setUp(self):
        super().setUp()
        def user(authorization: str = Header(default='')):
            if authorization not in ('one', 'two'): raise HTTPException(401)
            return {'user_id': 1 if authorization == 'one' else 2}
        self.clear = Mock()
        app = FastAPI()
        app.include_router(create_account_reply_control_router(user, self.db))
        app.include_router(create_human_handoff_router(user, self.db, self.clear))
        self.client = TestClient(app)
        self.auth = {'Authorization': 'one'}

    def test_effective_chat_state_and_legacy_resume_cannot_bypass_store_off(self):
        self.service.set_enabled(1, 'a', 'manual', False, 0)
        ticket = self.begin()
        path = '/chat/reply-control/a'
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers={'Authorization':'two'}).status_code, 404)
        for payload in ({'enabled': 'false', 'revision': 0}, {'enabled': False, 'revision': True}):
            self.assertEqual(self.client.put(path, json=payload, headers=self.auth).status_code, 422)
        self.assertEqual(self.client.put(path, json={'enabled': False, 'revision': 0}, headers=self.auth).status_code, 200)
        ordinary = self.client.get('/chat/handoffs/a/ordinary', headers=self.auth).json()
        self.assertFalse(ordinary['enabled']); self.assertFalse(ordinary['account_enabled'])
        self.assertTrue(ordinary['conversation_enabled']); self.assertEqual(ordinary['account_revision'], 1)
        self.assertTrue(self.client.get('/chat/handoffs/b/ordinary', headers=self.auth).json()['enabled'])
        self.assertEqual(self.client.put('/chat/handoffs/a/manual', json={'enabled': True, 'revision': 1}, headers=self.auth).status_code, 409)
        self.assertEqual(self.client.post('/chat/handoffs/a/chat/resume', json={'revision':ticket['revision']}, headers=self.auth).status_code, 409)
        self.clear.assert_not_called()
        self.assertEqual(self.client.put(path, json={'enabled': True, 'revision': 0}, headers=self.auth).status_code, 409)
        self.assertEqual(self.client.put(path, json={'enabled': True, 'revision': 1}, headers=self.auth).status_code, 200)
        self.assertTrue(self.client.get('/chat/handoffs/a/ordinary', headers=self.auth).json()['enabled'])
        self.assertFalse(self.client.get('/chat/handoffs/a/manual', headers=self.auth).json()['enabled'])
        self.assertFalse(self.client.get('/chat/handoffs/a/chat', headers=self.auth).json()['enabled'])


class AccountCallerTests(ActualReplyCallerTests):
    def test_store_off_stops_literal_qa_keywords_api_default_and_model_before_selection(self):
        self.qa()
        self.env['AUTO_REPLY']['api']['enabled'] = True
        self.instance.get_api_reply = AsyncMock(return_value='API fallback')
        self.set_ai(False)
        self.run_caller('多少钱')
        self.instance.get_api_reply.assert_not_called(); self.instance.get_keyword_reply.assert_not_called()
        self.instance.get_ai_reply.assert_not_called(); self.instance.get_default_reply.assert_not_called()
        self.instance.send_im_text.assert_not_called(); self.instance.send_msg.assert_not_called()
        self.model.assert_not_called()

    def test_store_off_on_while_model_running_cannot_send_or_reclose_chat(self):
        rule = self.qa()
        for answer in ('{"status":"match","id":%s}' % rule['id'], '{"status":"unclear","id":null}'):
            def classify(*_, answer=answer):
                self.set_ai(False); self.set_ai(True)
                return answer
            self.model.side_effect = classify
            state = AccountReplyControl(self.db).state(1, 'a')
            self.run_caller(stamp=state['changed_ms'] + 1)
        self.instance.send_im_text.assert_not_called()
        self.assertTrue(self.service.control(1, 'a', 'chat')['enabled'])
        self.assertEqual(self.service.pending(1), [])

    def test_old_queued_message_not_replayed_and_new_qa_works_after_reenable(self):
        self.qa(); self.set_ai(False); self.set_ai(True)
        state = AccountReplyControl(self.db).state(1, 'a')
        self.run_caller('多少钱', stamp=state['changed_ms'])
        self.instance.send_im_text.assert_not_called()
        self.run_caller('多少钱', stamp=state['changed_ms'] + 1)
        self.instance.send_im_text.assert_awaited_once_with('chat', 'buyer', '10元10个，按照此回答。')

    def test_store_off_while_external_reply_is_running_prevents_its_send(self):
        self.env['AUTO_REPLY']['api']['enabled'] = True
        async def api_reply(*_):
            self.set_ai(False)
            return 'This late API reply must not be sent'
        self.instance.get_api_reply = AsyncMock(side_effect=api_reply)
        self.run_caller()
        self.instance.get_api_reply.assert_awaited_once()
        self.instance.send_msg.assert_not_called()
        self.instance.send_im_text.assert_not_called()
        self.assertEqual(self.service.pending(1), [])


class PlatformCardTests(unittest.TestCase):
    @staticmethod
    def card(task='求送小红花-卖家', source='RED_FLOWER:offline', kind='26'):
        return {'1': {'10': {'extJson': json.dumps({'contentType':kind}),
                             'bizTag':json.dumps({'taskName':task, 'sourceId':source})}}}

    def test_only_typed_known_platform_tasks_exempt_human_takeover(self):
        self.assertTrue(is_platform_seller_card(self.card()))
        self.assertTrue(is_platform_seller_card(self.card('期待评价_卖家', 'C2C:offline')))
        for message in (None, {}, self.card(kind='1'), self.card(kind='2'), self.card(task='ordinary'),
                        self.card(source='other'), {'1': {'10': {'reminderContent':'快给ta一个评价吧～'}}},
                        {'1': {'10': {'extJson':'invalid','bizTag':'invalid'}}}):
            self.assertFalse(is_platform_seller_card(message))

    def test_actual_self_direction_branch_ignores_system_cards_but_observes_manual_text_and_images(self):
        tree = ast.parse((ROOT / 'XianyuAutoAsync.py').read_text())
        branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                      and ast.unparse(n.test) == 'send_user_id == self.myid')
        wrapper = ast.parse('async def route(self, message):\n pass').body[0]
        wrapper.body = branch.body
        ast.fix_missing_locations(wrapper)
        pause = SimpleNamespace(observe_self_message=AsyncMock())
        env = dict(logger=Mock(), pause_manager=pause, send_message='快给ta一个评价吧～',
                   msg_time='offline', item_id='item', chat_id='chat', create_time=1)
        exec(compile(ast.Module(body=[wrapper], type_ignores=[]), 'self-branch', 'exec'), env)
        instance = SimpleNamespace(cookie_id='a', _is_system_or_order_event=lambda _:False,
                                   _extract_message_id=lambda _: 'offline-id')
        for message in (self.card(), self.card('期待评价_卖家', 'C2C:offline')):
            asyncio.run(env['route'](instance, message))
        pause.observe_self_message.assert_not_called()
        for message in (self.card(kind='1'), self.card(kind='2')):
            asyncio.run(env['route'](instance, message))
        self.assertEqual(pause.observe_self_message.await_count, 2)


if __name__ == '__main__': unittest.main()
