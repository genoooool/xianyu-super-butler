import importlib
import ast
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
            target="test-target", playwright_dir=".", backend="signed-app-backend"
        )), mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(
            smoke_backend.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(
            smoke_backend.urllib.request, "urlopen", return_value=response
        ), mock.patch.object(smoke_backend, "stop_backend") as stop, mock.patch.object(
            smoke_backend, "verify_desktop_bootstrap"
        ) as bootstrap:
            with mock.patch.object(smoke_backend, 'verify_runtime_lifecycle') as lifecycle:
                self.assertEqual(0, smoke_backend.main())
                self.assertEqual(2, lifecycle.call_count)

        output = popen.call_args.kwargs["stdout"]
        self.assertEqual([str(Path("signed-app-backend").resolve())], popen.call_args.args[0])
        self.assertNotEqual(smoke_backend.subprocess.PIPE, output)
        self.assertTrue(output.closed)
        process.stdout.read.assert_not_called()
        stop.assert_called_once_with(process)
        bootstrap.assert_called_once()


class DesktopBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Load only this pure handler; importing reply_server initializes
        # legacy databases and account managers that are unrelated here.
        import secrets
        from fastapi import HTTPException, Query
        from fastapi.responses import HTMLResponse

        source = Path(__file__).resolve().parents[1] / "app" / "reply_server.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        handler = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == "desktop_bootstrap")
        handler.decorator_list = []
        namespace = {
            "DESKTOP_ACCESS_TOKEN": "test-launch-secret",
            "DESKTOP_ACCESS_COOKIE": "xianyu_desktop_access",
            "secrets": secrets, "HTTPException": HTTPException,
            "Query": Query, "HTMLResponse": HTMLResponse,
        }
        exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), "exec"), namespace)
        self.handler = namespace["desktop_bootstrap"]
        self.http_error = HTTPException

    async def test_bootstrap_commits_a_document_and_keeps_strict_httponly_cookie(self):
        response = await self.handler("test-launch-secret")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("location", response.headers)
        self.assertIn('window.location.replace("/")', response.body.decode())
        self.assertNotIn(b"test-launch-secret", response.body)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual("no-referrer", response.headers["referrer-policy"])

    async def test_bootstrap_rejects_invalid_token(self):
        with self.assertRaises(self.http_error) as raised:
            await self.handler("wrong-token")
        self.assertEqual(403, raised.exception.status_code)


if __name__ == "__main__":
    unittest.main()
