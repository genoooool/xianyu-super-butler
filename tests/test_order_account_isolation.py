"""Offline buyer/seller event replay: actual runtime methods and in-memory SQL only."""
import asyncio
import base64
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.db_manager import DBManager
from app.order_status_handler import OrderStatusHandler
from XianyuAutoAsync import XianyuLive
from utils.order_detail_fetcher import OrderDetailFetcher, fetch_order_detail_simple
from utils.seller_order_sync import _save_order, fetch_order_detail_direct


class OrderAccountIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = DBManager.__new__(DBManager)
        self.db.sql_log_enabled = False
        self.db.lock = threading.RLock()
        self.db.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.conn.executescript('''
            CREATE TABLE cookies(id TEXT PRIMARY KEY, value TEXT, created_at TEXT);
            INSERT INTO cookies VALUES('store', 'unb=100; test=offline', ''),
                ('buyer-store', 'unb=200; test=offline', '');
            CREATE TABLE item_info(cookie_id TEXT, item_id TEXT, item_price TEXT);
            INSERT INTO item_info VALUES('store', 'item', '1.00');
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, cookie_id TEXT, item_id TEXT,
                buyer_id TEXT, spec_name TEXT, spec_value TEXT, quantity TEXT, amount TEXT,
                order_status TEXT, is_bargain INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP, version INTEGER DEFAULT 1,
                chat_id TEXT, receiver_name TEXT, receiver_phone TEXT, receiver_address TEXT,
                receiver_city TEXT, system_shipped INTEGER DEFAULT 0, buy_num INTEGER,
                auction_price TEXT, confirm_fee TEXT, refund_fee TEXT, post_fee TEXT);
        ''')
        self.addCleanup(self.db.conn.close)
        self.db_patch = patch('app.db_manager.db_manager', self.db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.browser_patch = patch.object(OrderDetailFetcher, 'init_browser', new_callable=AsyncMock,
                                         side_effect=AssertionError('No real browser in order replay'))
        self.browser = self.browser_patch.start()
        self.addCleanup(self.browser_patch.stop)
        self.record = dict(order_id='order', item_id='item', buyer_id='200', amount='0.10',
                           buy_num=1, status_text='待发货')
        self.detail = {**self.record, 'spec_name': '规格', 'spec_value': '教程',
                       'seller_verified': True, 'order_status': 'pending_ship'}
        self.locks = defaultdict(asyncio.Lock)

    def live(self, cookie_id):
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = cookie_id
        live.myid = '100' if cookie_id == 'store' else '200'
        live.cookies_str = f'unb={live.myid}'
        live._pending_order_real_values = {}
        live._order_detail_locks = self.locks
        live._order_detail_lock_times = {}
        live.order_status_handler = OrderStatusHandler()
        live.is_sync_package = lambda _: True
        live.is_chat_message = lambda _: False
        live._extract_order_id = lambda _: 'order'
        live.extract_item_id_from_message = lambda _: 'item'
        live.fetch_order_real_values = AsyncMock(return_value=self.record if cookie_id == 'store' else {})
        return live

    def message(self, sender='200'):
        return {'1': {'2': 'chat@goofish', '10': {
            'senderUserId': sender, 'reminderContent': '[我已付款，等待你发货]',
            'reminderUrl': 'https://www.goofish.com/?itemId=item',
        }}}

    def save(self, **changes):
        return self.db.insert_or_update_order(**{
            'order_id': 'order', 'cookie_id': 'store', 'item_id': 'item',
            'buyer_id': '200', 'order_status': 'pending_ship', 'amount': '0.10', **changes,
        })

    async def replay(self, first, second, concurrent=False):
        async def direct(cookie, *_):
            # The buyer can read SKU details, but does not have a sold.get record.
            return dict(self.detail) if cookie == 'store' else {
                'spec_value': '教程', 'seller_verified': False, 'from_seller_api': True,
            }

        async def receive(cookie):
            live = self.live(cookie)
            payload = self.message('200' if cookie == 'store' else '100')
            envelope = {'headers': {'mid': 'offline'}, 'body': {'syncPushPackage': {'data': [
                {'data': base64.b64encode(json.dumps(payload).encode()).decode()}
            ]}}}
            with patch('app.cookie_manager.manager', None):
                await live.handle_message(envelope, SimpleNamespace(send=AsyncMock()))

        with patch('utils.seller_order_sync.fetch_order_detail_direct', side_effect=direct):
            if concurrent:
                await asyncio.gather(receive(first), receive(second))
            else:
                await receive(first)
                if first == 'buyer-store':
                    self.assertIsNone(self.db.get_order_by_id('order'))
                await receive(second)
        row = self.db.get_order_by_id('order')
        self.assertIsNotNone(row)
        self.assertEqual((row['cookie_id'], row['buyer_id'], row['item_id']), ('store', '200', 'item'))
        self.assertEqual(row['order_status'], 'pending_ship')
        self.assertEqual(row['amount'], '0.10')
        self.assertEqual(row['spec_value'], '教程')
        self.assertEqual(self.db.conn.execute('SELECT system_shipped FROM orders').fetchone()[0], 0)
        self.browser.assert_not_awaited()

    async def test_buyer_event_first_cannot_claim_seller_order(self):
        await self.replay('buyer-store', 'store')

    async def test_seller_event_first_cannot_be_overwritten(self):
        await self.replay('store', 'buyer-store')

    async def test_two_account_receive_tasks_preserve_seller(self):
        await self.replay('buyer-store', 'store', concurrent=True)

    def test_db_rejects_foreign_first_insert_even_with_wrong_sender_as_buyer(self):
        self.assertFalse(self.save(cookie_id='buyer-store', buyer_id='100'))
        self.assertIsNone(self.db.get_order_by_id('order'))
        self.assertTrue(self.save())

    def test_db_rejects_entire_cross_account_update_without_changing_any_fields(self):
        self.assertTrue(self.save())
        before = self.db.conn.execute('SELECT * FROM orders').fetchall()
        self.assertFalse(self.save(cookie_id='buyer-store', amount='999', order_status='shipped'))
        self.assertEqual(self.db.conn.execute('SELECT * FROM orders').fetchall(), before)

    def test_db_concurrent_writers_cannot_transfer_ownership(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda cookie: self.save(cookie_id=cookie), ['buyer-store', 'store'] * 8))
        self.assertEqual(results, [False, True] * 8)
        self.assertEqual(self.db.get_order_by_id('order')['cookie_id'], 'store')

    def test_db_uses_platform_identity_not_local_account_alias(self):
        self.assertFalse(self.save(item_id='not-synced', cookie_id='buyer-store'))
        self.assertIsNone(self.db.get_order_by_id('order'))

    def test_same_owner_updates_and_optimistic_version_still_work(self):
        self.assertTrue(self.save())
        self.assertTrue(self.db.insert_or_update_order('order', cookie_id='store', amount='0.20', expected_version=1))
        self.assertFalse(self.db.insert_or_update_order('order', cookie_id='store', amount='999', expected_version=1))
        self.assertTrue(self.db.insert_or_update_order('order', system_shipped=True))
        self.assertEqual(self.db.get_order_by_id('order')['amount'], '0.20')

    def test_delayed_buyer_status_event_cannot_mutate_or_report_success(self):
        handler = OrderStatusHandler()
        self.assertFalse(handler.update_order_status('order', 'cancelled', 'buyer-store'))
        self.assertTrue(self.save())
        before = self.db.conn.execute('SELECT * FROM orders').fetchall()
        self.assertFalse(handler.process_pending_updates('order'))
        self.assertFalse(handler.update_order_status('order', 'pending_ship', 'buyer-store'))
        self.assertEqual(self.db.conn.execute('SELECT * FROM orders').fetchall(), before)
        self.assertTrue(handler.update_order_status('order', 'shipped', 'store'))

    async def test_both_cache_paths_reject_foreign_owner_and_allow_seller(self):
        self.assertTrue(self.save(spec_name='规格', spec_value='教程'))
        for cookie, expected in [('buyer-store', False), ('store', True)]:
            simple = await fetch_order_detail_simple('order', 'offline', cookie_id=cookie)
            fetcher = OrderDetailFetcher('offline', cookie_id=cookie)
            direct = await fetcher.fetch_order_detail('order')
            self.assertEqual(bool(simple), expected)
            self.assertEqual(bool(direct), expected)
            if expected:
                self.assertTrue(simple['from_cache'])
                self.assertTrue(direct['from_cache'])
        self.browser.assert_not_awaited()

    async def test_missing_account_never_reads_shared_cache(self):
        self.assertTrue(self.save())
        with patch.object(self.db, 'get_order_by_id') as lookup:
            with patch.object(OrderDetailFetcher, 'init_browser', new_callable=AsyncMock, return_value=False):
                self.assertIsNone(await fetch_order_detail_simple('order'))
            fetcher = OrderDetailFetcher('offline')
            with patch.object(fetcher, '_ensure_browser_ready', return_value=False):
                self.assertIsNone(await fetcher.fetch_order_detail('order'))
            lookup.assert_not_called()

    async def test_unknown_product_requires_sold_record_not_buyer_sku_result(self):
        live = self.live('buyer-store')
        with patch('utils.seller_order_sync.fetch_order_detail_direct', new_callable=AsyncMock,
                   return_value={'spec_value': '教程', 'from_seller_api': True, 'seller_verified': False}):
            self.assertIsNone(await live.fetch_order_detail_info('order', 'not-synced', '100'))
        self.assertIsNone(self.db.get_order_by_id('order'))
        self.browser.assert_not_awaited()

    async def test_sku_only_refresh_preserves_known_paid_state_and_amount(self):
        self.assertTrue(self.save())
        with patch('utils.seller_order_sync.fetch_order_detail_direct', new_callable=AsyncMock,
                   return_value={'spec_value': '教程', 'order_status': 'unknown', 'seller_verified': False}):
            self.assertIsNotNone(await self.live('store').fetch_order_detail_info('order', 'item', '200'))
        row = self.db.get_order_by_id('order')
        self.assertEqual((row['order_status'], row['amount']), ('pending_ship', '0.10'))

    async def test_verified_seller_can_fetch_unsynced_product_and_overrides_sender_hint(self):
        detail = {**self.detail, 'item_id': 'not-synced'}
        with patch('utils.seller_order_sync.fetch_order_detail_direct', new_callable=AsyncMock, return_value=detail):
            result = await self.live('store').fetch_order_detail_info('order', 'not-synced', '100')
        self.assertIsNotNone(result)
        row = self.db.get_order_by_id('order')
        self.assertEqual((row['buyer_id'], row['item_id']), ('200', 'not-synced'))

    def test_seller_sync_keeps_unsynced_item_support_but_cannot_steal_order(self):
        self.assertTrue(_save_order(self.db, 'store', {**self.record, 'item_id': 'not-synced'}))
        self.assertFalse(_save_order(self.db, 'buyer-store', self.record))
        self.assertEqual(self.db.get_order_by_id('order')['cookie_id'], 'store')

    async def test_preexisting_wrong_owner_is_not_silently_repaired(self):
        # Simulate a legacy corrupt row; normal admission correctly rejects this insert now.
        self.db.conn.execute('INSERT INTO orders(order_id,cookie_id,item_id,buyer_id,order_status) '
                             "VALUES('order','buyer-store','item','200','pending_ship')")
        self.db.conn.commit()
        before = self.db.conn.execute('SELECT * FROM orders').fetchall()
        self.assertFalse(self.save())
        self.assertFalse(self.save(cookie_id='buyer-store'))
        with patch('utils.seller_order_sync.fetch_order_detail_direct', new_callable=AsyncMock) as direct:
            self.assertIsNone(await self.live('store').fetch_order_detail_info('order', 'item', '200'))
            direct.assert_not_awaited()
        self.assertEqual(self.db.conn.execute('SELECT * FROM orders').fetchall(), before)

    async def test_db_refusal_during_detail_fetch_is_not_returned_as_success(self):
        with patch('utils.seller_order_sync.fetch_order_detail_direct', new_callable=AsyncMock, return_value=self.detail):
            with patch.object(self.db, 'insert_or_update_order', return_value=False) as save:
                self.assertIsNone(await self.live('store').fetch_order_detail_info('order', 'item', '200'))
                save.assert_called_once()

    async def test_bargain_write_refusal_stops_before_platform_action(self):
        live = self.live('store')
        live.order_status_handler = None
        live.is_chat_message = lambda _: True
        live.fetch_order_detail_info = AsyncMock(return_value=None)
        live.auto_freeshipping = AsyncMock()
        live._handle_auto_delivery = AsyncMock()
        payload = self.message()
        payload['1']['10'].update(reminderContent='[卡片消息]', sessionType='30')
        payload['1']['6'] = {'3': {'5': json.dumps({
            'dxCard': {'item': {'main': {'exContent': {'title': '我已小刀，待刀成'}}}}
        })}}
        envelope = {'headers': {'mid': 'offline'}, 'body': {'syncPushPackage': {'data': [
            {'data': base64.b64encode(json.dumps(payload).encode()).decode()}
        ]}}}
        for error in (None, RuntimeError('offline write failure')):
            with self.subTest(error=error), patch('app.cookie_manager.manager', None):
                with patch.object(self.db, 'insert_or_update_order', return_value=False, side_effect=error) as save:
                    await live.handle_message(envelope, SimpleNamespace(send=AsyncMock()))
                    save.assert_called_once()
                    self.assertTrue(save.call_args.kwargs['is_bargain'])
            live.auto_freeshipping.assert_not_awaited()
            live._handle_auto_delivery.assert_not_awaited()

    async def test_sku_only_endpoint_does_not_prove_seller_role(self):
        api = SimpleNamespace(
            get_sold_orders=AsyncMock(return_value={'items': []}),
            get_order_detail=AsyncMock(return_value={'components': [{'render': 'orderInfoVO',
                'data': {'itemInfo': {'skuInfo': '规格:教程'}}}]}),
            close=AsyncMock(),
        )
        with patch('utils.seller_order_sync.XianyuSellerAPI', return_value=api):
            result = await fetch_order_detail_direct('buyer-store', 'offline', 'order')
        self.assertEqual(result['spec_value'], '教程')
        self.assertIs(result['seller_verified'], False)
        api.close.assert_awaited_once()

    async def test_only_matching_sold_record_proves_seller_role(self):
        api = SimpleNamespace(
            get_sold_orders=AsyncMock(return_value={'items': ['offline-record']}),
            get_order_detail=AsyncMock(return_value={'components': [{'render': 'orderInfoVO',
                'data': {'itemInfo': {'skuInfo': '规格:教程'}}}]}),
            close=AsyncMock(),
        )
        for order_id, expected in [('order', True), ('different-order', False)]:
            with patch('utils.seller_order_sync.XianyuSellerAPI', return_value=api):
                with patch('utils.seller_order_sync.parse_sold_order', return_value=self.record):
                    result = await fetch_order_detail_direct('store', 'offline', order_id)
            self.assertIs(result['seller_verified'], expected)


if __name__ == '__main__':
    unittest.main()
