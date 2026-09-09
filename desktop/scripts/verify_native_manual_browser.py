"""Exercise the visible browser lifecycle against an entirely intercepted fixture."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from utils import manual_captcha, browser_limit
from utils.captcha_remote_control import captcha_controller


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
                await request.fulfill(content_type='text/html', body='''<!doctype html>
                    <html lang="zh"><meta charset="utf-8"><title>工作台离线窗口检查</title>
                    <body style="font:24px system-ui;padding:64px;background:#fff9db">
                    <h1>专用浏览器 · 离线检查</h1>
                    <p>这是本地测试页面，无需扫码或操作真实账号。</p>
                    <div id="nocaptcha">等待本地测试完成</div>
                    <button onclick="document.querySelector('#nocaptcha').remove()">模拟页面完成</button>
                    </body></html>''')
            await context.route('**/*', route)
            def page_created(page):
                pages.append(page)
                ready.set()
            context.on('page', page_created)
            return context
        browser.new_context = context_factory
        return browser
    async def challenge(*_):
        return 'https://offline.invalid/verification-fixture'
    saved = False
    async def save(result):
        nonlocal saved
        assert browsers[0].is_connected() and not pages[0].is_closed()
        assert result['cookies_str'] == 'unb=offline-fixture'
        saved = True
    with patch.object(browser_limit, 'launch_browser', side_effect=launch), \
         patch.object(manual_captcha, '_fetch_live_verification_url', side_effect=challenge):
        task = asyncio.create_task(manual_captcha.open_manual_session(
            'offline-fixture', 'unb=offline-fixture', headless=False,
            attempt_id='offline-fixture', owner=0, finalize=save))
        try:
            await asyncio.wait_for(ready.wait(), 15)
            page = pages[0]
            await page.get_by_text('等待本地测试完成', exact=True).wait_for()
            # Let the production presence check observe the fixture before the
            # local test button removes it. This is not a real CAPTCHA solver.
            for _ in range(40):
                if 'offline-fixture' in captcha_controller.active_sessions:
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError('Native session was not registered')
            await page.screenshot(path=str(output / 'native-window.png'))
            await page.get_by_role('button', name='模拟页面完成', exact=True).click()
            result = await asyncio.wait_for(task, 15)
            assert result['success'] and saved
            assert not browsers[0].is_connected()
            report = {'status': 'passed', 'headed': True, 'external_requests': 0,
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
