"""Replay synthetic platform envelopes through the real receive/debounce/QA path."""
import ast
import asyncio
import base64
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_human_handoff as handoff_tests


class MessageIdentityPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        handoff_tests.Fixture.setUp(self)
        from app.services.ai_knowledge import initialize_schema, KnowledgeService
        self.db.conn.executescript("CREATE TABLE item_info(cookie_id TEXT,item_id TEXT); INSERT INTO item_info VALUES('a','item');")
        initialize_schema(self.db.conn.cursor())
        self.db.conn.commit()
        self.knowledge = KnowledgeService(self.db)
        self.db.matches_message_filter = Mock(return_value=False)
        self.db.get_ai_reply_settings = Mock(return_value={'ai_enabled': True})
        self.env = dict(asyncio=asyncio, time=time, logger=Mock(), AUTO_REPLY={'enabled': True},
                        pause_manager=SimpleNamespace(is_chat_paused=lambda *_: False))
        self.instance._add_reply_decision_log = Mock(return_value=1)
        self.instance._update_reply_decision_log = Mock()
        self.instance._safe_str = str
        self.model = Mock(side_effect=AssertionError('Exact QA must not call a model'))
        self.knowledge.save(1, dict(scope='account', cookie_id='a', topic='问候',
            keywords='你好', content='固定问候', entry_type='qa', match_mode='exact'))
        source = Path(__file__).resolve().parents[1] / 'XianyuAutoAsync.py'
        cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'XianyuLive')
        names = {'handle_message', '_extract_message_id', '_schedule_debounced_reply', '_process_chat_message_reply'}
        methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        self.assertEqual(len(methods), len(names))
        node = ast.ClassDef(name='Pipeline', bases=[], keywords=[], body=methods, decorator_list=[])
        self.env.update(json=json, base64=base64, generate_mid=lambda: 'offline 0')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(source), 'exec'), self.env)
        live = self.env['Pipeline']()
        live.__dict__.update(vars(self.instance))
        self.instance = live
        live.user_id = 1
        live.myid = 'seller'
        live.order_status_handler = None
        live.is_sync_package = lambda _: True
        live.is_chat_message = lambda _: True
        live._extract_order_id = lambda _: None
        live._is_auto_delivery_trigger = lambda _: False
        live._is_system_or_order_event = lambda _: False
        live.send_notification = AsyncMock()
        live.message_debounce_tasks = {}
        live.message_debounce_lock = asyncio.Lock()
        live.message_debounce_delay = 0
        live.processed_message_ids = {}
        live.processed_message_ids_lock = asyncio.Lock()
        live.processed_message_ids_max_size = 10000
        live.message_expire_time = 3600
        self.tasks = []
        def track(coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task
        live._create_tracked_task = track
        self.ws = SimpleNamespace(send=AsyncMock())
        self.modules = patch.dict(sys.modules, {
            'app.db_manager': SimpleNamespace(db_manager=self.db),
            'app.cookie_manager': SimpleNamespace(manager=None),
            'app.ai_reply_engine': SimpleNamespace(ai_reply_engine=SimpleNamespace(_generate_with_retry=self.model)),
            'app.desktop_notifications': SimpleNamespace(desktop_notifications=SimpleNamespace(publish=Mock())),
        })
        self.modules.start()

    async def asyncTearDown(self):
        await asyncio.gather(*self.tasks)
        self.modules.stop()
        handoff_tests.Fixture.tearDown(self)

    async def push(self, mid='platform-1.PNM', stamp=None, chat='chat', text='你好', biz=None, sender='buyer'):
        detail = dict(senderUserId=sender, reminderContent=text, reminderUrl='https://example.test/?itemId=item')
        if biz is not None:
            detail['bizTag'] = json.dumps({'messageId': biz})
        parsed = {'1': {'2': chat+'@goofish', '5': stamp or int(time.time()*1000), '10': detail}}
        if mid is not None:
            parsed['1']['3'] = mid
        envelope = {'headers': {'mid': 'transport-'+str(len(self.tasks))}, 'body': {
            'syncPushPackage': {'data': [{'data': base64.b64encode(json.dumps(parsed).encode()).decode()}]}}}
        await self.instance.handle_message(envelope, self.ws)
        await asyncio.gather(*self.tasks)
        return parsed

    async def test_self_order_card_is_not_manual_takeover_but_phone_text_is(self):
        observer = AsyncMock()
        self.env['pause_manager'].observe_self_message = observer
        self.instance._is_system_or_order_event = lambda text: text == '[你已发货]'
        await self.push(text='[你已发货]', sender='seller')
        observer.assert_not_called()
        await self.push(mid='phone-2', text='我来处理', sender='seller')
        observer.assert_awaited_once()
        self.instance.send_im_text.assert_not_called()

    async def test_same_text_new_platform_messages_each_reach_qa(self):
        for i in range(3):
            await self.push(mid=f'message-{i}.PNM', stamp=1000+i)
        self.assertEqual(self.instance.send_im_text.await_count, 3)
        self.model.assert_not_called()

    async def test_two_different_images_with_same_placeholder_are_not_duplicates(self):
        self.knowledge.save(1, dict(scope='account', cookie_id='a', topic='图片确认',
            keywords='[图片]', content='已收到图片', entry_type='qa', match_mode='exact'))
        await self.push(mid='image-one.PNM', stamp=1000, text='[图片]')
        await self.push(mid='image-two.PNM', stamp=1000, text='[图片]')
        self.assertEqual(self.instance.send_im_text.await_count, 2)

    async def test_only_same_platform_id_is_deduplicated(self):
        await self.push(stamp=1000)
        await self.push(stamp=1000)
        await self.push(mid='platform-2.PNM', stamp=1000)
        self.assertEqual(self.instance.send_im_text.await_count, 2)

    async def test_platform_id_wins_over_reused_client_tag(self):
        await self.push(mid='one.PNM', biz='reused-client-tag')
        await self.push(mid='two.PNM', biz='reused-client-tag')
        self.assertEqual(self.instance.send_im_text.await_count, 2)

    async def test_missing_id_never_uses_text_as_identity(self):
        await self.push(mid=None, stamp=1000)
        await self.push(mid=None, stamp=1000)
        self.assertEqual(self.instance.send_im_text.await_count, 2)

    async def test_same_id_in_other_conversation_is_not_suppressed(self):
        await self.push(chat='chat')
        await self.push(chat='different-chat')
        self.assertEqual(self.instance.send_im_text.await_count, 2)

    async def test_pending_resume_then_same_text_new_message_replies(self):
        self.service.begin(1, 'a', 'chat', 0, 1, 'unclear', 'buyer', '买家', 'item')
        await self.push(mid='before.PNM', stamp=1000)
        self.instance.send_im_text.assert_not_called()
        self.service.resume(1, 'a', 'chat', 1)
        stamp = self.service.state(1, 'a', 'chat')['resumed_ms']
        await self.push(mid='after.PNM', stamp=stamp+1)
        self.instance.send_im_text.assert_awaited_once_with('chat', 'buyer', '固定问候')
        await self.push(mid='old-delayed.PNM', stamp=stamp-1)
        self.assertEqual(self.instance.send_im_text.await_count, 1)

    async def test_client_id_fallback_without_biztag(self):
        parsed = {'1': {'10': {'extJson': json.dumps({'messageId': 'client-1'})}}}
        self.assertEqual(self.instance._extract_message_id(parsed), 'client-1')
        for bad in ([], {}, True, '', None):
            self.assertIsNone(self.instance._extract_message_id({'1': {'3': bad}}))


if __name__ == '__main__':
    unittest.main()
