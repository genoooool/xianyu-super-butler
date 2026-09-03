#!/usr/bin/env python3
"""Offline UI acceptance on a fresh database, never an installed seller profile."""

import argparse
import os
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import psutil
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix='notification-ui-', dir=args.output_dir))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    secret = secrets.token_urlsafe(32)
    env = dict(os.environ, XIANYU_DESKTOP='1', XIANYU_DESKTOP_SMOKE='1', XIANYU_DATA_DIR=str(runtime),
               XIANYU_DESKTOP_TOKEN=secret, API_HOST='127.0.0.1', API_PORT=str(port), PYTHONUNBUFFERED='1')
    with (runtime / 'backend.log').open('w') as output:
        process = subprocess.Popen([str(args.backend)], env=env, cwd=runtime, stdout=output, stderr=subprocess.STDOUT)
        owner = psutil.Process(process.pid)
        try:
            started = time.monotonic()
            while time.monotonic() - started < 90:
                if process.poll() is not None:
                    raise RuntimeError('backend exited')
                try:
                    with urllib.request.urlopen(base + '/health', timeout=1) as response:
                        if response.status == 200:
                            break
                except Exception:
                    time.sleep(.2)
            else:
                raise RuntimeError('health timeout')
            print(f'Frozen backend ready: {time.monotonic() - started:.2f}s', flush=True)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.browser, headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                page = context.new_page()
                errors, requested = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requested.append(request.url))
                page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
                # UI persistence fixture only. Smoke backend itself is forbidden
                # from touching the real Keychain; native CRUD is tested separately.
                saved_login = {'value': None, 'writes': 0}
                def credentials(route):
                    method = route.request.method
                    if method == 'POST':
                        saved_login['value'] = route.request.post_data_json
                        saved_login['writes'] += 1
                    elif method == 'DELETE':
                        saved_login['value'] = None
                    route.fulfill(json={'available': True, 'saved': bool(saved_login['value']), **(saved_login['value'] or {})})
                page.route('**/desktop/credentials', credentials)
                page.goto(base + '/desktop/bootstrap?' + urllib.parse.urlencode({'token': secret}))
                remember = page.get_by_role('checkbox', name='记住账号和密码', exact=True)
                expect(remember).not_to_be_checked()
                remember.check()
                page.locator('input[type="text"]').fill('admin')
                page.locator('input[type="password"]').fill('wrong-test-password')
                page.get_by_role('button', name='登录', exact=True).click()
                expect(page.get_by_role('alert')).to_contain_text('用户名或密码错误')
                assert saved_login['writes'] == 0
                page.locator('input[type="text"]').fill('admin')
                page.locator('input[type="password"]').fill('admin123')
                page.get_by_role('button', name='登录', exact=True).click()
                page.get_by_role('heading', name='运营概览', exact=True).wait_for(timeout=20000)
                assert saved_login['writes'] == 1
                assert 'admin123' not in page.evaluate('JSON.stringify(localStorage)')
                page.evaluate("localStorage.removeItem('auth_token')")
                page.reload()
                expect(remember).to_be_checked()
                expect(page.locator('input[type="text"]')).to_have_value('admin')
                expect(page.locator('input[type="password"]')).to_have_value('admin123')
                page.screenshot(path=str(args.output_dir / 'remember-login.png'), animations='disabled')
                remember.click()
                expect(remember).not_to_be_checked()
                assert saved_login['value'] is None
                page.reload()
                expect(remember).not_to_be_checked()
                expect(page.locator('input[type="password"]')).to_have_value('')
                page.locator('input[type="text"]').fill('admin')
                page.locator('input[type="password"]').fill('admin123')
                page.get_by_role('button', name='登录', exact=True).click()
                page.get_by_role('heading', name='运营概览', exact=True).wait_for(timeout=20000)
                assert saved_login['writes'] == 1
                page.get_by_text('有效成交额 (CNY)', exact=True).wait_for(timeout=20000)
                # The signed backend must also contain the newly added knowledge
                # schema/router. This fresh profile has no seller accounts.
                auth = {'Authorization': 'Bearer ' + page.evaluate("localStorage.getItem('auth_token')")}
                assert context.request.get(base + '/ai-knowledge').status == 401
                assert context.request.get(base + '/ai-knowledge', headers=auth).json()['entries'] == []
                fact = dict(scope='shared', topic='使用方法', content='隔离验收资料')
                saved = context.request.post(base + '/ai-knowledge', headers=auth, data=fact)
                assert saved.status == 200, saved.status
                assert context.request.get(base + '/ai-knowledge', headers=auth).json()['entries'][0]['content'] == fact['content']
                document = '# 使用说明\n' + '这是测试资料。\n' * 400
                document_preview = context.request.post(base + '/ai-knowledge/documents/preview?filename=guide.md',
                                                        headers={**auth, 'Content-Type': 'application/octet-stream'}, data=document.encode())
                assert document_preview.status == 200
                assert len(document_preview.json()['chunks']) > 1
                assert len(context.request.get(base + '/ai-knowledge', headers=auth).json()['entries']) == 1
                imported = dict(scope='shared', topic='导入说明', content=document)
                assert context.request.post(base + '/ai-knowledge/documents/import', headers=auth, data=imported).status == 200
                assert context.request.post(base + '/ai-knowledge/documents/import', headers=auth, data=imported).status == 409
                assert context.request.post(base + '/ai-knowledge/preview', headers=auth,
                                            data=dict(cookie_id='nonexistent', message='使用方法')).status == 404
                assert context.request.get(base + '/ai-knowledge/nonexistent', headers=auth).status == 404
                page.wait_for_function("fetch('/desktop/notifications/status', {headers:{Authorization:'Bearer '+localStorage.getItem('auth_token')}}).then(r=>r.json()).then(s=>s.active)")
                for chunk in ['Settings-', 'AIReply-', 'ItemList-', 'MessageManagement-', 'AccountList-']:
                    assert not any('/assets/' in url and chunk in url for url in requested), chunk
                assert page.locator('.marquee:visible').count() == 0
                assert '发现新版本' not in page.locator('body').inner_text()
                page.screenshot(path=str(args.output_dir / 'dashboard-lazy.png'), animations='disabled')
                page.get_by_role('button', name='系统设置', exact=True).click()
                toggle = page.get_by_role('switch', name='新消息弹窗', exact=True)
                toggle.wait_for()
                assert toggle.get_attribute('aria-checked') == 'true'
                sound = page.get_by_role('switch', name='消息提示音', exact=True)
                sound.wait_for()
                assert sound.get_attribute('aria-checked') == 'true'
                failing_session = '**/desktop/notifications/session'
                page.route(failing_session, lambda route: route.fulfill(status=503, json={'detail': 'offline test'}))
                sound.click()
                page.get_by_text('提示音设置未保存，请重试', exact=True).wait_for()
                assert sound.get_attribute('aria-checked') == 'true'
                assert page.evaluate("localStorage.getItem('desktop_message_notification_sound')") is None
                page.unroute(failing_session)
                test = page.get_by_role('button', name='测试弹窗', exact=True)
                test.click()
                page.get_by_text('测试提醒已提交；是否弹出取决于系统通知权限和免打扰设置', exact=True).wait_for()
                native = {'X-Xianyu-Desktop-Token': secret}
                batch = context.request.get(base + '/desktop/notifications/poll', headers=native).json()
                assert batch['test_count'] == 1 and batch['count'] == 0 and batch['sound'], batch
                assert context.request.get(base + '/desktop/notifications/poll').status == 403
                assert context.request.get(base + '/desktop/notifications/poll', params={'after': batch['cursor']}, headers=native).json()['test_count'] == 0
                page.screenshot(path=str(args.output_dir / 'notification-settings.png'), animations='disabled')
                sound.click()
                page.wait_for_function("document.querySelector('[aria-label=消息提示音]').getAttribute('aria-checked') === 'false'")
                quiet = context.request.get(base + '/desktop/notifications/poll', headers=native).json()
                assert quiet == {**batch, 'sound': False}, quiet
                page.reload()
                sound.wait_for()
                assert sound.get_attribute('aria-checked') == 'false'
                # Sound preferences persist independently; disabling banners also
                # disables this control and clears already queued notifications.
                toggle.click()
                page.wait_for_function("document.querySelector('[aria-label=新消息弹窗]').getAttribute('aria-checked') === 'false'")
                assert test.is_disabled()
                assert sound.is_disabled()
                disabled = context.request.get(base + '/desktop/notifications/poll', headers=native).json()
                assert disabled['test_count'] == 0 and not disabled['sound']
                page.reload()
                toggle.wait_for()
                assert toggle.get_attribute('aria-checked') == 'false'
                toggle.click()
                page.wait_for_function("document.querySelector('[aria-label=新消息弹窗]').getAttribute('aria-checked') === 'true'")
                assert sound.get_attribute('aria-checked') == 'false'
                sound.click()
                page.wait_for_function("document.querySelector('[aria-label=消息提示音]').getAttribute('aria-checked') === 'true'")
                assert context.request.get(base + '/desktop/notifications/poll', headers=native).json()['sound']
                sound.click()
                page.wait_for_function("document.querySelector('[aria-label=消息提示音]').getAttribute('aria-checked') === 'false'")
                page.get_by_role('button', name='总览', exact=True).click()
                page.get_by_role('heading', name='运营概览', exact=True).wait_for()
                # Logout from another page must revoke the native session too.
                page.get_by_role('button', name='退出登录', exact=True).click()
                page.get_by_role('button', name='登录', exact=True).wait_for()
                logged_out = context.request.get(base + '/desktop/notifications/poll', headers=native).json()
                assert logged_out['count'] == 0 and not logged_out['sound']
                assert not errors, errors
                context.close()
                # A fresh webview has no localStorage (as with a new launch port).
                # Restore the user's quiet preference from the real packaged DB.
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
                page.goto(base + '/desktop/bootstrap?' + urllib.parse.urlencode({'token': secret}))
                assert page.evaluate("localStorage.getItem('desktop_message_notification_sound')") is None
                page.locator('input[type="text"]').fill('admin')
                page.locator('input[type="password"]').fill('admin123')
                page.get_by_role('button', name='登录', exact=True).click()
                page.get_by_role('heading', name='运营概览', exact=True).wait_for(timeout=20000)
                page.wait_for_function("localStorage.getItem('desktop_message_notification_sound') === 'false'")
                page.get_by_role('button', name='系统设置', exact=True).click()
                sound = page.get_by_role('switch', name='消息提示音', exact=True)
                sound.wait_for()
                assert sound.get_attribute('aria-checked') == 'false'
                assert not context.request.get(base + '/desktop/notifications/poll', headers=native).json()['sound']
                assert not errors, errors
                browser.close()
                print('Passed: remembered-login opt-in/failed-login/restore/forget/no-plaintext UI with fixture; packaged document preview/import/collision/auth; initial-page-only chunks; sound toggle/rollback/fresh-webview persistence; native guard; logout; no JS errors', flush=True)
        finally:
            children = owner.children(recursive=True) if process.poll() is None else []
            process.terminate()
            process.wait(timeout=15)
            _, alive = psutil.wait_procs(children, timeout=12)
            assert not [child for child in alive if child.status() != psutil.STATUS_ZOMBIE], 'live descendants'


if __name__ == '__main__':
    main()
