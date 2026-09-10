"""Local Chrome bridge contracts, with all HTTP handled by an offline transport."""
import asyncio
import json
import time
import unittest
from urllib.parse import urlencode

import httpx

from utils.regular_chrome_verification import RegularChromeSession, bridge_receipt
from utils.browser_im_verification import IM_APP_KEY, TOKEN_URL, MESSAGE_URL


def cookies(account='123'):
    return [{'name': 'unb', 'value': account, 'domain': '.goofish.com'},
            {'name': 'cookie2', 'value': 'browser-cookie', 'domain': '.goofish.com'}]


def entry(device='official-123'):
    return dict(url=TOKEN_URL, method='POST', timestamp=time.time()*1000,
                requestHeaders={'Origin': 'https://www.goofish.com'},
                requestBodyPreview=urlencode({'data': json.dumps({'appKey': IM_APP_KEY, 'deviceId': device})}),
                responseStatus=200, responsePreview=json.dumps({'ret':['SUCCESS'], 'data':{'accessToken':'official-token'}}))


class RegularChromeTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self, *, account='123', closed=False):
        commands = []
        def handler(request):
            body = json.loads(request.content)
            commands.append(body)
            data = {}
            result = {'ok': True}
            if body['action'] == 'cookies':
                data = cookies(account)
            elif body['action'] == 'navigate':
                result['page'] = 'owned-tab'
            elif body['action'] == 'network-capture-read':
                if closed:
                    return httpx.Response(200, json={'ok':False, 'errorCode':'page_closed'})
                data = [entry()]
            result['data'] = data
            return httpx.Response(200, json=result)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return RegularChromeSession('known-profile', 'http://127.0.0.1:8080/static/verification-ready.html', client=client), commands

    async def test_same_profile_passive_capture_and_close_only_owned_tab(self):
        bridge, commands = self.bridge()
        try:
            receipt = await bridge.wait('123', 2)
            self.assertEqual(receipt.device_id, 'official-123')
            self.assertEqual(receipt.token, 'official-token')
            self.assertFalse(any(c.get('op') == 'close' for c in commands))
        finally:
            await bridge.close()
        self.assertEqual([c['url'] for c in commands if c['action']=='navigate'], [bridge.prepare_url, MESSAGE_URL])
        self.assertTrue(all(c['contextId']=='known-profile' for c in commands))
        self.assertEqual(commands[-1]['action'], 'tabs')
        self.assertEqual(commands[-1]['op'], 'close')
        self.assertEqual(commands[-1]['page'], 'owned-tab')
        self.assertFalse(any(c['action'] in ('exec','close-window','cdp') for c in commands))

    async def test_wrong_account_never_opens_or_changes_browser(self):
        bridge, commands = self.bridge(account='other')
        try:
            with self.assertRaisesRegex(RuntimeError, '账号不匹配'):
                await bridge.wait('123', 2)
        finally:
            await bridge.close()
        self.assertEqual([c['action'] for c in commands], ['cookies'])

    async def test_closed_tab_never_reopens_or_retries_authentication(self):
        bridge, commands = self.bridge(closed=True)
        try:
            with self.assertRaisesRegex(RuntimeError, '连接中断'):
                await bridge.wait('123', 2)
        finally:
            await bridge.close()
        self.assertEqual(sum(c.get('url')==MESSAGE_URL for c in commands), 1)

    def test_bound_account_fresh_response_and_complete_request_are_required(self):
        self.assertIsNotNone(bridge_receipt(entry(), cookies(), '123', 0))
        self.assertIsNone(bridge_receipt(entry('official-other'), cookies(), '123', 0))
        self.assertIsNone(bridge_receipt(entry(), cookies('other'), '123', 0))
        self.assertIsNone(bridge_receipt(entry(), cookies(), '123', time.time()*1000+10000))
        for patch in ({'url':MESSAGE_URL}, {'responseStatus':403}, {'responseBodyTruncated':True},
                      {'requestHeaders': {'Origin': 'https://example.com'}},
                      {'requestBodyPreview':''}, {'responsePreview':'{"ret":["FAIL_SYS_USER_VALIDATE"]}'}):
            self.assertIsNone(bridge_receipt({**entry(),**patch}, cookies(), '123', 0))

    def test_prepare_page_cannot_redirect_to_a_remote_site(self):
        with self.assertRaises(ValueError):
            RegularChromeSession('known-profile','https://example.com/')
