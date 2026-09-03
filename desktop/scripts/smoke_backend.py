#!/usr/bin/env python3
"""Start the packaged backend and verify its loopback health endpoint."""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--playwright-dir", required=True)
    return parser.parse_args()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[2]
    suffix = ".exe" if os.name == "nt" else ""
    backend = root / "desktop" / "src-tauri" / "binaries" / f"xianyu-backend-{args.target}{suffix}"
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
        kwargs: dict[str, object] = {
            "cwd": temp_dir,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen([str(backend)], **kwargs)
        output: list[str] = []
        try:
            deadline = time.time() + 150
            while time.time() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                        if response.status == 200:
                            print("Packaged backend health check passed")
                            return 0
                except Exception:
                    pass
                time.sleep(1)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            if process.stdout:
                output.extend(process.stdout.read().splitlines())

        print("Packaged backend did not become healthy", file=sys.stderr)
        print("\n".join(output[-200:]), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
