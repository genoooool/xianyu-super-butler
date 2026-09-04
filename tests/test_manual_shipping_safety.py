"""Offline regression of both manual modes and real platform-confirmation code."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.secure_confirm import SecureConfirm
from app.services import receipt_audit
from app.services.shipping_validation import shipping_owner_error, shipping_response_result
import test_delivery_receipts as flows


class ShippingOwnerTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(get_item_info=Mock(return_value={'item_id': 'item'}))
        self.order = dict(cookie_id='local-seller-alias', item_id='item', buyer_id='buyer-platform')

    def test_seller_alias_uses_real_unb_not_local_id(self):
        self.assertIsNone(shipping_owner_error(self.db, self.order, 'local-seller-alias', 'unb=seller-platform'))
        self.db.get_item_info.assert_called_once_with('local-seller-alias', 'item')

    def test_wrong_account_missing_identity_item_and_buyer_account_are_rejected(self):
        cases = [(self.order, 'different', 'unb=seller-platform'),
                 (self.order, 'local-seller-alias', 'unb=buyer-platform'),
                 (self.order, 'local-seller-alias', 'opaque-cookie'),
                 (dict(self.order, item_id=''), 'local-seller-alias', 'unb=seller-platform')]
        for order, cookie_id, cookies in cases:
            self.assertIsNotNone(shipping_owner_error(self.db, order, cookie_id, cookies))
        self.db.get_item_info.assert_not_called()

    def test_missing_own_item_blocks_even_if_workspace_user_owns_both_accounts(self):
        self.db.get_item_info.return_value = None
        self.assertIn('核对订单归属', shipping_owner_error(self.db, self.order, 'local-seller-alias', 'unb=seller-platform'))


class ManualShippingRouteTests(unittest.IsolatedAsyncioTestCase):
    setUp = flows.ReceiptFlowTests.setUp
    manual = flows.ReceiptFlowTests.manual
    deliver = flows.ReceiptFlowTests.deliver

    async def test_corrupted_buyer_order_blocked_before_network_or_cards_in_both_modes(self):
        self.order['buyer_id'] = 'seller-platform'
        for mode in ['status_only', 'full_delivery']:
            with patch('aiohttp.ClientSession') as session:
                result = await self.manual(mode)
            self.assertFalse(result['results'][0]['success'])
            self.assertIn('买家账号', result['results'][0]['message'])
            session.assert_not_called()
        self.live._auto_delivery.assert_not_awaited()
        self.live.auto_confirm.assert_not_awaited()
        self.assertEqual(self.writes, [])
        self.db.insert_or_update_order.assert_not_called()

    async def test_foreign_item_blocked_before_both_modes(self):
        self.db.get_item_info.return_value = None
        for mode in ['status_only', 'full_delivery']:
            with patch('aiohttp.ClientSession') as session:
                result = await self.manual(mode)
            self.assertFalse(result['results'][0]['success'])
            self.assertIn('核对订单归属', result['results'][0]['message'])
            session.assert_not_called()
        self.live._auto_delivery.assert_not_awaited()

    async def test_status_only_shows_platform_reason_without_changing_sql(self):
        response = SimpleNamespace(status=200, headers={}, json=AsyncMock(return_value={
            'ret': ['PERMISSION_ERROR::用户无权限操作']}))
        request = AsyncMock()
        request.__aenter__.return_value = response
        session = SimpleNamespace(post=Mock(return_value=request))
        context = AsyncMock()
        context.__aenter__.return_value = session
        with patch('aiohttp.ClientSession', return_value=context):
            result = await self.manual('status_only')
        self.assertFalse(result['results'][0]['success'])
        self.assertIn('用户无权限操作', result['results'][0]['message'])
        self.assertNotIn('重试次数', result['results'][0]['message'])
        self.assertEqual(session.post.call_count, 1)
        self.db.insert_or_update_order.assert_not_called()
        self.live._auto_delivery.assert_not_awaited()
        self.assertEqual(self.writes, [])


class ShippingResponseTests(unittest.IsolatedAsyncioTestCase):
    def confirm(self, result=None, error=None, http_status=200):
        response = SimpleNamespace(status=http_status, headers={}, json=AsyncMock(return_value=result, side_effect=error))
        request = AsyncMock()
        request.__aenter__.return_value = response
        self.session = SimpleNamespace(post=Mock(return_value=request))
        return SecureConfirm(self.session, '_m_h5_tk=offline_123; unb=seller', 'local-seller-alias')

    async def test_permission_error_keeps_reason_and_is_sent_once(self):
        confirm = self.confirm({'ret': ['PERMISSION_ERROR::用户无权限操作']})
        with patch.object(receipt_audit, '_emit') as emit:
            result = await confirm.auto_confirm('order', 'item')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'PERMISSION_ERROR')
        self.assertIn('用户无权限操作', result['error'])
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(emit.call_args.args[0]['assessment'], 'rejected')
        self.assertEqual(emit.call_args.args[0]['ret_codes'], ['PERMISSION_ERROR'])

    async def test_success_requires_explicit_platform_success(self):
        self.assertTrue((await self.confirm({'ret': ['SUCCESS::调用成功'], 'data': {}}).auto_confirm('order', 'item'))['success'])
        self.assertEqual(self.session.post.call_count, 1)

    async def test_malformed_conflicting_and_http_failure_never_pass_or_retry(self):
        for result, status in [({}, 200), ([], 200), (None, 200), ({'ret': 'SUCCESS::调用成功'}, 200),
                               ({'ret': ['SUCCESS::调用成功', 'PERMISSION_ERROR::denied']}, 200),
                               ({'ret': ['SUCCESS::调用成功'], 'data': {'success': False}}, 200),
                               ({'ret': ['SUCCESS::调用成功']}, 500),
                               ({'ret': ['SUCCESS::调用成功']}, True)]:
            confirm = self.confirm(result, http_status=status)
            answer = await confirm.auto_confirm('order', 'item')
            self.assertFalse(answer['success'])
            self.assertEqual(self.session.post.call_count, 1)

    async def test_network_timeout_invalid_json_and_disconnect_do_not_retry(self):
        for error in [asyncio.TimeoutError('PRIVATE'), ConnectionError('PRIVATE'), ValueError('PRIVATE')]:
            confirm = self.confirm(error=error)
            with patch.object(receipt_audit, '_emit') as emit:
                result = await confirm.auto_confirm('order', 'item')
            self.assertEqual(result['assessment'], 'unknown')
            self.assertIn('勿重复完整发货', result['error'])
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertEqual(self.session.post.call_count, 1)
            self.assertNotIn('PRIVATE', json.dumps(emit.call_args.args[0]))

    async def test_cancellation_propagates_without_retry(self):
        confirm = self.confirm(error=asyncio.CancelledError())
        with patch.object(receipt_audit, '_emit') as emit, self.assertRaises(asyncio.CancelledError):
            await confirm.auto_confirm('order', 'item')
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(emit.call_args.args[0]['error_type'], 'CancelledError')

    def test_safe_codes_not_private_platform_text_are_used(self):
        for code in ['PERMISSION_ERROR', 'FAIL_SYS_SESSION_EXPIRED', 'FAIL_SYS_TOKEN_EXOIRED', 'FAIL_BIZ_ORDER_CLOSED']:
            result = shipping_response_result({'ret': [code+'::cookie=PRIVATE card=SECRET']}, 200)
            self.assertFalse(result['success'])
            self.assertEqual(result['assessment'], 'rejected')
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertNotIn('SECRET', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
