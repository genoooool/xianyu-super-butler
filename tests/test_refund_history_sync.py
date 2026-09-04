"""Real sync + in-memory DB; seller responses mocked, no shipment/network calls."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_order_account_isolation as isolation
from utils.seller_order_sync import sync_account_orders
from utils.xianyu_seller_api import SellerApiError


def sold(oid='order', status='交易成功', *, active=False, buyer='200'):
    return {'commonData': {'orderId': oid, 'itemId': 'item', 'orderStatus': status,
                           'inRefund': active},
            'buyerInfoVO': {'buyerId': buyer, 'userNick': '测试买家'},
            'priceVO': {'totalPrice': '10.00', 'confirmFee': '7.00', 'refundFee': '3.00', 'buyNum': 1}}


def history(oid='order', result='退款关闭'):
    return {'commonData': {'orderId': oid, 'itemId': 'item', 'orderStatus': '已发货退款'},
            'buyerInfoVO': {'buyerId': '200'},
            'priceVO': {'auctionPrice': '999', 'refundFee': '3.00'},
            'refundInfoVO': {'refundStatus': result}}


class RefundHistorySyncTests(unittest.IsolatedAsyncioTestCase):
    save = isolation.OrderAccountIsolationTests.save

    def setUp(self):
        isolation.OrderAccountIsolationTests.setUp(self)
        self.api = SimpleNamespace(
            cookies_str='offline', close=AsyncMock(),
            get_expected_order_total=AsyncMock(return_value=0),
            iter_sold_orders=AsyncMock(return_value=[]),
            iter_refund_orders=AsyncMock(return_value=[history()]),
            get_sold_orders=AsyncMock(return_value={'items': [sold()], 'total_count': 0, 'next_page': True}))
        self.api_patch = patch('utils.seller_order_sync.XianyuSellerAPI', return_value=self.api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    async def sync(self, **kwargs):
        return await sync_account_orders('store', 'offline', **kwargs)

    def rows(self):
        return self.db.conn.execute('SELECT * FROM orders ORDER BY order_id').fetchall()

    async def test_closed_refund_does_not_cancel_completed_trade_or_zero_revenue(self):
        self.save(order_status='refunding')
        result = await self.sync()
        self.assertTrue(result['complete'])
        self.assertEqual((result['saved'], result['from_refund']), (1, 1))
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'completed')
        self.assertEqual(self.db.conn.execute('SELECT amount,confirm_fee,refund_fee FROM orders').fetchone(),
                         ('10.00', '7.00', '3.00'))
        self.api.get_sold_orders.assert_awaited_once_with(order_ids='order', rows_per_page=50)
        self.api.close.assert_awaited_once()

    async def test_refund_paid_is_not_always_closed_trade(self):
        self.api.iter_refund_orders.return_value = [history(result='已退款买家¥3.00')]
        await self.sync()
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'completed')
        self.api.get_sold_orders.return_value = {'items': [sold(status='交易关闭')]}
        await self.sync()
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'cancelled')

    async def test_real_active_refund_flag_is_still_refunding(self):
        self.api.get_sold_orders.return_value = {'items': [sold(active=True)]}
        await self.sync()
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'refunding')

    async def test_missing_unrecognized_or_duplicate_response_never_writes(self):
        self.save(order_status='completed')
        baseline = self.rows()
        for records in ([], [sold('wrong-order')], [sold(status='未知状态')],
                        [sold(), sold(status='交易关闭')],
                        [{'commonData': {'orderId': 'order', 'orderStatus': '交易关闭'}}]):
            with self.subTest(records=records):
                self.api.get_sold_orders.return_value = {'items': records}
                result = await self.sync()
                self.assertFalse(result['complete'])
                self.assertEqual(result['failed'], 1)
                self.assertEqual(self.rows(), baseline)

    async def test_cross_account_buyer_response_cannot_modify_order(self):
        self.save(order_status='completed')
        baseline = self.rows()
        self.api.get_sold_orders.return_value = {'items': [sold(status='交易关闭', buyer='100')]}
        result = await self.sync()
        self.assertEqual(result['failed'], 1)
        self.assertFalse(result['complete'])
        self.assertEqual(self.rows(), baseline)

    async def test_history_query_failure_does_not_fallback_to_refund_type(self):
        self.save(order_status='completed')
        baseline = self.rows()
        for error in [SellerApiError('test', ['FAIL_SYS_SESSION_EXPIRED']), TimeoutError()]:
            self.api.get_sold_orders.side_effect = error
            result = await self.sync()
            self.assertFalse(result['complete'])
            self.assertEqual(self.rows(), baseline)

    async def test_current_sold_list_wins_and_history_duplicate_is_not_requeried(self):
        self.api.iter_sold_orders.return_value = [sold()]
        self.api.iter_refund_orders.return_value = [history(), history()]
        result = await self.sync()
        self.assertTrue(result['complete'])
        self.assertEqual(result['total'], 1)
        self.api.get_sold_orders.assert_not_awaited()

    async def test_history_disabled_does_not_request_or_write_archive(self):
        result = await self.sync(include_refund_history=False)
        self.assertEqual(result['total'], 0)
        self.api.iter_refund_orders.assert_not_awaited()
        self.api.get_sold_orders.assert_not_awaited()
        self.assertEqual(self.rows(), [])

    async def test_batches_and_deduplication_use_requested_ids_not_misleading_counts(self):
        self.api.iter_refund_orders.return_value = [history(str(i)) for i in range(51)] + [history('0')]
        async def exact(**kwargs):
            return {'items': [sold(oid) for oid in kwargs['order_ids'].split(',')],
                    'total_count': 0, 'next_page': True}
        self.api.get_sold_orders.side_effect = exact
        result = await self.sync()
        self.assertTrue(result['complete'])
        self.assertEqual((result['saved'], self.api.get_sold_orders.await_count), (51, 2))

    async def test_query_failure_stops_remaining_batches(self):
        self.api.iter_refund_orders.return_value = [history(str(i)) for i in range(51)]
        self.api.get_sold_orders.side_effect = TimeoutError()
        result = await self.sync()
        self.assertFalse(result['complete'])
        self.assertEqual(result['failed'], 51)
        self.api.get_sold_orders.assert_awaited_once()
        self.assertEqual(self.rows(), [])

    async def test_real_shape_26_order_replay_stays_correct_on_repeated_sync(self):
        expected = {str(i): 'cancelled' if i < 19 else 'completed' for i in range(26)}
        for oid in expected:
            self.save(order_id=oid, order_status='refunding')
        self.api.iter_refund_orders.return_value = [
            history(oid, '已退款买家¥3.00' if int(oid) < 20 else '退款关闭') for oid in expected]
        self.api.get_sold_orders.return_value = {'items': [
            sold(oid, '交易关闭' if state == 'cancelled' else '交易成功') for oid, state in expected.items()]}
        for _ in range(3):
            result = await self.sync()
            self.assertTrue(result['complete'])
            self.assertEqual(result['saved'], 26)
            actual = dict(self.db.conn.execute('SELECT order_id,order_status FROM orders'))
            self.assertEqual(actual, expected)
        self.assertEqual(self.db.conn.execute('SELECT sum(system_shipped) FROM orders').fetchone()[0], 0)
