"""Check notification sound controls against a frozen backend and empty profile."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time

import httpx
from playwright.sync_api import expect, sync_playwright


def run(args):
    work = args.output_dir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    token, password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    base = f'http://127.0.0.1:{port}'
    env = dict(os.environ, XIANYU_DESKTOP='1', XIANYU_DESKTOP_SMOKE='1',
               XIANYU_DATA_DIR=str(work), DB_PATH=str(work / 'data/xianyu_data.db'),
               COOKIES_STR='', ADMIN_PASSWORD=password, XIANYU_DESKTOP_TOKEN=token,
               API_HOST='127.0.0.1', API_PORT=str(port), PYTHONUTF8='1')
    env.pop('XIANYU_RESOURCE_DIR', None)
    with (work / 'backend.log').open('w') as log:
        process = subprocess.Popen([str(args.backend.resolve())], cwd=work, env=env, stdout=log, stderr=log)
        try:
            with httpx.Client(trust_env=False) as client:
                for _ in range(150):
                    assert process.poll() is None, 'Backend exited'
                    try:
                        if client.get(base + '/health').status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.2)
                else:
                    raise AssertionError('Backend readiness timeout')
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=args.browser, headless=True)
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
                    page.goto(base + '/desktop/bootstrap?token=' + token)
                    page.locator('input[type="text"]').fill('admin')
                    page.locator('input[type="password"]').fill(password)
                    page.get_by_role('button', name='登录', exact=True).click()
                    page.get_by_role('button', name='系统设置', exact=True).click()
                    sound = page.get_by_role('switch', name='消息提示音', exact=True)
                    banner = page.get_by_role('switch', name='新消息弹窗', exact=True)
                    expect(sound).to_be_visible()
                    expect(sound).to_have_attribute('aria-checked', 'true')
                    auth = {'Authorization': 'Bearer ' + page.evaluate("localStorage.getItem('auth_token')")}
                    assert page.request.get(base + '/cookies/details', headers=auth).json() == []
                    for wanted in (False, True):
                        sound.click()
                        expect(sound).to_have_attribute('aria-checked', str(wanted).lower())
                        page.reload()
                        expect(sound).to_have_attribute('aria-checked', str(wanted).lower())
                        status = page.request.get(base + '/desktop/notifications/status', headers=auth).json()
                        assert status['sound_available'] and status['preference']['sound'] is wanted
                        # Queue once: repeated test clicks within five seconds
                        # are intentionally refused by the notification hub.
                        if not wanted:
                            page.get_by_role('button', name='测试弹窗', exact=True).click()
                            page.get_by_text('测试提醒已提交；是否弹出取决于系统通知权限和免打扰设置', exact=True).wait_for()
                        batch = page.request.get(base + '/desktop/notifications/poll', headers={'X-Xianyu-Desktop-Token': token}).json()
                        assert batch['test_count'] > 0 and batch['sound'] is wanted, batch
                    page.screenshot(path=str(work / 'sound-settings.png'))
                    banner.click()
                    expect(sound).to_be_disabled()
                    expect(page.get_by_role('button', name='测试弹窗', exact=True)).to_be_disabled()
                    assert not errors, errors
                    report = dict(status='passed', sound_switch_visible=True, sound_on_off_persisted=True,
                                  native_poll_respects_sound=True, disabled_with_banner=True,
                                  javascript_errors=errors, live_accounts=0)
                    (work / 'result.json').write_text(json.dumps(report, indent=2))
                    print(json.dumps(report))
                finally:
                    browser.close()
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    run(parser.parse_args())
