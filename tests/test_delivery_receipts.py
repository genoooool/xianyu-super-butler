"""Exercise actual delivery/IM methods with local platform receipts; no live orders."""
import ast
import asyncio
import base64
from collections import defaultdict
import itertools
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from app.services.reply_delivery import require_receipt  # load before replacing DB module


ROOT = Path(__file__).resolve().parents[1]
METHODS = {
    '_handle_auto_delivery', 'can_auto_delivery', 'mark_delivery_sent',
    '_send_delivery_request', 'send_msg', 'send_image_msg', '_send_im_request',
    '_resolve_im_response', '_fail_pending_im_requests', 'send_im_text',
}


def load_methods():
    tree = ast.parse((ROOT / 'XianyuAutoAsync.py').read_text())
    source_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'XianyuLive')
    methods = [n for n in source_class.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in METHODS]
    assert len(methods) == len(METHODS)
    cls = ast.ClassDef(name='DeliveryLive', bases=[], keywords=[], body=methods, decorator_list=[])
    ids = itertools.count()
    env = dict(asyncio=asyncio, base64=base64, json=json, time=time, logger=Mock(),
               generate_mid=lambda: str(next(ids)), generate_uuid=lambda: str(next(ids)))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(ROOT / 'XianyuAutoAsync.py'), 'exec'), env)
    return env['DeliveryLive']


def load_route(name, env):
    tree = ast.parse((ROOT / 'app/reply_server.py').read_text())
    method = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    method.decorator_list = []
    method.returns = None
    for arg in method.args.args:
        arg.annotation = None
    method.args.defaults = [ast.Constant(None) for _ in method.args.defaults]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(ROOT / 'app/reply_server.py'), 'exec'), env)
    return env[name]


Live = load_methods()


class ReceiptFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.receipts = [{'headers': {'code': 200}, 'body': {}}]
        self.writes = []
        self.written = asyncio.Event()
        self.live = Live()
        live = self.live
        live.cookie_id = 'seller'
        live.myid = 'seller'
        live.delivery_sent_orders = set()
        live.delivery_blocked_orders = set()
        live.last_delivery_time = {}
        live.delivery_cooldown = 600
        live.confirmed_orders = {}
        live._order_locks = defaultdict(asyncio.Lock)
        live._lock_usage_times = {}
        live._lock_hold_info = {}
        live._im_pending = {}
        live._im_request_lock = asyncio.Lock()
        live._safe_str = str
        live._extract_order_id = lambda _: 'order'
        live._is_cdn_url = lambda _: True
        live.is_lock_held = lambda _: False
        live._delayed_lock_release = AsyncMock()
        live.apply_delivery_block_rules = AsyncMock(return_value={'action': 'allow'})
        live.send_delivery_failure_notification = AsyncMock()
        live.is_auto_confirm_enabled = lambda: True
        live.order_status_handler = SimpleNamespace(handle_auto_delivery_order_status=Mock(return_value=True))
        self.parts = ['CARD-1']
        self.part_index = 0

        async def acquire(*args, delivery_context, **kwargs):
            delivery_context['delivery_count'] = len(self.parts)
            content = self.parts[self.part_index]
            self.part_index += 1
            return content

        async def confirm(*args):
            self.events.append('ship')
            return {'success': True}

        async def send(raw):
            message = json.loads(raw)
            self.writes.append(message)
            self.events.append('write')
            self.written.set()
            receipt = self.receipts.pop(0)
            if isinstance(receipt, BaseException):
                raise receipt
            if receipt is not None:
                response = dict(receipt)
                response['headers'] = dict(response.get('headers') or {}, mid=message['headers']['mid'])
                asyncio.get_running_loop().call_soon(self.deliver, response)

        live._auto_delivery = AsyncMock(side_effect=acquire)
        live.auto_confirm = AsyncMock(side_effect=confirm)
        live.ws = SimpleNamespace(closed=False, send=AsyncMock(side_effect=send))
        actual_request = live._send_im_request

        async def fast_request(lwp, body):
            return await actual_request(lwp, body, timeout=.04)

        live._send_im_request = fast_request
        self.order = dict(order_id='order', cookie_id='seller', item_id='item', buyer_id='buyer',
                          chat_id='chat', order_status='pending_ship', system_shipped=False)
        self.db = SimpleNamespace(
            get_item_info=Mock(return_value={'item_id': 'item'}),
            get_order_by_id=Mock(side_effect=lambda _: dict(self.order)),
            get_all_cookies=Mock(return_value={'seller': 'offline-cookie'}),
            get_item_multi_quantity_delivery_status=Mock(return_value=False),
            insert_or_update_order=Mock(),
        )
        self.patch_db = patch.dict('sys.modules', {'app.db_manager': SimpleNamespace(db_manager=self.db)})
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)

    def deliver(self, response):
        self.events.append('receipt')
        return self.live._resolve_im_response(response)

    async def automatic(self):
        return await self.live._handle_auto_delivery(self.live.ws, {}, '测试买家', 'buyer', 'item', 'chat', 'offline')

    async def manual(self, mode='full_delivery'):
        async def dispatch(cookie_id, operation):
            self.assertEqual(cookie_id, 'seller')
            self.events.append('account-loop')
            return await operation(self.live)
        env = dict(asyncio=asyncio, time=time, HTTPException=HTTPException, log_with_user=Mock(), _run_on_account_loop=dispatch)
        route = load_route('manual_ship_orders', env)
        with patch.dict('sys.modules', {'XianyuAutoAsync': SimpleNamespace(XianyuLive=SimpleNamespace(get_instance=lambda _: self.live))}):
            return await route(['order'], mode, None, {'user_id': 1})

    def assert_unshipped(self):
        self.live.auto_confirm.assert_not_awaited()
        self.live.order_status_handler.handle_auto_delivery_order_status.assert_not_called()
        self.db.insert_or_update_order.assert_not_called()
        self.assertNotIn('order', self.live.delivery_sent_orders)
        self.assertIn('order', self.live.delivery_blocked_orders)
        self.assertFalse(self.live.can_auto_delivery('order'))
        self.assertEqual(self.live._im_pending, {})

    async def test_delayed_ack_is_required_before_platform_shipping(self):
        self.receipts = [None]
        task = asyncio.create_task(self.automatic())
        await self.written.wait()
        self.assertNotIn('ship', self.events)
        response = dict(headers=dict(code=200, mid=self.writes[0]['headers']['mid']), body={})
        self.deliver(response)
        await task
        self.assertEqual(self.events, ['write', 'receipt', 'ship'])
        self.db.insert_or_update_order.assert_called_once()
        self.assertTrue(self.db.insert_or_update_order.call_args.kwargs['system_shipped'])
        self.assertNotIn('order', self.live.delivery_blocked_orders)

    async def test_text_and_image_ack_then_shipping(self):
        self.parts = ['CARD-1', '__IMAGE_SEND__9|https://img.alicdn.com/card.png']
        self.receipts *= 2
        await self.automatic()
        self.assertEqual(self.events, ['write', 'receipt', 'write', 'receipt', 'ship'])
        payloads = [json.loads(base64.b64decode(m['body'][0]['content']['custom']['data'])) for m in self.writes]
        self.assertEqual(payloads[0]['text']['text'], 'CARD-1')
        self.assertEqual(payloads[1]['image']['pics'][0]['url'], 'https://img.alicdn.com/card.png')

    async def test_partial_timeout_stops_later_parts_and_repeat_trigger(self):
        self.parts = ['CARD-1', 'CARD-2', 'CARD-3']
        self.receipts = [self.receipts[0], None]
        await self.automatic()
        self.assert_unshipped()
        self.assertEqual(len(self.writes), 2)
        self.assertIn('成功发送 1 个', self.live.send_delivery_failure_notification.call_args.args[3])
        await self.automatic()
        self.assertEqual(len(self.writes), 2)

    async def test_image_rejection_cannot_mark_order_shipped(self):
        self.parts = ['__IMAGE_SEND__https://img.alicdn.com/card.png']
        self.receipts = [dict(headers=dict(code=403), body={})]
        await self.automatic()
        self.assert_unshipped()

    async def test_ambiguous_or_negative_receipts_are_rejected(self):
        for receipt in [dict(body={}), dict(headers=dict(code=500), body={}),
                        dict(headers=dict(code=200), body=dict(reason='denied')),
                        dict(headers=dict(code=200), body=dict(code=403)),
                        dict(headers=dict(code=200), body=dict(success=False)),
                        dict(headers=dict(code=200), body=[]), dict(headers=dict(code=200))]:
            with self.subTest(receipt=receipt):
                self.receipts = [receipt]
                with self.assertRaises(RuntimeError):
                    await self.live.send_msg(self.live.ws, 'chat', 'buyer', 'CARD', wait_for_ack=True)
                self.assertEqual(self.live._im_pending, {})

    async def test_observed_top_level_success_allows_text_image_and_shipping(self):
        self.parts = ['CARD-1', '__IMAGE_SEND__https://img.alicdn.com/card.png']
        self.receipts = [dict(code=200, body=dict(messageId=f'offline-{i}.PNM')) for i in range(2)]
        await self.automatic()
        self.assertEqual(self.events, ['write', 'receipt', 'write', 'receipt', 'ship'])

    async def test_top_level_negative_or_conflicting_receipts_stop_shipping(self):
        for receipt in [dict(code=500, body={}), dict(code=500, headers=dict(code=200), body={}),
                        dict(code=200, headers=dict(code=403), body={}),
                        dict(code=200, body=dict(code=500)), dict(code=200, body=dict(success=False)),
                        dict(code=200, body=dict(error='rejected')), dict(code=True, body={})]:
            with self.subTest(receipt=receipt):
                self.receipts = [receipt]
                with self.assertRaises(RuntimeError):
                    await self.live.send_msg(self.live.ws, 'chat', 'buyer', 'CARD', wait_for_ack=True)
                self.live.auto_confirm.assert_not_awaited()

    async def test_plain_text_uses_same_receipt_validation(self):
        self.receipts = [dict(code=200, body=dict(code=200, messageId='offline.PNM'))]
        response = await self.live.send_im_text('chat', 'buyer', 'hello')
        self.assertEqual(response['body']['messageId'], 'offline.PNM')
        self.receipts = [dict(code=500, body={})]
        with self.assertRaises(RuntimeError):
            await self.live.send_im_text('chat', 'buyer', 'hello')

    async def test_receipt_strict_validation_never_accepts_malformed_or_body_only_success(self):
        for response in [dict(code=200, headers=[] , body={}), dict(code=200, headers=None, body={}),
                         dict(body=dict(code=200, messageId='not-proof')),
                         dict(code=200, success=False, body={}), dict(code=200, error='denied', body={}),
                         dict(code='unknown', headers=dict(code=200), body={})]:
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                require_receipt(response, explicit_success=True)

    async def test_unrelated_receipt_does_not_release_delivery(self):
        self.receipts = [None]
        task = asyncio.create_task(self.automatic())
        await self.written.wait()
        self.assertFalse(self.deliver(dict(headers=dict(mid='unrelated', code=200), body={})))
        await task
        self.assert_unshipped()
        self.assertFalse(self.deliver(dict(headers=dict(mid=self.writes[0]['headers']['mid'], code=200), body={})))
        self.live.auto_confirm.assert_not_awaited()

    async def test_disconnect_stops_delivery(self):
        self.receipts = [None]
        task = asyncio.create_task(self.automatic())
        await self.written.wait()
        self.live._fail_pending_im_requests('offline disconnect')
        await task
        self.assert_unshipped()

    async def test_cancellation_during_write_cleans_pending_and_keeps_block(self):
        async def blocked_write(raw):
            self.written.set()
            await asyncio.Event().wait()
        self.live.ws.send.side_effect = blocked_write
        task = asyncio.create_task(self.automatic())
        await self.written.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_unshipped()

    async def test_stalled_socket_write_is_bounded(self):
        async def blocked_write(raw):
            await asyncio.Event().wait()
        self.live.ws.send.side_effect = blocked_write
        await self.automatic()
        self.assert_unshipped()

    async def test_manual_full_delivery_uses_receipts(self):
        result = await self.manual()
        self.assertTrue(result['results'][0]['success'])
        self.assertEqual(self.events, ['account-loop', 'write', 'receipt', 'ship'])

    async def test_manual_full_delivery_accepts_observed_top_level_receipt(self):
        self.receipts = [dict(code=200, body=dict(messageId='manual.PNM'))]
        result = await self.manual()
        self.assertTrue(result['results'][0]['success'])
        self.assertEqual(self.events, ['account-loop', 'write', 'receipt', 'ship'])

    async def test_manual_request_runs_on_actual_account_event_loop(self):
        loop = asyncio.new_event_loop()
        started = threading.Event()
        def run():
            asyncio.set_event_loop(loop)
            loop.call_soon(started.set)
            loop.run_forever()
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(timeout=2))
        async def send(raw):
            self.assertIs(asyncio.get_running_loop(), loop)
            message = json.loads(raw)
            loop.call_soon(self.live._resolve_im_response, dict(headers=dict(code=200, mid=message['headers']['mid']), body={}))
        self.live.ws.send.side_effect = send
        env = dict(asyncio=asyncio, logger=Mock(), HTTPException=HTTPException,
                   cookie_manager=SimpleNamespace(manager=SimpleNamespace(loop=loop, instances={'seller': self.live})),
                   _is_account_connection_alive=lambda _: True)
        dispatch = load_route('_run_on_account_loop', env)
        try:
            receipt = await dispatch('seller', lambda instance: instance.send_msg(instance.ws, 'chat', 'buyer', 'CARD', wait_for_ack=True))
            self.assertEqual(receipt['headers']['code'], 200)
            self.assertEqual(self.live._im_pending, {})
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            loop.close()

    async def test_manual_partial_failure_stops_and_refuses_repeat(self):
        self.parts = ['CARD-1', 'CARD-2', 'CARD-3']
        self.receipts = [self.receipts[0], dict(headers=dict(code=403), body={})]
        result = await self.manual()
        self.assertFalse(result['results'][0]['success'])
        self.assert_unshipped()
        again = await self.manual()
        self.assertFalse(again['results'][0]['success'])
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(self.live._auto_delivery.await_count, 3)

    async def test_manual_image_timeout_prevents_later_text_and_shipping(self):
        self.parts = ['__IMAGE_SEND__9|https://img.alicdn.com/card.png', 'CARD-2']
        self.receipts = [None]
        result = await self.manual()
        self.assertFalse(result['results'][0]['success'])
        self.assert_unshipped()
        self.assertEqual(len(self.writes), 1)

    async def test_platform_shipping_rejection_does_not_advance_order(self):
        self.live.auto_confirm.side_effect = None
        self.live.auto_confirm.return_value = {'success': False, 'error': 'denied'}
        result = await self.manual()
        self.assertFalse(result['results'][0]['success'])
        self.assertNotIn('order_status', self.db.insert_or_update_order.call_args.kwargs)
        self.assertFalse(self.live.can_auto_delivery('order'))

    async def test_auto_confirm_off_preserves_user_choice(self):
        self.live.is_auto_confirm_enabled = lambda: False
        await self.automatic()
        self.live.auto_confirm.assert_not_awaited()
        self.live.order_status_handler.handle_auto_delivery_order_status.assert_not_called()
        self.assertTrue(self.db.insert_or_update_order.call_args.kwargs['system_shipped'])

    async def test_status_only_never_sends_cards_even_when_full_delivery_blocked(self):
        self.live.delivery_blocked_orders.add('order')
        session = AsyncMock()
        confirm = SimpleNamespace(auto_confirm=AsyncMock(return_value={'success': True}))
        with patch.dict('sys.modules', {
            'aiohttp': SimpleNamespace(ClientSession=Mock(return_value=session), ClientTimeout=Mock()),
            'app.secure_confirm': SimpleNamespace(SecureConfirm=Mock(return_value=confirm)),
        }):
            result = await self.manual('status_only')
        self.assertTrue(result['results'][0]['success'])
        self.live._auto_delivery.assert_not_awaited()
        self.live.ws.send.assert_not_awaited()
        confirm.auto_confirm.assert_awaited_once_with('order', 'item')
        self.assertEqual(self.db.insert_or_update_order.call_args.kwargs['order_status'], 'shipped')

    async def test_foreign_order_has_no_delivery_or_confirmation(self):
        self.order['cookie_id'] = 'foreign'
        await self.automatic()
        self.live._auto_delivery.assert_not_awaited()
        self.live.auto_confirm.assert_not_awaited()
        self.live.ws.send.assert_not_awaited()
        result = await self.manual()
        self.assertFalse(result['results'][0]['success'])
        self.live._auto_delivery.assert_not_awaited()
        self.live.auto_confirm.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
