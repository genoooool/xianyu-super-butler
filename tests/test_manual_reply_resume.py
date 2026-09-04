"""Manual replies keep control off; explicit switches own restoration and echo watermarks."""
import ast
import asyncio
import io
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from fastapi import HTTPException

from app.services.human_handoff import HumanHandoffs, initialize_schema, outgoing_precedes_resume
from app.services.manual_reply import receipt_message_id, send_manual_reply
from app.services.reply_assets import ReplyAssets, initialize_schema as assets_schema
from app.services.reply_delivery import ReplyDeliveryError


def receipt(message_id='manual-1'):
    return {'headers': {'code': 200, 'mid': 'offline-request'}, 'body': {'messageId': message_id}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(conn=sqlite3.connect(':memory:', check_same_thread=False), lock=threading.RLock())
        self.db.conn.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY); INSERT INTO users VALUES(1),(2);
            CREATE TABLE cookies(id TEXT PRIMARY KEY,user_id INTEGER);
            INSERT INTO cookies VALUES('a',1),('b',1),('other',2);''')
        initialize_schema(self.db.conn.cursor()); assets_schema(self.db.conn.cursor()); self.db.conn.commit()
        self.service = HumanHandoffs(self.db)
        self.clear = Mock()
        self.instance = SimpleNamespace(cookie_id='a', myid='seller', cookies_str='offline',
            send_im_text=AsyncMock(return_value=receipt()), _send_im_request=AsyncMock(return_value=receipt('image-1')))
        picture = io.BytesIO(); Image.new('RGB', (8, 8), 'gold').save(picture, format='PNG')
        self.image = ReplyAssets(self.db).save(1, picture.getvalue())['id']
        uploader = SimpleNamespace(upload_reply_bytes=AsyncMock(return_value='https://img.alicdn.com/offline.png'))
        manager = AsyncMock(); manager.__aenter__.return_value = uploader
        self.upload = patch.dict(sys.modules, {'utils.image_uploader': SimpleNamespace(ImageUploader=Mock(return_value=manager))})
        self.upload.start()

    def tearDown(self):
        self.upload.stop(); self.db.conn.close()

    def begin(self, cookie='a', chat='chat', revision=0, stamp=1):
        return self.service.begin(1, cookie, chat, revision, stamp, 'unclear', 'buyer', '买家', 'item')

    def send(self, **changes):
        data = dict(instance=self.instance, db=self.db, owner_id=1, cid='chat', toid='buyer',
                    text='人工回复', images=[], clear_timed_pause=self.clear)
        data.update(changes)
        return asyncio.run(send_manual_reply(**data))


class ManualControlTests(Fixture):
    def test_observed_top_level_200_receipt_keeps_manual_control(self):
        self.begin()
        self.instance.send_im_text.return_value = {'code': 200, 'headers': {'mid': '12345 0'},
                                                   'body': {'messageId': 'manual-1.PNM'}}
        self.assertEqual(self.send()['handoff_auto_resume'], 'disabled')
        self.clear.assert_not_called()
        self.assertFalse(self.service.control(1, 'a', 'chat')['enabled'])

    def test_confirmed_text_keeps_only_target_off_and_rejects_old_work(self):
        self.begin(); self.begin(cookie='b'); self.begin(chat='another')
        result = self.send(cid='chat@goofish', toid='buyer@goofish')
        self.assertEqual(result['handoff_auto_resume'], 'disabled')
        self.clear.assert_not_called()
        self.instance.send_im_text.assert_awaited_once_with('chat', 'buyer', '人工回复')
        state = self.service.state(1, 'a', 'chat')
        self.assertTrue(state['pending'])
        self.assertEqual({(v['cookie_id'], v['chat_id']) for v in self.service.pending(1)}, {('b', 'chat'), ('a', 'another')})
        self.assertFalse(self.service.can_reply(1, 'a', 'chat', 1, state['resumed_ms'] + 1))
        self.assertFalse(self.service.can_reply(1, 'a', 'chat', 2, state['resumed_ms']))
        self.assertFalse(self.service.can_reply(1, 'a', 'chat', 2, state['resumed_ms'] + 1))

    def test_images_wait_until_every_part_has_confirmed(self):
        self.begin()
        async def send_image(*args):
            self.clear.assert_not_called()
            self.assertTrue(self.service.state(1, 'a', 'chat')['pending'])
            return receipt('image-1')
        self.instance._send_im_request.side_effect = send_image
        result = self.send(images=[self.image])
        self.assertEqual(result['parts'], 2)
        self.assertEqual(result['messageId'], 'image-1')
        self.clear.assert_not_called()
        self.assertFalse(self.service.control(1, 'a', 'chat')['enabled'])

    def test_image_only_reply_also_keeps_manual_control(self):
        self.begin()
        result = self.send(text='', images=[self.image])
        self.assertEqual(result['parts'], 1)
        self.assertEqual(result['handoff_auto_resume'], 'disabled')
        self.instance.send_im_text.assert_not_called()

    def test_partial_image_failure_retains_takeover_and_never_retries(self):
        self.begin()
        self.instance._send_im_request.side_effect = TimeoutError()
        with self.assertRaises(ReplyDeliveryError) as error: self.send(images=[self.image])
        self.assertEqual(error.exception.sent_count, 1)
        self.assertTrue(self.service.state(1, 'a', 'chat')['pending'])
        self.clear.assert_not_called()
        self.assertEqual(self.instance.send_im_text.await_count, 1)
        self.assertEqual(self.instance._send_im_request.await_count, 1)

    def test_rejected_missing_or_malformed_receipt_never_resumes(self):
        self.begin()
        for response in (None, {}, {'body': {}}, {'headers': {'code': 500}, 'body': {}},
                         {'headers': {'code': 200}, 'body': {'success': False}},
                         {'headers': {'code': 200}, 'body': {'code': 403}},
                         {'headers': {'code': 200}, 'body': {'reason': 'rejected'}},
                         {'headers': {'code': 200}, 'body': []}):
            with self.subTest(response=response):
                self.instance.send_im_text.return_value = response
                with self.assertRaises(RuntimeError): self.send()
                self.assertTrue(self.service.state(1, 'a', 'chat')['pending'])
        self.clear.assert_not_called()

    def test_timeout_and_cancellation_preserve_takeover(self):
        self.begin()
        for error in (TimeoutError(), asyncio.CancelledError()):
            self.instance.send_im_text.side_effect = error
            with self.assertRaises(type(error)): self.send()
            self.assertTrue(self.service.state(1, 'a', 'chat')['pending'])
        self.clear.assert_not_called()

    def test_normal_reply_closes_only_target_without_creating_ai_attention(self):
        self.begin(chat='another')
        result = self.send()
        self.assertEqual(result['handoff_auto_resume'], 'disabled')
        self.assertFalse(self.service.control(1, 'a', 'chat')['enabled'])
        self.assertEqual([entry['chat_id'] for entry in self.service.pending(1)], ['another'])
        self.clear.assert_not_called()

    def test_handoff_created_during_normal_send_is_not_cleared(self):
        async def send(*args):
            self.begin()
            return receipt()
        self.instance.send_im_text.side_effect = send
        self.assertEqual(self.send()['handoff_auto_resume'], 'disabled')
        self.assertTrue(self.service.state(1, 'a', 'chat')['pending'])
        self.clear.assert_not_called()

    def test_new_revision_during_send_is_not_cleared(self):
        self.begin()
        async def send(*args):
            revision = self.service.state(1, 'a', 'chat')['revision']
            self.service.resume(1, 'a', 'chat', revision)
            state = self.service.state(1, 'a', 'chat')
            self.begin(revision=state['revision'], stamp=state['resumed_ms'] + 1)
            return receipt()
        self.instance.send_im_text.side_effect = send
        self.assertEqual(self.send()['handoff_auto_resume'], 'disabled')
        state = self.service.state(1, 'a', 'chat')
        self.assertTrue(state['pending']); self.assertEqual(state['revision'], 4)
        self.clear.assert_not_called()

    def test_wrong_buyer_does_not_release_pending_conversation(self):
        self.begin()
        self.assertEqual(self.send(toid='another-buyer')['handoff_auto_resume'], 'disabled')
        self.assertTrue(self.service.state(1, 'a', 'chat')['pending'])
        self.clear.assert_not_called()

    def test_user_switching_on_while_manual_send_awaits_ack_is_not_overwritten(self):
        async def send(*args):
            state = self.service.control(1, 'a', 'chat')
            self.assertFalse(state['enabled'])
            self.service.set_enabled(1, 'a', 'chat', True, state['revision'])
            return receipt()
        self.instance.send_im_text.side_effect = send
        self.send()
        self.assertTrue(self.service.control(1, 'a', 'chat')['enabled'])
        self.clear.assert_not_called()

    def test_foreign_owner_cannot_send(self):
        self.begin()
        with self.assertRaises(PermissionError): self.send(owner_id=2)
        self.instance.send_im_text.assert_not_called()
        self.clear.assert_not_called()

    def test_pause_storage_error_stops_before_any_send(self):
        self.begin()
        with patch.object(HumanHandoffs, 'pause_manual', side_effect=sqlite3.OperationalError('offline disk error')):
            with self.assertRaises(sqlite3.OperationalError): self.send()
        self.assertTrue(self.service.state(1, 'a', 'chat')['pending'])
        self.clear.assert_not_called()
        self.instance.send_im_text.assert_not_called()

    def test_receipt_ids_are_never_fabricated_from_request_id(self):
        self.assertEqual(receipt_message_id({'headers': {'mid': 'request'}, 'body': {}}), '')
        self.assertEqual(receipt_message_id({'body': {'data': {'msgId': 123}}}), '123')


class EchoTests(Fixture):
    def setUp(self):
        super().setUp()
        source = Path(__file__).resolve().parents[1] / 'XianyuAutoAsync.py'
        tree = ast.parse(source.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'AutoReplyPauseManager')
        env = dict(time=time, logger=Mock())
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), env)
        self.manager_type = env['AutoReplyPauseManager']
        self.manager = self.manager_type()
        self.db.get_cookie_pause_duration = Mock(return_value=10)
        self.module = patch.dict(sys.modules, {'app.db_manager': SimpleNamespace(db_manager=self.db)})
        self.module.start()

    def tearDown(self):
        self.module.stop(); super().tearDown()

    def test_early_and_late_exact_echo_cannot_repause_target(self):
        self.begin()
        self.manager.pause_chat('chat', 'a', message_id='manual-1')
        self.manager.pause_chat('chat', 'b')
        self.manager.pause_chat('another', 'a')
        self.send()
        state = self.service.state(1, 'a', 'chat')
        self.service.set_enabled(1, 'a', 'chat', True, state['revision'])
        self.manager.resume_chat('chat', 'a', ['manual-1'])
        self.manager.pause_chat('chat', 'a', message_id='manual-1', message_ms=int(time.time()*1000)+100000)
        self.assertFalse(self.manager.is_chat_paused('chat', 'a'))
        self.assertTrue(self.manager.is_chat_paused('chat', 'b'))
        self.assertTrue(self.manager.is_chat_paused('another', 'a'))
        self.manager.pause_chat('new-chat', 'a', message_id='manual-1')
        self.assertTrue(self.manager.is_chat_paused('new-chat', 'a'))

    def test_persisted_watermark_ignores_old_echo_after_restart_but_not_new_mobile_reply(self):
        self.begin(); self.send()
        state = self.service.state(1, 'a', 'chat')
        self.service.set_enabled(1, 'a', 'chat', True, state['revision'])
        stamp = self.service.state(1, 'a', 'chat')['resumed_ms']
        restarted = self.manager_type()
        restarted.pause_chat('chat', 'a', message_ms=stamp)
        self.assertFalse(restarted.is_chat_paused('chat', 'a'))
        restarted.pause_chat('chat', 'a', message_id='new-mobile-reply', message_ms=stamp+1)
        self.assertTrue(restarted.is_chat_paused('chat', 'a'))

    def test_invalid_timestamp_or_new_pending_state_never_bypasses_pause(self):
        self.begin(); self.send()
        state = self.service.state(1, 'a', 'chat')
        self.service.set_enabled(1, 'a', 'chat', True, state['revision'])
        stamp = self.service.state(1, 'a', 'chat')['resumed_ms']
        for invalid in (None, '', float('nan'), float('inf'), -1, 0):
            self.assertFalse(outgoing_precedes_resume(self.db, 'a', 'chat', invalid))
        self.begin(revision=3, stamp=stamp+1)
        self.assertFalse(outgoing_precedes_resume(self.db, 'a', 'chat', stamp))
        self.manager.pause_chat('chat', 'a', message_ms=stamp)
        self.assertTrue(self.manager.is_chat_paused('chat', 'a'))

    def test_echo_cache_is_bounded_and_expired_ids_do_not_suppress_new_pauses(self):
        self.manager.resume_chat('chat', 'a', list(map(str, range(2100))))
        self.assertLessEqual(len(self.manager.confirmed_reply_echoes), 2048)
        self.manager.confirmed_reply_echoes[('a', 'chat', 'expired')] = time.time()-1
        self.manager.pause_chat('chat', 'a', message_id='expired')
        self.assertTrue(self.manager.is_chat_paused('chat', 'a'))


class ManualRouteTests(Fixture):
    def setUp(self):
        super().setUp()
        source = Path(__file__).resolve().parents[1] / 'app/reply_server.py'
        node = next(n for n in ast.parse(source.read_text()).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == 'send_chat_message')
        node.decorator_list = []
        for arg in node.args.args: arg.annotation = None
        node.args.defaults = [ast.Constant(None) for _ in node.args.defaults]
        def owned(cookie, user):
            if self.service.owner(cookie) != user['user_id']: raise HTTPException(404)
        async def run(cookie, operation):
            self.assertEqual(cookie, self.instance.cookie_id)
            return await operation(self.instance)
        self.runner = AsyncMock(side_effect=run)
        env = dict(_get_owned_chat_account=owned, _run_on_account_loop=self.runner,
                   db_manager=self.db, _clear_handoff_timed_pause=self.clear,
                   HTTPException=HTTPException, logger=Mock())
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(source), 'exec'), env)
        self.route = env['send_chat_message']

    def request(self, owner=1, text='人工回复', images=None):
        return asyncio.run(self.route('a', SimpleNamespace(cid='chat', to_user_id='buyer',
            text=text, image_ids=images or []), {'user_id': owner, 'username': 'offline'}))

    def test_actual_route_returns_disabled_auto_resume_metadata_for_text(self):
        self.begin()
        result = self.request()
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['handoff_auto_resume'], 'disabled')
        self.assertNotIn('handoff_resumed_revision', result['data'])
        self.runner.assert_awaited_once()

    def test_actual_route_returns_metadata_for_image_only(self):
        self.begin()
        result = self.request(text='', images=[self.image])
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['handoff_auto_resume'], 'disabled')
        self.instance.send_im_text.assert_not_called()

    def test_actual_route_retains_auth_and_validation_before_sending(self):
        for kwargs, status in (({'owner': 2}, 404), ({'text': ' '}, 400), ({'images': ['invalid']}, 400)):
            with self.subTest(kwargs=kwargs), self.assertRaises(HTTPException) as error: self.request(**kwargs)
            self.assertEqual(error.exception.status_code, status)
        self.runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
