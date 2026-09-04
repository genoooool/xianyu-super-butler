import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.desktop_updates import DesktopUpdates, UpdateGate, UpdateBusy, APP_VERSION
from app.routers.desktop_updates import create_desktop_updates_router


class UpdateTests(unittest.TestCase):
    def setUp(self):
        platform = patch('app.routers.desktop_updates.sys', SimpleNamespace(platform='darwin'))
        platform.start()
        self.addCleanup(platform.stop)
        self.broker = DesktopUpdates()
        self.gate = UpdateGate()
        self.tokens = {'admin': {'is_admin': True}, 'user': {'is_admin': False}}
        app = FastAPI()
        app.include_router(create_desktop_updates_router(self.broker, self.gate, 'native-secret', lambda c: self.tokens.get(c.credentials)))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.admin = {'Authorization': 'Bearer admin'}
        self.native = {'X-Xianyu-Desktop-Token': 'native-secret'}

    def test_windows_only_offers_manual_updates_and_never_queues_install(self):
        with patch('app.routers.desktop_updates.sys', SimpleNamespace(platform='win32')):
            status = self.client.get('/desktop/updates/status', headers=self.admin).json()
            self.assertFalse(status['available'])
            self.assertEqual(status['releases_url'], 'https://github.com/genoooool/xianyu-super-butler/releases')
            for action in ('check', 'install'):
                self.assertEqual(self.action(action, '1.0.2').status_code, 400)
                self.assertIsNone(self.poll())
            self.assertEqual(self.gate.until, 0)

    def action(self, action='check', version=''):
        return self.client.post('/desktop/updates/action', headers=self.admin, json={'action': action, 'version': version})

    def poll(self):
        return self.client.get('/desktop/updates/poll', headers=self.native).json()['command']

    def report(self, command, phase, **fields):
        return self.client.post('/desktop/updates/report', headers=self.native,
            json={'id': command['id'], 'phase': phase, 'version': APP_VERSION, **fields})

    def test_admin_and_native_are_separate_and_cannot_supply_urls(self):
        for headers in ({}, {'Authorization': 'Bearer user'}, self.native):
            self.assertEqual(self.client.post('/desktop/updates/action', headers=headers, json={'action': 'check'}).status_code, 403)
        self.assertEqual(self.client.get('/desktop/updates/poll', headers=self.admin).status_code, 403)
        self.assertEqual(self.client.post('/desktop/updates/action', headers=self.admin, json={'action':'check','url':'https://evil'}).status_code, 422)
        self.assertEqual(self.action('install','1.0.1').status_code, 409)

    def test_version_binding_single_dispatch_and_logout_cancel(self):
        self.assertEqual(self.action().status_code, 200)
        self.assertEqual(self.action().status_code, 409)
        command = self.poll(); self.assertIsNone(self.poll())
        self.assertEqual(self.report(command,'available', latest_version='1.0.1').status_code, 200)
        self.assertEqual(self.action('install','1.0.2').status_code,409)
        self.assertEqual(self.action('install','1.0.1').status_code,200)
        command = self.poll(); self.tokens.clear()
        self.assertEqual(self.client.post('/desktop/updates/prepare',headers=self.native,json={'id':command['id']}).status_code,409)
        self.assertEqual(self.report(command,'installing').status_code,409)

    def test_idle_install_lease_busy_release_and_no_persisted_state(self):
        self.action(); command=self.poll(); self.report(command,'available',latest_version='1.0.1')
        self.action('install','1.0.1'); command=self.poll()
        self.gate.enter()
        self.assertEqual(self.client.post('/desktop/updates/prepare',headers=self.native,json={'id':command['id']}).status_code,409)
        self.gate.leave()
        response=self.client.post('/desktop/updates/prepare',headers=self.native,json={'id':command['id']})
        self.assertEqual(response.json(),{'ready':True,'data_compatibility':1})
        with self.assertRaises(UpdateBusy): self.gate.enter()
        self.client.post('/desktop/updates/release',headers=self.native)
        self.gate.enter(); self.gate.leave()
        self.assertEqual(DesktopUpdates().status()['phase'],'idle')

    def test_expired_shell_request_and_lease_do_not_freeze_business(self):
        now=[10.0]; broker=DesktopUpdates(lambda:now[0]); gate=UpdateGate(lambda:now[0])
        broker.request('check','token'); gate.prepare(); now[0]+=181
        self.assertEqual(broker.status()['phase'],'error')
        self.assertIsNone(broker.command); gate.enter(); gate.leave()

    def test_failed_and_cancelled_operations_release_counts(self):
        async def scenario():
            @self.gate.operation
            async def fail(): raise ValueError('test')
            with self.assertRaises(ValueError): await fail()
            self.assertEqual(self.gate.active,0)
            entered=asyncio.Event()
            @self.gate.operation
            async def slow():
                entered.set(); await asyncio.sleep(60)
            task=asyncio.create_task(slow()); await entered.wait()
            with self.assertRaises(UpdateBusy): self.gate.prepare()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(self.gate.active,0)
            self.gate.prepare(); task=asyncio.create_task(slow()); await asyncio.sleep(.02)
            self.assertEqual(self.gate.active,0)
            self.gate.release(); await asyncio.sleep(.15); self.assertEqual(self.gate.active,1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        asyncio.run(scenario())

    def test_versions_and_app_identity_agree(self):
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        conf=json.loads((root/'desktop/src-tauri/tauri.conf.json').read_text())
        self.assertEqual(conf['version'],APP_VERSION)
        self.assertEqual(conf['identifier'],'com.genoooool.xianyuworkbench')
        self.assertEqual(json.loads((root/'desktop/package.json').read_text())['version'],APP_VERSION)
        self.assertEqual(conf['plugins']['updater']['endpoints'],['https://github.com/genoooool/xianyu-super-butler/releases/latest/download/latest.json'])
        self.assertEqual(json.loads((root/'desktop/src-tauri/capabilities/default.json').read_text())['permissions'],['core:default'])


if __name__ == '__main__': unittest.main()
