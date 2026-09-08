"""Offline QR modal cancellation and late-response checks against built assets."""
import argparse
import asyncio
import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

import qrcode
from playwright.async_api import async_playwright, expect


async def run(args):
    args.output_dir.mkdir(parents=True, exist_ok=False)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=args.static_dir.parent))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    picture = BytesIO()
    qrcode.make('offline-qr-fixture').save(picture, format='PNG')
    qr = 'data:image/png;base64,' + base64.b64encode(picture.getvalue()).decode()
    generated, cancelled, errors = [], [], []
    qr_ready = True
    delayed = asyncio.Event()
    payloads = {
        '/verify': {'authenticated': True, 'user_id': 7, 'is_admin': False},
        '/cookies/details': [], '/ai-reply-settings': {},
        '/api/risk-control/status': {'blocked_accounts': []},
        '/desktop/notifications/status': {'available': False, 'active': False},
        '/desktop/credentials': {'available': False}, '/system-settings/public': {},
    }
    async def route(request):
        url = request.request.url
        if not url.startswith(base + '/'):
            await request.abort()
            return
        path = urlsplit(url).path
        if path == '/qr-login/generate':
            sid = str(len(generated) + 1)
            generated.append(sid)
            if sid == '3':
                await delayed.wait()
            await request.fulfill(json={'success': True, 'session_id': sid, 'status': 'loading', 'message': '正在打开官网'})
        elif path.startswith('/qr-login/check/'):
            await request.fulfill(json={'status': 'waiting' if qr_ready else 'loading',
                                        'qr_code_url': qr if qr_ready else None})
        elif path.startswith('/qr-login/cancel/'):
            assert request.request.method == 'POST'
            cancelled.append(path.rsplit('/', 1)[1])
            await request.fulfill(json={'success': True, 'status': 'cancelled'})
        elif path.startswith('/static/'):
            await request.continue_()
        else:
            assert request.request.method == 'GET', path
            await request.fulfill(json=payloads.get(path, {}))
    async def observed(condition):
        for _ in range(100):
            if condition():
                return
            await asyncio.sleep(0.02)
        raise AssertionError('Expected UI request not observed')
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=args.browser, headless=True)
            try:
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.route('**/*', route)
                await page.add_init_script("localStorage.setItem('auth_token','offline-test');localStorage.setItem('active_page','accounts')")
                await page.goto(base + '/static/index.html')
                trigger = page.get_by_role('button', name='扫码添加账号', exact=True).first
                close = page.get_by_role('button', name='关闭扫码登录', exact=True)
                await trigger.click()
                await expect(page.get_by_alt_text('闲鱼登录二维码')).to_be_visible()
                await page.locator('.modal-container').screenshot(path=str(args.output_dir / 'qr-modal.png'))
                await close.click()
                await observed(lambda: '1' in cancelled)
                await expect(close).to_have_count(0)

                qr_ready = False
                await trigger.click()
                await observed(lambda: '2' in generated)
                await close.click()
                await observed(lambda: '2' in cancelled)

                # Close before generate returns, then reopen. The old response
                # must close only its own session without reviving the old UI.
                await trigger.click()
                await observed(lambda: '3' in generated)
                await close.click()
                qr_ready = True
                await trigger.click()
                await expect(page.get_by_alt_text('闲鱼登录二维码')).to_be_visible()
                delayed.set()
                await observed(lambda: '3' in cancelled)
                await expect(close).to_be_visible()
                assert '4' not in cancelled
                await close.click()
                await observed(lambda: '4' in cancelled)
                assert not errors, errors
                report = {'status': 'passed', 'generated': generated, 'cancelled': cancelled,
                          'loading_close': True, 'late_response_isolated': True,
                          'javascript_errors': errors, 'business_mutations': 0}
                (args.output_dir / 'result.json').write_text(json.dumps(report, indent=2))
                print(json.dumps(report))
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--static-dir', type=Path, required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
