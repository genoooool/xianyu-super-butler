import ast
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

from app import desktop_lifecycle as lifecycle


ROOT = Path(__file__).resolve().parents[1]


class DesktopLifecycleTests(unittest.TestCase):
    def test_freeze_support_precedes_every_application_import(self):
        tree = ast.parse((ROOT / 'Start.py').read_text())
        startup = tree.body[1]
        self.assertIsInstance(startup, ast.If)
        self.assertEqual('__main__', startup.test.comparators[0].value)
        self.assertEqual('multiprocessing', startup.body[0].names[0].name)
        self.assertEqual('freeze_support', startup.body[1].value.func.attr)
        self.assertEqual('app.desktop_lifecycle', startup.body[2].module)

    def test_normal_server_mode_installs_nothing(self):
        with mock.patch.dict(os.environ, {'XIANYU_DESKTOP': '0'}), mock.patch.object(
            lifecycle.psutil, 'Process'
        ) as process:
            self.assertFalse(lifecycle.install_desktop_lifecycle())
        process.assert_not_called()

    def test_onefile_watches_bootloader_and_shell(self):
        backend = mock.Mock()
        bootloader = mock.Mock(pid=20)
        shell = mock.Mock(pid=10)
        backend.parent.return_value = bootloader
        backend.exe.return_value = bootloader.exe.return_value = '/app/backend'
        bootloader.parent.return_value = shell
        with mock.patch.object(sys, 'frozen', True, create=True):
            self.assertEqual([bootloader, shell], lifecycle.launcher_owners(backend))

    def test_already_orphaned_backend_refuses_startup(self):
        backend = mock.Mock()
        backend.parent.return_value = mock.Mock(pid=1)
        with self.assertRaises(RuntimeError):
            lifecycle.launcher_owners(backend)

    def test_shutdown_targets_only_verified_descendants(self):
        backend = mock.Mock()
        child, grandchild = mock.Mock(), mock.Mock()
        backend.children.side_effect = [[child, grandchild], []]
        with mock.patch.object(lifecycle.psutil, 'wait_procs', side_effect=[([child], [grandchild]), ([grandchild], [])]):
            lifecycle.stop_descendants(backend)
        child.terminate.assert_called_once()
        grandchild.terminate.assert_called_once()
        child.kill.assert_not_called()
        grandchild.kill.assert_called_once()
        backend.terminate.assert_not_called()
        backend.kill.assert_not_called()

    def test_zombie_owner_counts_as_exited(self):
        owner = mock.Mock()
        owner.status.return_value = psutil.STATUS_ZOMBIE
        self.assertFalse(lifecycle.is_alive(owner))

    def test_parent_exit_reaps_a_real_child_without_touching_peer(self):
        # No business imports, local accounts, browser or network are involved.
        backend_code = (
            'import subprocess,sys,time; '
            'from app.desktop_lifecycle import install_desktop_lifecycle; '
            'install_desktop_lifecycle(); '
            'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"]); '
            'print(child.pid,flush=True); time.sleep(120)'
        )
        owner_code = (
            'import subprocess,sys,time; '
            f'backend=subprocess.Popen([sys.executable,"-c",{backend_code!r}]); '
            'print(backend.pid,flush=True); time.sleep(120)'
        )
        env = dict(os.environ, XIANYU_DESKTOP='1')
        owner = subprocess.Popen([sys.executable, '-u', '-c', owner_code], cwd=ROOT,
                                 env=env, stdout=subprocess.PIPE, text=True)
        peer = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        descendants = []
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                lines = executor.submit(lambda: [owner.stdout.readline(), owner.stdout.readline()])
                try:
                    pids = [int(line.strip()) for line in lines.result(timeout=10)]
                except Exception:
                    for child in psutil.Process(owner.pid).children(recursive=True):
                        child.kill()
                    owner.kill()
                    raise
            descendants = [psutil.Process(pid) for pid in pids]
            owner.kill()
            owner.wait(timeout=5)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and any(lifecycle.is_alive(p) for p in descendants):
                time.sleep(0.1)
            self.assertFalse(any(lifecycle.is_alive(p) for p in descendants))
            self.assertIsNone(peer.poll())
        finally:
            for process in descendants:
                if lifecycle.is_alive(process):
                    process.kill()
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=5)
            owner.stdout.close()
            peer.terminate()
            peer.wait(timeout=5)

    def test_workbench_does_not_mount_or_poll_top_banner(self):
        source = (ROOT / 'frontend' / 'App.tsx').read_text()
        self.assertNotIn('AnnouncementBanner', source)


if __name__ == '__main__':
    unittest.main()
