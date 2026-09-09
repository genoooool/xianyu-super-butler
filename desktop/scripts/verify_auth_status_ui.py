"""Offline UI check: distinguish verification, expired login, and connected accounts."""
import argparse
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect


async def run(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=args.static_dir.parent))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    payloads = {
        '/verify': {'authenticated': True, 'user_id': 7, 'is_admin': False},
        '/cookies/details': [dict(id=key, nickname=name, enabled=True, runtime_state=state)
            for key, name, state in [('fixture-a', '待安全验证', 'connecting'),
                                     ('fixture-b', '旧授权已过期', 'need_relogin'),
                                     ('fixture-c', '长期授权正常', 'running')]],
        '/ai-reply-settings': {},
        '/api/risk-control/status': {'accounts': [{'cookie_id': 'fixture-a', 'blocked': True,
            'remaining_seconds': 0, 'verification_required': True, 'verification_type': 'slider',
            'verification_message': '闲鱼要求安全验证'}]},
        '/desktop/notifications/status': {'available': False, 'active': False},
        '/desktop/credentials': {'available': False}, '/system-settings/public': {},
        '/api/captcha/manual-mode': {'native': True},
    }
    calls, errors = [], []
    manual_started, manual_done = asyncio.Event(), asyncio.Event()
    manual_result = {'success': False, 'message': '已取消人工验证', 'session_id': 'fixture-a'}
    async def route(request):
        if not request.request.url.startswith(base + '/'):
            await request.abort()
            return
        path = urlsplit(request.request.url).path
        calls.append((request.request.method, path))
        if path.startswith('/static/'):
            await request.continue_()
        elif path == '/api/captcha/manual-session':
            assert 'attempt_id' in request.request.post_data
            manual_started.set()
            await manual_done.wait()
            await request.fulfill(json=manual_result)
        elif path == '/api/captcha/manual-session/cancel':
            assert 'attempt_id' in request.request.post_data
            manual_done.set()
            await request.fulfill(json={'success': True})
        else:
            await request.fulfill(json=payloads.get(path, {}))
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=args.browser, headless=True)
            try:
                page = await browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.route('**/*', route)
                await page.add_init_script("localStorage.setItem('auth_token','offline-test');localStorage.setItem('active_page','accounts')")
                await page.goto(base + '/static/index.html')
                await expect(page.get_by_text('需重新扫码', exact=True)).to_be_visible()
                await expect(page.get_by_text('监听中', exact=True)).to_be_visible()
                await expect(page.get_by_text('安全验证不代表长期授权已过期', exact=False)).to_be_visible()
                await expect(page.get_by_text('分钟后恢复', exact=False)).to_have_count(0)
                await expect(page.get_by_text('分钟后检查状态', exact=False)).to_have_count(0)
                await expect(page.get_by_text('在我的浏览器打开验证页', exact=False)).to_have_count(0)
                await expect(page.get_by_text('等待人工验证', exact=True)).to_be_visible()
                await page.screenshot(path=str(args.output_dir / 'accounts.png'), animations='disabled')
                assert all(method == 'GET' for method, _ in calls), calls
                assert not any('/qr-login/' in path or '/manual-session' in path for _, path in calls)
                websockets = []
                page.on('websocket', lambda ws: websockets.append(ws.url))
                action = page.get_by_role('button', name='人工滑块验证', exact=True)
                await action.click()
                await asyncio.wait_for(manual_started.wait(), 5)
                await expect(page.get_by_text('在官网窗口直接操作', exact=True)).to_be_visible()
                await expect(page.get_by_alt_text('服务器端验证页面')).to_have_count(0)
                await page.screenshot(path=str(args.output_dir / 'native-verification.png'), animations='disabled')
                await page.get_by_role('button', name='关闭', exact=True).last.click()
                await expect(page.get_by_text('在官网窗口直接操作', exact=True)).to_have_count(0)
                await expect(action).to_be_enabled()
                assert len([path for _, path in calls if path.endswith('/manual-session/cancel')]) == 1
                manual_started.clear()
                manual_done.clear()
                manual_result.update(success=True, message='验证已通过并保存')
                await action.click()
                await asyncio.wait_for(manual_started.wait(), 5)
                manual_done.set()
                await expect(page.get_by_text('在官网窗口直接操作', exact=True)).to_have_count(0)
                await expect(action).to_be_enabled()
                assert not websockets, websockets
                assert not errors, errors
                result = {'status': 'passed', 'no_automatic_login_or_verification': True,
                          'native_manual_no_screenshot_or_websocket': True,
                          'cancel_linked_and_success_closes_modal': True,
                          'distinct_expiry_and_challenge': True, 'javascript_errors': errors}
                (args.output_dir / 'result.json').write_text(json.dumps(result, indent=2))
                print(json.dumps(result))
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--static-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--browser', required=True)
    asyncio.run(run(parser.parse_args()))
