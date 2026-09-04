import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_manual_reply_resume import Fixture, receipt
from app.services.manual_reply import send_manual_reply_outcome


class ManualFeedbackTests(Fixture):
    def outcome(self, text='测试', images=None):
        manager = AsyncMock()
        manager.__aenter__.return_value = SimpleNamespace(upload_reply_bytes=AsyncMock(return_value='https://img.alicdn.com/test.png'))
        with patch.dict(sys.modules, {'utils.image_uploader': SimpleNamespace(ImageUploader=Mock(return_value=manager))}):
            return asyncio.run(send_manual_reply_outcome(self.instance, self.db, 1, 'chat', 'buyer',
                                                         text, images or [], self.clear))

    def test_all_success_ids_are_returned(self):
        result = self.outcome(images=[self.image])
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['messageIds'], ['manual-1', 'image-1'])
        self.assertEqual(result['data']['status'], 'sent')
        self.assertFalse(result['data']['retryable'])
        self.assertFalse(self.service.control(1, 'a', 'chat')['enabled'])
        self.assertTrue(self.service.control(1, 'b', 'chat')['enabled'])

    def test_only_explicit_zero_part_rejection_can_be_retried(self):
        self.instance.send_im_text.return_value = {'code': 403, 'body': {}}
        result = self.outcome(images=[self.image])
        self.assertFalse(result['success'])
        self.assertEqual(result['data']['status'], 'failed')
        self.assertTrue(result['data']['retryable'])
        self.instance._send_im_request.assert_not_called()

    def test_partial_rejection_does_not_allow_replaying_text(self):
        self.instance._send_im_request.return_value = {'code': 403, 'body': {}}
        result = self.outcome(images=[self.image])
        self.assertEqual(result['data']['parts'], 1)
        self.assertEqual(result['data']['messageIds'], ['manual-1'])
        self.assertEqual(result['data']['status'], 'unconfirmed')
        self.assertFalse(result['data']['retryable'])
        self.instance.send_im_text.assert_awaited_once()

    def test_unknown_conflicting_and_server_errors_never_enable_retry(self):
        for response in (None, {}, {'code': 500, 'body': {}}, {'code': 408, 'body': {}},
                         {'code': 409, 'body': {}}, {'code': 499, 'body': {}},
                         {'code': 403, 'body': {'data': {'code': 200}}},
                         {'code': 200, 'body': {'code': 403}},
                         {'code': 403, 'body': {'messageId': 'delivered'}},
                         {'headers': 'invalid', 'body': {}}, {'body': {}}):
            with self.subTest(response=response):
                self.instance.send_im_text.return_value = response
                result = self.outcome()
                self.assertEqual(result['data']['status'], 'unconfirmed')
                self.assertFalse(result['data']['retryable'])

    def test_network_timeout_is_unknown_and_no_automatic_retry(self):
        self.instance.send_im_text.side_effect = TimeoutError('private diagnostic must not be exposed')
        result = self.outcome()
        self.assertEqual(result['data']['status'], 'unconfirmed')
        self.assertNotIn('private', str(result))
        self.instance.send_im_text.assert_awaited_once()


class SendRouteFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_is_not_lost_at_account_loop_boundary(self):
        from app import reply_server as server
        result = dict(success=False, data=dict(status='failed', retryable=True))
        with patch.object(server, '_get_owned_chat_account'), \
             patch.object(server, '_run_on_account_loop', AsyncMock(return_value=result)), \
             patch.object(server.account_request_dedup, 'invalidate_chat') as invalidate:
            actual = await server.send_chat_message('a', server.ChatSendMessageRequest(cid='chat', to_user_id='buyer', text='test'),
                                                     dict(user_id=1, username='offline'))
        self.assertEqual(actual, result)
        invalidate.assert_called_once_with('a', 'chat')

    async def test_gateway_timeout_also_invalidates_old_history(self):
        from fastapi import HTTPException
        from app import reply_server as server
        with patch.object(server, '_get_owned_chat_account'), \
             patch.object(server, '_run_on_account_loop', AsyncMock(side_effect=HTTPException(504))), \
             patch.object(server.account_request_dedup, 'invalidate_chat') as invalidate:
            with self.assertRaises(HTTPException):
                await server.send_chat_message('a', server.ChatSendMessageRequest(cid='chat', to_user_id='buyer', text='test'), dict(user_id=1))
        invalidate.assert_called_once_with('a', 'chat')


if __name__ == '__main__':
    unittest.main()
