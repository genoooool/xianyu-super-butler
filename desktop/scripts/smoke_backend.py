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
