"""Real methods + in-memory SQL; no platform, browser, card or production data."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_order_account_isolation as isolation
from utils.order_detail_fetcher import OrderDetailFetcher, fetch_order_detail_simple
from utils.order_status_rules import normalize_order_status


class OrderStatusFreshnessTests(unittest.IsolatedAsyncioTestCase):
    save = isolation.OrderAccountIsolationTests.save
    message = isolation.OrderAccountIsolationTests.message

    def setUp(self):
        isolation.OrderAccountIsolationTests.setUp(self)
        self.live = isolation.OrderAccountIsolationTests.live(self, 'store')
        self.live.order_status_handler = None
        self.live._order_status_requests_allowed = Mock(return_value=True)
        self.live._order_status_started_ms = int(time.time() * 1000) - 1000
        self.live._recent_order_status_tasks = {}
        self.live.background_tasks = set()
        self.db.get_item_delivery_config = Mock(return_value={'is_multi_spec': False})
        self.db.get_item_multi_spec_status = Mock(return_value=False)

    async def asyncTearDown(self):
        await self.live._cancel_order_status_refreshes()

    def test_unpaid_labels_do_not_mean_paid(self):
        for text in ['待付款', '等待买家付款', '买家未付款', '处理中']:
            self.assertEqual(normalize_order_status('', text), 'processing')
        self.assertEqual(normalize_order_status('', '新订单'), 'unknown')

    def test_verified_status_wins_over_delayed_unpaid_card(self):
        self.live._pending_order_real_values['order'] = self.record
        message = self.message()
        message['1']['10']['reminderContent'] = '[我已拍下，待付款]'
        self.assertTrue(self.live._save_order_event_snapshot('order', message, 'item', '200'))
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'pending_ship')

    def test_verified_status_can_save_generic_transaction_card(self):
        self.live._pending_order_real_values['order'] = self.record
        message = self.message()
        message['1']['10']['reminderContent'] = '[卡片消息]'
        self.assertTrue(self.live._save_order_event_snapshot('order', message, 'item', '200'))
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'pending_ship')

    def test_late_observations_never_reopen_advanced_orders(self):
        for current, incoming in [('pending_ship', 'processing'), ('shipped', 'processing'),
                                  ('completed', 'pending_ship'), ('cancelled', 'pending_ship'),
                                  ('refunding', 'pending_ship'), ('completed', 'shipped'),
                                  ('pending_ship', 'unknown')]:
            with self.subTest(current=current, incoming=incoming):
                self.assertTrue(self.save(order_status=current))
                self.assertTrue(self.save(order_status=incoming, preserve_status_progress=True))
                self.assertEqual(self.db.get_order_by_id('order')['order_status'], current)

    def test_explicit_transitions_and_version_guard_remain_available(self):
        self.save(order_status='refunding')
        self.assertTrue(self.save(order_status='pending_ship'))
        row = self.db.get_order_by_id('order')
        self.assertFalse(self.save(order_status='shipped', expected_version=row['version'] - 1,
                                   preserve_status_progress=True))
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'pending_ship')

    def test_delayed_full_sync_cannot_undo_newer_payment(self):
        from utils.seller_order_sync import _save_order
        self.save()
        self.assertTrue(_save_order(self.db, 'store', dict(self.record, status_text='待付款')))
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'pending_ship')

    async def test_mutable_amount_cache_is_not_payment_evidence_in_either_layer(self):
        for state in ['processing', 'pending_ship', 'unknown', 'refunding']:
            self.save(order_status=state)
            with patch.object(OrderDetailFetcher, 'init_browser', AsyncMock(return_value=False)) as browser:
                self.assertIsNone(await fetch_order_detail_simple('order', 'offline', cookie_id='store'))
                browser.assert_awaited_once()
            fetcher = OrderDetailFetcher('offline', cookie_id='store')
            with patch.object(fetcher, '_ensure_browser_ready', AsyncMock(return_value=False)) as browser:
                self.assertIsNone(await fetcher.fetch_order_detail('order'))
                browser.assert_awaited_once()

    async def test_force_refresh_reaches_inner_cache_layer(self):
        self.save(order_status='completed')
        with patch.object(OrderDetailFetcher, 'init_browser', AsyncMock(return_value=True)), \
                patch.object(OrderDetailFetcher, '_ensure_browser_ready', AsyncMock(return_value=False)) as ready:
            self.assertIsNone(await fetch_order_detail_simple('order', 'offline', cookie_id='store', force_refresh=True))
            ready.assert_awaited_once()

    async def test_force_refresh_cannot_bypass_account_isolation(self):
        self.save()
        self.assertIsNone(await fetch_order_detail_simple('order', 'offline', cookie_id='buyer-store', force_refresh=True))
        self.assertIsNone(await OrderDetailFetcher('offline', cookie_id='buyer-store').fetch_order_detail('order', force_refresh=True))
        self.browser.assert_not_awaited()

    async def test_no_sku_does_not_discard_fresh_paid_status_or_start_browser(self):
        self.save(order_status='processing')
        detail = {**self.detail, 'spec_name': '', 'spec_value': ''}
        with patch('utils.seller_order_sync.fetch_order_detail_direct', AsyncMock(return_value=detail)):
            result = await self.live.fetch_order_detail_info('order', 'item', '200')
        self.assertEqual(result['order_status'], 'pending_ship')
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'pending_ship')
        self.browser.assert_not_awaited()

    async def test_multi_sku_supplement_cannot_overwrite_fresh_state_or_amount(self):
        self.save(order_status='processing')
        self.db.get_item_delivery_config.return_value = {'is_multi_spec': True}
        detail = {**self.detail, 'spec_name': '', 'spec_value': ''}
        supplement = dict(spec_name='类型', spec_value='教程', order_status='processing', amount='999')
        with patch('utils.seller_order_sync.fetch_order_detail_direct', AsyncMock(return_value=detail)), \
                patch('utils.order_detail_fetcher.fetch_order_detail_simple', AsyncMock(return_value=supplement)) as browser:
            result = await self.live.fetch_order_detail_info('order', 'item', '200')
        self.assertTrue(browser.call_args.kwargs['force_refresh'])
        self.assertEqual((result['order_status'], result['amount'], result['spec_value']), ('pending_ship', '0.10', '教程'))

    async def test_short_poll_updates_sql_when_payment_arrives_later(self):
        self.save(order_status='processing')
        self.live.fetch_order_real_values.side_effect = [dict(self.record, status_text='待付款'), self.record]
        with patch('XianyuAutoAsync.asyncio.sleep', AsyncMock()) as sleep:
            await self.live._poll_recent_order_status('order')
        self.assertEqual(self.live.fetch_order_real_values.await_count, 2)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'pending_ship')

    async def test_poll_is_bounded_when_still_unpaid_or_unknown(self):
        for response in [dict(self.record, status_text='待付款'), {}]:
            self.save(order_status='processing')
            self.live.fetch_order_real_values = AsyncMock(return_value=response)
            with patch('XianyuAutoAsync.asyncio.sleep', AsyncMock()):
                await self.live._poll_recent_order_status('order')
            self.assertEqual(self.live.fetch_order_real_values.await_count, 5)
            self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'processing')

    async def test_closed_status_stops_poll_and_never_becomes_paid(self):
        self.save(order_status='processing')
        self.live.fetch_order_real_values.return_value = dict(self.record, status_text='交易关闭')
        with patch('XianyuAutoAsync.asyncio.sleep', AsyncMock()):
            await self.live._poll_recent_order_status('order')
        self.live.fetch_order_real_values.assert_awaited_once()
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'cancelled')

    async def test_event_schedules_one_job_and_shutdown_cancels_it(self):
        self.save(order_status='processing')
        message = self.message()
        message['1']['5'] = int(time.time() * 1000)
        self.live._schedule_recent_order_status_refresh('order', message)
        self.live._schedule_recent_order_status_refresh('order', message)
        self.assertEqual(len(self.live._recent_order_status_tasks), 1)
        task = self.live._recent_order_status_tasks['order'][1]
        await self.live._cancel_order_status_refreshes()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.live.background_tasks)
        self.live.fetch_order_real_values.assert_not_awaited()

    async def test_historical_missing_future_and_foreign_events_never_schedule(self):
        self.save(order_status='processing')
        for stamp in [None, 1, int(time.time() * 1000) + 10000]:
            message = self.message()
            message['1']['5'] = stamp
            self.live._schedule_recent_order_status_refresh('order', message)
        self.live.cookie_id = 'buyer-store'
        message['1']['5'] = int(time.time() * 1000)
        self.live._schedule_recent_order_status_refresh('order', message)
        self.assertFalse(self.live._recent_order_status_tasks)

    async def test_schedule_has_a_finite_job_cap_and_does_not_restart_completed_job(self):
        self.save(order_status='processing')
        message = self.message()
        message['1']['5'] = int(time.time() * 1000)
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(None)
        self.live._recent_order_status_tasks = {'order': (time.time(), completed)}
        self.live._schedule_recent_order_status_refresh('order', message)
        self.assertIs(self.live._recent_order_status_tasks['order'][1], completed)
        self.live._recent_order_status_tasks = {str(i): (time.time(), completed) for i in range(128)}
        self.live._schedule_recent_order_status_refresh('order', message)
        self.assertNotIn('order', self.live._recent_order_status_tasks)

    async def test_confirm_retries_only_status_reads_before_paid(self):
        self.save(order_status='processing')
        self.live.fetch_order_real_values.side_effect = [{}, dict(self.record, status_text='待付款'), self.record]
        with patch('XianyuAutoAsync.asyncio.sleep', AsyncMock()):
            self.assertTrue(await self.live._confirm_order_payment('order'))
        self.assertEqual(self.live.fetch_order_real_values.await_count, 3)

    async def test_local_paid_cache_cannot_substitute_for_failed_fresh_read(self):
        self.save()
        self.live.fetch_order_real_values.return_value = {}
        with patch('XianyuAutoAsync.asyncio.sleep', AsyncMock()):
            self.assertFalse(await self.live._confirm_order_payment('order'))
        self.assertEqual(self.live.fetch_order_real_values.await_count, 3)

    async def test_wrong_order_item_buyer_and_foreign_owner_fail_closed(self):
        self.save(order_status='processing')
        for changed in [dict(order_id='other'), dict(item_id='other'), dict(buyer_id='other')]:
            self.live.fetch_order_real_values.return_value = dict(self.record, **changed)
            self.assertEqual(await self.live._refresh_order_payment_status('order'), 'unknown')
        self.live.cookie_id = 'buyer-store'
        self.live.fetch_order_real_values.return_value = self.record
        self.assertEqual(await self.live._refresh_order_payment_status('order'), 'unknown')
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'processing')

    async def test_save_failure_cannot_confirm_payment(self):
        self.save()
        with patch.object(self.db, 'insert_or_update_order', return_value=False):
            self.assertEqual(await self.live._refresh_order_payment_status('order'), 'unknown')

    async def test_timeout_is_unknown_and_cancellation_propagates(self):
        self.save()
        self.live.fetch_order_real_values.side_effect = asyncio.TimeoutError()
        self.assertEqual(await self.live._refresh_order_payment_status('order'), 'unknown')
        self.live.fetch_order_real_values.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.live._refresh_order_payment_status('order')

    async def test_disabled_or_risk_blocked_account_does_not_query(self):
        self.save(order_status='processing')
        self.live._order_status_requests_allowed.return_value = False
        with patch('XianyuAutoAsync.asyncio.sleep', AsyncMock()):
            await self.live._poll_recent_order_status('order')
            self.assertFalse(await self.live._confirm_order_payment('order'))
        self.live.fetch_order_real_values.assert_not_awaited()

    async def test_terminal_order_or_concurrent_close_never_confirms_paid(self):
        for state in ['shipped', 'completed', 'cancelled', 'refunding']:
            self.save(order_status=state)
            self.assertFalse(await self.live._confirm_order_payment('order'))
        self.live.fetch_order_real_values.assert_not_awaited()
        self.save(order_status='pending_ship')

        async def closed_during_request(_):
            self.save(order_status='cancelled')
            return self.record

        self.live.fetch_order_real_values.side_effect = closed_during_request
        self.assertFalse(await self.live._confirm_order_payment('order'))
        self.assertEqual(self.db.get_order_by_id('order')['order_status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
