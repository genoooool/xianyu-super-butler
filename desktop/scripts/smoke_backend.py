#!/usr/bin/env python3
"""Start the packaged backend and verify its loopback health endpoint."""

from __future__ import annotations

import argparse
import http.cookiejar
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

import psutil


def verify_runtime_lifecycle(backend: Path, env: dict, work_dir: Path, abrupt: bool = True) -> None:
    """Exercise frozen helpers and launcher exit without loading accounts."""
    def is_alive(process):
        try:
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    mode = 'abrupt' if abrupt else 'graceful'
    log_path = work_dir / f'runtime-probe-{mode}.log'
    report_path = work_dir / f'runtime-probe-ready-{mode}.json'
    probe_env = dict(env, XIANYU_RUNTIME_PROBE_REPORT=str(report_path))
    tracked = {}
    with log_path.open('w', encoding='utf-8') as output:
        process = subprocess.Popen([str(backend), '--desktop-runtime-probe'], cwd=work_dir,
                                   env=probe_env, stdout=output, stderr=subprocess.STDOUT)
        root = psutil.Process(process.pid)
        try:
            deadline = time.monotonic() + 40
            ready_at = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Packaged runtime probe exited before shutdown check')
                children = root.children(recursive=True)
                for child in children:
                    tracked[(child.pid, child.create_time())] = child
                if len(children) > 24:
                    raise RuntimeError('Packaged runtime recursively spawned too many children')
                if report_path.is_file():
                    ready_at = ready_at or time.monotonic()
                    if time.monotonic() - ready_at >= 3:
                        break
                time.sleep(0.2)
            else:
                raise RuntimeError('Packaged worker/browser probe did not become ready')
            # Both normal SIGTERM forwarding and an abruptly lost bootloader
            # must clean up the backend, worker, resource tracker and browser.
            if abrupt:
                process.kill()
            else:
                process.terminate()
            process.wait(timeout=12)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and any(is_alive(p) for p in tracked.values()):
                time.sleep(0.2)
            if any(is_alive(p) for p in tracked.values()):
                raise RuntimeError('Packaged backend left live descendants after launcher exit')
            print(f'Packaged spawn worker, blank browser and {mode} cleanup checks passed', flush=True)
        except Exception:
            print(log_path.read_text(encoding='utf-8', errors='replace')[-6000:], file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                for child in root.children(recursive=True):
                    tracked[(child.pid, child.create_time())] = child
                process.kill()
            for child in tracked.values():
                if is_alive(child):
                    child.kill()
            process.wait(timeout=5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--backend", help="Test the final signed sidecar inside an application bundle")
    parser.add_argument("--playwright-dir", required=True)
    return parser.parse_args()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stop_backend(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # A PyInstaller one-file executable owns a second backend process.
        # Terminating only the bootloader leaves that child (and its handles)
        # alive on Windows.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def verify_desktop_bootstrap(base_url: str, token: str) -> None:
    try:
        with urllib.request.urlopen(base_url + "/", timeout=3):
            raise RuntimeError("Desktop frontend was accessible without its session cookie")
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    bootstrap_url = base_url + "/desktop/bootstrap?" + urllib.parse.urlencode({"token": token})
    with opener.open(bootstrap_url, timeout=3) as response:
        if response.status != 200 or response.url != bootstrap_url:
            raise RuntimeError("Desktop bootstrap must commit a document before navigation")
    with opener.open(base_url + "/", timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("Desktop frontend did not accept the bootstrap cookie")


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[2]
    suffix = ".exe" if os.name == "nt" else ""
    backend = (
        Path(args.backend).resolve()
        if args.backend
        else root / "desktop" / "src-tauri" / "binaries" / f"xianyu-backend-{args.target}{suffix}"
    )
    if not backend.is_file():
        raise FileNotFoundError(backend)

    port = free_port()
    with tempfile.TemporaryDirectory(prefix="xianyu-desktop-smoke-") as temp_dir:
        env = os.environ.copy()
        env.update(
            {
                "XIANYU_DESKTOP": "1",
                "XIANYU_DATA_DIR": temp_dir,
                "XIANYU_DESKTOP_TOKEN": secrets.token_urlsafe(32),
                "PLAYWRIGHT_BROWSERS_PATH": str(Path(args.playwright_dir).resolve()),
                "API_HOST": "127.0.0.1",
                "API_PORT": str(port),
                "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        verify_runtime_lifecycle(backend, env, Path(temp_dir))
        verify_runtime_lifecycle(backend, env, Path(temp_dir), abrupt=False)
        log_path = Path(temp_dir) / "backend-output.log"
        healthy = False
        # A file cannot fill up a pipe or block on EOF held by a child process.
        with log_path.open("w", encoding="utf-8") as output:
            kwargs: dict[str, object] = {
                "cwd": temp_dir,
                "env": env,
                "stdout": output,
                "stderr": subprocess.STDOUT,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen([str(backend)], **kwargs)
            try:
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                            if response.status == 200:
                                verify_desktop_bootstrap(
                                    f"http://127.0.0.1:{port}", env["XIANYU_DESKTOP_TOKEN"]
                                )
                                healthy = True
                                print("Packaged backend health and desktop bootstrap checks passed", flush=True)
                                break
                    except Exception:
                        pass
                    time.sleep(1)
            finally:
                stop_backend(process)

        if healthy:
            return 0
        print("Packaged backend did not become healthy", file=sys.stderr)
        for path in [log_path, *sorted((Path(temp_dir) / "logs").glob("*.log"))]:
            print(f"--- {path.name} ---", file=sys.stderr)
            print("\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
