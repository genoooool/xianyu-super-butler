#!/usr/bin/env python3
"""Launch an app in an account-free smoke profile, then verify login readiness and exit.

Canonicalize the executable: macOS Tauri intentionally rejects symlinked launch
paths such as /tmp (which points to /private/tmp). Do not weaken that protection.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

import psutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    binary = (args.app/'Contents/MacOS/xianyu-workbench').resolve(strict=True)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    temp = Path(tempfile.gettempdir())
    previous = set(temp.glob('xianyu-desktop-smoke-*'))
    with (args.output_dir/'native.log').open('w') as log:
        process = subprocess.Popen([str(binary), '--desktop-smoke'], stdout=log, stderr=log)
        owner = psutil.Process(process.pid)
        tracked = {}
        try:
            deadline = time.monotonic()+30
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    raise RuntimeError('Native app exited early; inspect native.log')
                for child in owner.children(recursive=True):
                    tracked[child.pid] = child
                locations = set(temp.glob('xianyu-desktop-smoke-*'))-previous
                if len(locations)>1:
                    raise RuntimeError('Another smoke run is active; cannot identify this profile')
                for location in locations:
                    launcher = location/'logs/desktop-launcher.log'
                    if not launcher.exists():
                        continue
                    lines = launcher.read_text().splitlines()
                    ready = [line for line in lines if 'backend health check passed in' in line]
                    if ready and any('GET /desktop/credentials - 200' in line for line in lines):
                        report = dict(status='passed', native_readiness=ready[-1], login_page_loaded=True,
                                      isolated_runtime=str(location), buyer_messages_sent=0)
                        (args.output_dir/'result.json').write_text(json.dumps(report, indent=2)+'\n')
                        print(json.dumps(report), flush=True)
                        break
                else:
                    time.sleep(.2)
                    continue
                break
            else:
                raise RuntimeError('Native login readiness not observed')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
            _, alive = psutil.wait_procs(list(tracked.values()), timeout=12)
            active = [p for p in alive if p.is_running() and p.status()!=psutil.STATUS_ZOMBIE]
            for child in active:
                child.kill()
            if active:
                raise RuntimeError('Native test left descendants; test failed and they were stopped')


if __name__ == '__main__':
    main()
