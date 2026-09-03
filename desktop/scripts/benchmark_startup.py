#!/usr/bin/env python3
"""Sequential isolated startup measurement. Preserves logs; never uses seller data."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
import urllib.request

import psutil


def measure(backend, output):
    output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    env = dict(os.environ, XIANYU_DESKTOP='1', XIANYU_DESKTOP_SMOKE='1', XIANYU_DATA_DIR=str(output),
               XIANYU_DESKTOP_TOKEN=secrets.token_urlsafe(32), API_HOST='127.0.0.1', API_PORT=str(port))
    with (output / 'backend.log').open('w') as log:
        start = time.monotonic()
        process = subprocess.Popen([str(backend)], cwd=output, env=env, stdout=log, stderr=log)
        owner = psutil.Process(process.pid)
        tracked = []
        try:
            while time.monotonic() - start < 90:
                if process.poll() is not None: raise RuntimeError('Backend exited before readiness')
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=.5) as response:
                        if response.status == 200: return round(time.monotonic() - start, 3)
                except OSError: pass
                time.sleep(.1)
            raise RuntimeError('Readiness timeout')
        finally:
            if process.poll() is None:
                tracked = owner.children(recursive=True)
                process.terminate()
                try: process.wait(timeout=12)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            _, alive = psutil.wait_procs(tracked, timeout=8)
            for child in alive: child.kill()
            if alive: raise RuntimeError('Backend did not clean its descendants')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    results = {'before': [], 'after': []}
    for run in range(3):
        for name in ('before', 'after'):
            duration = measure(getattr(args, name), args.output_dir / f'{name}-{run + 1}')
            results[name].append(duration)
            print(f'{name} run {run + 1}: {duration:.3f}s', flush=True)
    (args.output_dir / 'results.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__': main()
