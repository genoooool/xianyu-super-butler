"""Exercise the visible browser lifecycle against an entirely intercepted fixture."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from utils import manual_captcha, browser_limit
from utils.browser_im_verification import MESSAGE_URL, TOKEN_URL, IM_APP_KEY


async def run(output):
    output.mkdir(exist_ok=True, parents=True)
    ready = asyncio.Event()
    pages = []
    browsers = []
    real_launch = browser_limit.launch_browser
    async def launch(playwright, options, purpose):
        browser = await real_launch(playwright, options, purpose)
        browsers.append(browser)
        new_context = browser.new_context
        async def context_factory(**kwargs):
            context = await new_context(**kwargs)
            async def route(request):
                # Every URL is fulfilled locally. No account or website access.
                if request.request.url == TOKEN_URL:
                    await request.fulfill(json={'ret': ['SUCCESS::调用成功'], 'data': {'accessToken': 'offline-token'}},
                        headers={'Access-Control-Allow-Origin': 'https://www.goofish.com',
                                 'Access-Control-Allow-Credentials': 'true'})
                    return
                assert request.request.url == MESSAGE_URL, 'Unexpected fixture URL'
                await request.fulfill(content_type='text/html', body='''<!doctype html>
                    <html lang="zh"><meta charset="utf-8"><title>工作台离线窗口检查</title>
                    <body style="font:24px system-ui;padding:64px;background:#fff9db">
                    <h1>专用浏览器 · 离线检查</h1>
                    <p>这是本地测试页面，无需扫码或操作真实账号。</p>
                    <div id="nocaptcha">等待本地测试完成</div>
                    <button onclick="document.querySelector('#nocaptcha').remove();history.pushState({},'', '/')">模拟返回首页</button>
                    <button id="auth">模拟官网聊天认证</button>
                    <script>document.querySelector('#auth').onclick=()=>fetch(TOKEN_URL, {
                        method:'POST',credentials:'include',headers:{'Content-Type':'application/x-www-form-urlencoded'},
                        body:new URLSearchParams({data:JSON.stringify({appKey:APP_KEY,deviceId:'offline-device'})})
                    });</script>
                    </body></html>'''.replace('TOKEN_URL', json.dumps(TOKEN_URL)).replace('APP_KEY', json.dumps(IM_APP_KEY)))
            await context.route('**/*', route)
            def page_created(page):
                pages.append(page)
                ready.set()
            context.on('page', page_created)
            return context
        browser.new_context = context_factory
        return browser
    saved = False
    async def save(result):
        nonlocal saved
        assert browsers[0].is_connected() and not pages[0].is_closed()
        assert result['cookies_str'] == 'unb=offline-fixture'
        saved = True
    with patch.object(browser_limit, 'launch_browser', side_effect=launch), \
         patch.object(manual_captcha, '_fetch_live_verification_url', side_effect=AssertionError('No backend challenge request allowed')):
        task = asyncio.create_task(manual_captcha.open_manual_session(
            'offline-fixture', 'unb=offline-fixture', headless=False,
            attempt_id='offline-fixture', owner=0, finalize=save))
        try:
            await asyncio.wait_for(ready.wait(), 15)
            page = pages[0]
            await page.get_by_text('等待本地测试完成', exact=True).wait_for()
            await page.get_by_role('button', name='模拟返回首页', exact=True).click()
            await asyncio.sleep(.3)
            assert not task.done(), 'Homepage navigation is not IM authentication'
            await page.screenshot(path=str(output / 'native-window.png'))
            await page.get_by_role('button', name='模拟官网聊天认证', exact=True).click()
            result = await asyncio.wait_for(task, 15)
            assert result['success'] and saved
            assert not browsers[0].is_connected()
            report = {'status': 'passed', 'headed': True, 'external_requests': 0,
                      'homepage_does_not_complete': True, 'official_im_response_observed': True,
                      'saved_before_close': True, 'owned_browser_closed': True}
            (output / 'result.json').write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
        finally:
            if not task.done():
                task.cancel()
                await task


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output_dir))
