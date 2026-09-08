"""Exercise QR cancellation in the packaged backend using an empty profile.

Opens only the unauthenticated official QR page; never scans or uses live cookies.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time

import httpx
import psutil


async def run(args):
    work = args.output_dir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    token, password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    env = dict(os.environ, XIANYU_DESKTOP='1', XIANYU_DESKTOP_SMOKE='1',
               XIANYU_DATA_DIR=str(work), DB_PATH=str(work / 'data/xianyu_data.db'),
               COOKIES_STR='', ADMIN_PASSWORD=password, XIANYU_DESKTOP_TOKEN=token,
               PLAYWRIGHT_BROWSERS_PATH=str(args.playwright_dir.resolve()),
               API_HOST='127.0.0.1', API_PORT=str(port), MAX_CONCURRENT_BROWSERS='1',
               PYTHONUTF8='1', PYTHONUNBUFFERED='1')
    env.pop('XIANYU_RESOURCE_DIR', None)
    tracked = {}
    def alive(process):
        try:
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
    with (work / 'backend.log').open('w') as log:
        process = subprocess.Popen([str(args.backend.resolve())], cwd=work, env=env, stdout=log, stderr=log)
        root = psutil.Process(process.pid)
        def browsers():
            found = []
            for child in root.children(recursive=True):
                try:
                    if 'chromium-' in child.exe():
                        tracked[(child.pid, child.create_time())] = child
                        found.append(child)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return found
        async def closed():
            for _ in range(40):
                current = browsers()
                if not current and not any(alive(p) for p in tracked.values()):
                    return
                await asyncio.sleep(.1)
            raise AssertionError('Cancelled QR left an owned Chromium process')
        try:
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=30) as client:
                for _ in range(100):
                    assert process.poll() is None, 'Backend exited before health check'
                    try:
                        if (await client.get('/health')).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.2)
                else:
                    raise AssertionError('Backend did not become ready')
                (await client.get('/desktop/bootstrap', params={'token': token})).raise_for_status()
                login = (await client.post('/login', json={'username': 'admin', 'password': password})).json()
                assert login['success']
                client.headers['Authorization'] = 'Bearer ' + login['token']
                assert (await client.get('/cookies/details')).json() == []
                started = time.monotonic()
                generated = (await client.post('/qr-login/generate')).json()
                assert generated['success'] and generated['status'] == 'loading'
                assert time.monotonic() - started < 3, 'Handle must be available while the browser starts'
                sid = generated['session_id']
                for _ in range(150):
                    state = (await client.get('/qr-login/check/' + sid)).json()
                    browsers()
                    if state['status'] == 'waiting' and state.get('qr_code_url'):
                        break
                    assert state['status'] == 'loading', state.get('status')
                    await asyncio.sleep(.2)
                else:
                    raise AssertionError('Official QR did not load')
                assert tracked, 'No owned browser observed'
                answer = (await client.post('/qr-login/cancel/' + sid)).json()
                assert answer['status'] == 'cancelled'
                await closed()
                assert (await client.post('/qr-login/cancel/' + sid)).json()['success']
                # The single browser slot must be reusable after closing.
                early = (await client.post('/qr-login/generate')).json()
                assert early['success'] and early['status'] == 'loading'
                browsers()
                assert (await client.post('/qr-login/cancel/' + early['session_id'])).json()['success']
                await closed()
                assert (await client.get('/cookies/details')).json() == []
                report = {'status': 'passed', 'official_qr_loaded': True, 'cancel_closes_owned_browser': True,
                          'loading_cancel': True, 'repeated_cancel': True, 'browser_slot_reusable': True,
                          'owned_browser_processes_observed': len(tracked), 'live_accounts': 0, 'phone_scans': 0}
                (work / 'result.json').write_text(json.dumps(report, indent=2))
                print(json.dumps(report))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
            assert not any(alive(p) for p in tracked.values()), 'Test browser remained after backend exit'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--playwright-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
