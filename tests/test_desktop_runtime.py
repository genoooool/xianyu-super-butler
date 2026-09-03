import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class DesktopRuntimePathTests(unittest.TestCase):
    def _reload_runtime_paths(self, data_dir: str):
        with mock.patch.dict(os.environ, {"XIANYU_DATA_DIR": data_dir}, clear=False):
            sys.modules.pop("app.runtime_paths", None)
            return importlib.import_module("app.runtime_paths")

    def test_runtime_layout_is_created_in_explicit_data_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._reload_runtime_paths(temp_dir)
            paths.ensure_runtime_layout()
            self.assertEqual(Path(temp_dir).resolve(), paths.DATA_ROOT)
            self.assertTrue((Path(temp_dir) / "data").is_dir())
            self.assertTrue((Path(temp_dir) / "logs").is_dir())
            self.assertTrue((Path(temp_dir) / "static" / "uploads" / "images").is_dir())
            self.assertTrue((Path(temp_dir) / "global_config.yml").is_file())

    def test_desktop_working_directory_switches_to_data_directory(self):
        original_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    with mock.patch.dict(
                        os.environ,
                        {"XIANYU_DATA_DIR": temp_dir, "XIANYU_DESKTOP": "1"},
                        clear=False,
                    ):
                        sys.modules.pop("app.runtime_paths", None)
                        paths = importlib.import_module("app.runtime_paths")
                        paths.prepare_desktop_working_directory()
                        self.assertEqual(Path(temp_dir).resolve(), Path.cwd())
                finally:
                    # Windows cannot remove the process's current directory.
                    os.chdir(original_cwd)
        finally:
            sys.modules.pop("app.runtime_paths", None)


class ChromiumPathTests(unittest.TestCase):
    def test_finds_legacy_macos_playwright_chromium_layout(self):
        from utils import xianyu_slider_stealth as slider

        with tempfile.TemporaryDirectory() as temp_dir:
            executable = (
                Path(temp_dir)
                / "chromium-1234"
                / "chrome-mac"
                / "Chromium.app"
                / "Contents"
                / "MacOS"
                / "Chromium"
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"binary")
            with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": temp_dir}, clear=False):
                self.assertEqual(str(executable), slider.find_chromium_executable())


class PackagedBackendSmokeTests(unittest.TestCase):
    def test_windows_cleanup_terminates_the_entire_sidecar_tree(self):
        from desktop.scripts import smoke_backend

        process = mock.Mock(pid=1234)
        with mock.patch.object(smoke_backend.os, "name", "nt"), mock.patch.object(
            smoke_backend.subprocess, "run"
        ) as run:
            smoke_backend.stop_backend(process)

        self.assertEqual(["taskkill", "/PID", "1234", "/T", "/F"], run.call_args.args[0])
        self.assertEqual(20, run.call_args.kwargs["timeout"])
        process.wait.assert_called_once_with(timeout=10)
        process.terminate.assert_not_called()

    def test_healthy_backend_logs_to_file_without_waiting_for_pipe_eof(self):
        from desktop.scripts import smoke_backend

        process = mock.Mock()
        process.poll.return_value = None
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(smoke_backend, "parse_args", return_value=mock.Mock(
            target="test-target", playwright_dir="."
        )), mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(
            smoke_backend.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(
            smoke_backend.urllib.request, "urlopen", return_value=response
        ), mock.patch.object(smoke_backend, "stop_backend") as stop:
            self.assertEqual(0, smoke_backend.main())

        output = popen.call_args.kwargs["stdout"]
        self.assertNotEqual(smoke_backend.subprocess.PIPE, output)
        self.assertTrue(output.closed)
        process.stdout.read.assert_not_called()
        stop.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
