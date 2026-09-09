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
    args.output_dir.mkdir(parents=True, exist_ok=False)
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
            'remaining_seconds': 58, 'verification_type': 'slider',
            'verification_message': '闲鱼要求安全验证'}]},
        '/desktop/notifications/status': {'available': False, 'active': False},
        '/desktop/credentials': {'available': False}, '/system-settings/public': {},
    }
    calls, errors = [], []
    async def route(request):
        if not request.request.url.startswith(base + '/'):
            await request.abort()
            return
        path = urlsplit(request.request.url).path
        calls.append((request.request.method, path))
        if path.startswith('/static/'):
            await request.continue_()
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
                await page.screenshot(path=str(args.output_dir / 'accounts.png'))
                assert all(method == 'GET' for method, _ in calls), calls
                assert not any('/qr-login/' in path or '/manual-session' in path for _, path in calls)
                assert not errors, errors
                result = {'status': 'passed', 'no_automatic_login_or_verification': True,
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
