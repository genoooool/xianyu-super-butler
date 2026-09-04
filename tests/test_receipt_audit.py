"""Receipt audit records real protocol fields without card bodies or credentials."""
import asyncio
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services import receipt_audit as audit
from app.secure_confirm import SecureConfirm
import test_delivery_receipts as flows


class ReceiptAuditTests(unittest.TestCase):
    def test_real_mid_format_and_top_level_code_are_audited(self):
        with patch.object(audit, '_emit') as emit:
            audit.record_im('shop', '123456789 0', 'response', response={
                'code': 200, 'headers': {'mid': '123456789 0'}, 'body': {'messageId': 'offline.PNM'}})
        doc = emit.call_args.args[0]
        self.assertTrue(doc['matched'])
        self.assertEqual(doc['request_mid'], '123456789 0')
        self.assertEqual(doc['assessment'], 'success_by_current_rule')
        self.assertEqual(audit.im_fields({'code': 500, 'body': {}})['assessment'], 'rejected')
        for malformed in ('secret token', '123\n0', '12 0\n', '12\t0', '1'*200+' 0'):
            self.assertIsNone(audit.request_identifier(malformed))

    def test_success_failure_and_unknown_are_explicit(self):
        cases = [({'headers': {'code': 200}, 'body': {}}, 'success_by_current_rule'),
                 ({'headers': {'code': 403}, 'body': {}}, 'rejected'),
                 ({'headers': {'code': 200}, 'body': {'success': False}}, 'rejected'),
                 ({'headers': {'code': 200}, 'body': {'reason': 'denied'}}, 'rejected'),
                 ({'headers': {'code': 200}, 'body': {'code': 500}}, 'rejected'),
                 ({'body': {}}, 'unknown'), (None, 'unknown'),
                 ({'headers': {'code': 200}, 'body': []}, 'unknown')]
        for response, result in cases:
            with self.subTest(response=response): self.assertEqual(audit.im_fields(response)['assessment'], result)

    def test_only_whitelisted_protocol_values_are_logged(self):
        response = {'headers': {'code': 200, 'mid': 'request-1', 'cookie': 'cookie-secret'},
                    'body': {'code': 403, 'success': False, 'messageId': 'message-2',
                             'reason': 'CARD-SECRET token=TOP-SECRET', 'error': 'password=MY-PASSWORD',
                             'content': 'CARD-SECRET', 'data': {'token': 'NESTED-SECRET'}}}
        with patch.object(audit, '_emit') as emit, audit.receipt_scope('order-1', 'auto_delivery', 2):
            audit.record_im('shop-1', 'request-1', 'response', response=response)
        result = emit.call_args.args[0]
        self.assertEqual(result['order_id'], 'order-1')
        self.assertEqual(result['header_code'], 200); self.assertEqual(result['body_code'], 403)
        self.assertEqual(result['message_id'], 'message-2'); self.assertTrue(result['matched'])
        self.assertTrue(result['reason_present']); self.assertTrue(result['error_present'])
        serialized = json.dumps(result)
        for secret in ('cookie-secret', 'CARD-SECRET', 'TOP-SECRET', 'MY-PASSWORD', 'NESTED-SECRET'):
            self.assertNotIn(secret, serialized)

    def test_unknown_is_not_success_and_never_records_exception_contents(self):
        with patch.object(audit, '_emit') as emit:
            audit.record_im('a', 'request-1', 'unknown', error=TimeoutError('CARD-SECRET'))
        self.assertEqual(emit.call_args.args[0]['assessment'], 'unknown')
        self.assertEqual(emit.call_args.args[0]['error_type'], 'TimeoutError')
        self.assertNotIn('CARD-SECRET', json.dumps(emit.call_args.args[0]))

    def test_unmatched_receipt_is_never_reported_as_success(self):
        with patch.object(audit, '_emit') as emit:
            audit.record_im('a', 'request-1', 'response', response={'headers': {'code': 200, 'mid': 'other'}, 'body': {}})
        self.assertFalse(emit.call_args.args[0]['matched'])
        self.assertEqual(emit.call_args.args[0]['assessment'], 'unknown')

    def test_shipping_ret_is_recorded_separately_without_free_text(self):
        with patch.object(audit, '_emit') as emit:
            audit.record_shipping('a', 'order', 1, http_status=200,
                response={'ret': ['SUCCESS::调用成功'], 'data': {'token': 'SECRET'}})
            audit.record_shipping('a', 'order', 2, http_status=200,
                response={'ret': ['FAIL_SYS_SESSION_EXPIRED::cookie=SECRET']})
            audit.record_shipping('a', 'order', 3, error=TimeoutError('SECRET'))
        results = [call.args[0] for call in emit.call_args_list]
        self.assertEqual([v['assessment'] for v in results], ['success_by_current_rule', 'rejected', 'unknown'])
        self.assertEqual(results[1]['ret_codes'], ['FAIL_SYS_SESSION_EXPIRED'])
        self.assertNotIn('SECRET', json.dumps(results))

    def test_log_sink_failure_does_not_raise_into_delivery(self):
        with patch.object(audit, '_emit', side_effect=OSError('disk full')):
            audit.record_im('a', 'request', 'started')
            audit.record_im('a', 'request', 'response', response={'body': {}})
            audit.record_shipping('a', 'order', 1, error=TimeoutError())

    def test_context_isolated_between_orders_and_reset_after_exit(self):
        async def send(order):
            with audit.receipt_scope(order, 'auto_delivery', 1):
                await asyncio.sleep(0)
                audit.record_im('shop', order, 'started')
        async def run():
            await asyncio.gather(send('order-a'), send('order-b'))
            audit.record_im('shop', 'plain', 'started')
        with patch.object(audit, '_emit') as emit:
            asyncio.run(run())
        docs = [call.args[0] for call in emit.call_args_list]
        self.assertEqual({doc['request_mid']: doc.get('order_id') for doc in docs},
                         {'order-a': 'order-a', 'order-b': 'order-b', 'plain': None})

    def test_context_propagates_to_account_event_loop(self):
        loop = asyncio.new_event_loop(); thread = threading.Thread(target=loop.run_forever)
        thread.start()
        async def record(): audit.record_im('shop', 'request', 'started')
        try:
            with patch.object(audit, '_emit') as emit, audit.receipt_scope('order', 'manual_delivery', 1):
                asyncio.run_coroutine_threadsafe(record(), loop).result(timeout=3)
            self.assertEqual(emit.call_args.args[0]['order_id'], 'order')
            self.assertEqual(emit.call_args.args[0]['flow'], 'manual_delivery')
        finally:
            loop.call_soon_threadsafe(loop.stop); thread.join(timeout=3); loop.close()


class ReceiptAuditIntegrationTests(unittest.IsolatedAsyncioTestCase):
    setUp = flows.ReceiptFlowTests.setUp
    deliver = flows.ReceiptFlowTests.deliver
    automatic = flows.ReceiptFlowTests.automatic
    manual = flows.ReceiptFlowTests.manual
    assert_unshipped = flows.ReceiptFlowTests.assert_unshipped

    async def test_actual_auto_delivery_logs_matching_request_and_order(self):
        with patch.object(audit, '_emit') as emit:
            await self.automatic()
        docs = [call.args[0] for call in emit.call_args_list]
        self.assertEqual([doc['event'] for doc in docs], ['started', 'response'])
        self.assertEqual(docs[1]['request_mid'], self.writes[0]['headers']['mid'])
        self.assertEqual(docs[1]['order_id'], 'order')
        self.assertEqual(docs[1]['flow'], 'auto_delivery'); self.assertEqual(docs[1]['part'], 1)
        self.assertEqual(docs[1]['assessment'], 'success_by_current_rule')
        self.assertNotIn('CARD-1', json.dumps(docs))
        self.assertIn('ship', self.events)

    async def test_actual_manual_delivery_logs_order_and_part(self):
        self.parts = ['CARD-1', '__IMAGE_SEND__https://img.alicdn.com/card.png']
        self.receipts *= 2
        with patch.object(audit, '_emit') as emit:
            result = await self.manual()
        self.assertTrue(result['results'][0]['success'])
        docs = [call.args[0] for call in emit.call_args_list if call.args[0]['event'] == 'response']
        self.assertEqual([v['part'] for v in docs], [1, 2])
        self.assertTrue(all(v['flow'] == 'manual_delivery' and v['order_id'] == 'order' for v in docs))
        self.assertNotIn('card.png', json.dumps(docs))

    async def test_actual_timeout_records_unknown_and_prevents_shipping(self):
        self.receipts = [None]
        with patch.object(audit, '_emit') as emit:
            await self.automatic()
        self.assert_unshipped()
        doc = emit.call_args.args[0]
        self.assertEqual(doc['assessment'], 'unknown'); self.assertEqual(doc['order_id'], 'order')
        self.assertEqual(doc['error_type'], 'TimeoutError')

    async def test_actual_send_keeps_success_if_logging_fails(self):
        with patch.object(audit, '_emit', side_effect=OSError('disk full')):
            await self.automatic()
        self.assertIn('ship', self.events)

    async def test_actual_cancel_records_unknown_without_retry(self):
        async def blocked_write(raw):
            self.written.set()
            await asyncio.Event().wait()
        self.live.ws.send.side_effect = blocked_write
        with patch.object(audit, '_emit') as emit:
            task = asyncio.create_task(self.automatic())
            await self.written.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(emit.call_args.args[0]['error_type'], 'CancelledError')
        self.assert_unshipped()

    async def test_non_send_im_requests_are_not_audited(self):
        with patch.object(audit, '_emit') as emit:
            await self.live._send_im_request('/r/Conversation/listNewestPagination', [1])
        emit.assert_not_called()

    async def test_real_shipping_method_records_http_and_platform_ret(self):
        response = SimpleNamespace(status=200, headers={},
            json=AsyncMock(return_value={'ret': ['SUCCESS::调用成功'], 'data': {'token': 'SECRET'}}))
        manager = AsyncMock(); manager.__aenter__.return_value = response
        confirm = SecureConfirm(SimpleNamespace(post=Mock(return_value=manager)), '_m_h5_tk=offline_123', 'shop')
        with patch.object(audit, '_emit') as emit:
            result = await confirm.auto_confirm('order', 'item')
        self.assertTrue(result['success'])
        self.assertEqual(emit.call_args.args[0]['kind'], 'platform_shipping')
        self.assertEqual(emit.call_args.args[0]['http_status'], 200)
        self.assertEqual(emit.call_args.args[0]['ret_codes'], ['SUCCESS'])
        self.assertNotIn('SECRET', json.dumps(emit.call_args.args[0]))


if __name__ == '__main__':
    unittest.main()
