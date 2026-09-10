"""Offline proof boundaries for the official message-page response observer."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, Mock

from utils.browser_im_verification import (
    IM_APP_KEY, MESSAGE_URL, TOKEN_URL, read_browser_im_receipt, wait_for_browser_im,
)


class BrowserIMProofTests(unittest.IsolatedAsyncioTestCase):
    def response(self, **changes):
        data = urlencode({'data': json.dumps({'appKey': IM_APP_KEY, 'deviceId': 'website-device'})})
        response = SimpleNamespace(url=TOKEN_URL, status=200,
            request=SimpleNamespace(frame=SimpleNamespace(url=MESSAGE_URL), post_data=data,
                all_headers=AsyncMock(return_value={'cookie': 'unb=123; cookie2=old'})),
            text=AsyncMock(return_value=json.dumps({'ret': ['SUCCESS::调用成功'],
                                                  'data': {'accessToken': 'website-token'}})))
        for key, value in changes.items():
            setattr(response, key, value)
        return response

    def context(self, account='123'):
        return SimpleNamespace(cookies=AsyncMock(return_value=[
            {'name': 'unb', 'value': account, 'domain': '.goofish.com', 'expires': -1},
            {'name': 'cookie2', 'value': 'browser-updated', 'domain': '.goofish.com', 'expires': -1},
            {'name': 'unrelated', 'value': 'excluded', 'domain': '.example.com', 'expires': -1},
        ]))

    async def test_accepts_real_shape_and_browser_cookie_updates_with_device_id(self):
        receipt = await read_browser_im_receipt(self.response(), self.context(), '123')
        self.assertEqual(receipt.token, 'website-token')
        self.assertEqual(receipt.device_id, 'website-device')
        self.assertEqual(receipt.cookies, 'unb=123; cookie2=browser-updated')

    async def test_accepts_jsonp_and_get_requests_without_executing_callback(self):
        response = self.response()
        response.url += '?' + response.request.post_data
        response.request.post_data = None
        response.text.return_value = 'mtopjsonp42(' + response.text.return_value + ');'
        self.assertIsNotNone(await read_browser_im_receipt(response, self.context(), '123'))

    async def test_homepage_other_api_lookalike_origin_and_error_are_not_proof(self):
        for url in (MESSAGE_URL, TOKEN_URL.replace('.com/', '.com.evil.invalid/'),
                    TOKEN_URL.replace('https:', 'http:'), TOKEN_URL.replace('pc.login.token', 'user.page')):
            response = self.response(url=url)
            self.assertIsNone(await read_browser_im_receipt(response, self.context(), '123'))
            response.text.assert_not_awaited()
        self.assertIsNone(await read_browser_im_receipt(self.response(status=403), self.context(), '123'))

    async def test_requires_request_and_current_browser_to_match_expected_account(self):
        response = self.response()
        response.request.all_headers.return_value = {'cookie': 'unb=other'}
        self.assertIsNone(await read_browser_im_receipt(response, self.context(), '123'))
        self.assertIsNone(await read_browser_im_receipt(self.response(), self.context('other'), '123'))
        response = self.response()
        response.request.frame.url = 'https://example.com/'
        self.assertIsNone(await read_browser_im_receipt(response, self.context(), '123'))

    async def test_rejects_challenge_empty_token_and_wrong_client(self):
        for payload in ({'ret': ['FAIL_SYS_USER_VALIDATE'], 'data': {'accessToken': 'not-valid'}},
                        {'ret': ['SUCCESS'], 'data': {}}, {'ret': 'SUCCESS', 'data': {'accessToken': 'bad'}}):
            response = self.response(text=AsyncMock(return_value=json.dumps(payload)))
            self.assertIsNone(await read_browser_im_receipt(response, self.context(), '123'))
        response = self.response()
        response.request.post_data = urlencode({'data': json.dumps({'deviceId': 'device', 'appKey': 'other'})})
        self.assertIsNone(await read_browser_im_receipt(response, self.context(), '123'))

    async def test_waits_for_token_not_navigation_or_captcha_disappearance(self):
        handlers = {}
        page = Mock(goto=AsyncMock(), bring_to_front=AsyncMock(), is_closed=Mock(return_value=False))
        page.on.side_effect = lambda event, callback: handlers.update({event: callback})
        task = asyncio.create_task(wait_for_browser_im(page, self.context(), '123', 1))
        await asyncio.sleep(0)
        page.goto.assert_awaited_once_with(MESSAGE_URL, wait_until='domcontentloaded', timeout=30000)
        handlers['response'](self.response(url='https://www.goofish.com/'))
        handlers['response'](self.response(text=AsyncMock(return_value='{"ret":["FAIL_SYS_USER_VALIDATE"]}')))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        handlers['response'](self.response())
        self.assertEqual((await task).token, 'website-token')
        page.remove_listener.assert_called_once()

    async def test_no_website_authentication_times_out_without_retry(self):
        page = Mock(goto=AsyncMock(), bring_to_front=AsyncMock(), is_closed=Mock(return_value=False))
        with self.assertRaises(TimeoutError):
            await wait_for_browser_im(page, self.context(), '123', .02)
        page.goto.assert_awaited_once()
        page.remove_listener.assert_called_once()
