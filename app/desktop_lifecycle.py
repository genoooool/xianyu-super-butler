"""Keep the frozen desktop backend and its children tied to their launcher.

No application/configuration/database imports are allowed here: this runs
immediately after multiprocessing.freeze_support(), before startup side effects.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
import time

import psutil


def launcher_owners(process: psutil.Process) -> list[psutil.Process]:
    """Track the bootloader and its launcher, retaining PID creation identity."""
    parent = process.parent()
    if parent is None or parent.pid <= 1:
        raise RuntimeError("Desktop backend has no live launcher")
    owners = [parent]
    # PyInstaller onefile has a bootloader parent with the same executable.
    # Also watch its owner so force-quitting the shell cannot orphan both.
    if getattr(sys, "frozen", False) and parent.exe() == process.exe():
        launcher = parent.parent()
        if launcher is None or launcher.pid <= 1:
            raise RuntimeError("Desktop bootloader has no live launcher")
        owners.append(launcher)
    return owners


def is_alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def stop_descendants(process: psutil.Process) -> None:
    """Terminate only this backend's descendants, never processes by name."""
    # A second pass catches children created while cancellation was in flight.
    for _ in range(2):
        try:
            children = process.children(recursive=True)
        except psutil.NoSuchProcess:
            return
        if not children:
            return
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(children, timeout=1)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=1)


def install_desktop_lifecycle() -> bool:
    """Install desktop-only shutdown handlers; leave source/server mode alone."""
    if os.getenv("XIANYU_DESKTOP", "").lower() not in {"1", "true", "yes"}:
        return False

    process = psutil.Process()
    owners = launcher_owners(process)
    shutting_down = threading.Event()

    def on_terminate(signum, frame):
        if not shutting_down.is_set():
            shutting_down.set()
            # asyncio.run cancels active account tasks on SystemExit. Cleanup
            # below runs after normal Python/multiprocessing finalizers.
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_terminate)
    atexit.register(stop_descendants, process)

    def watch_launcher():
        while not shutting_down.wait(0.25):
            if not all(is_alive(owner) for owner in owners):
                if os.name == "nt":
                    import _thread
                    _thread.interrupt_main(signal.SIGTERM)
                else:
                    os.kill(os.getpid(), signal.SIGTERM)
                break
        # Bound shutdown even if a third-party thread blocks interpreter exit.
        time.sleep(5)
        stop_descendants(process)
        os._exit(0)

    threading.Thread(target=watch_launcher, name="desktop-launcher-watch", daemon=True).start()
    return True


def _probe_worker(connection) -> None:
    connection.send("worker-ready")
    connection.close()
    time.sleep(120)


def run_runtime_probe() -> int:
    """Offline packaging check: real spawn worker + blank Chromium, no accounts."""
    import json
    import multiprocessing
    from pathlib import Path
    from playwright.sync_api import sync_playwright

    if os.getenv("XIANYU_DESKTOP") != "1" or not getattr(sys, "frozen", False):
        raise RuntimeError("Runtime probe requires a frozen desktop backend")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_probe_worker, args=(sender,), daemon=True)
    worker.start()
    sender.close()
    if not receiver.poll(15) or receiver.recv() != "worker-ready":
        raise RuntimeError("Frozen multiprocessing worker did not start")
    receiver.close()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium", headless=True)
        browser.new_page().goto("about:blank")
        # A windowed Windows executable may have no stdout even when redirected.
        Path(os.environ['XIANYU_RUNTIME_PROBE_REPORT']).write_text(
            json.dumps({"pid": os.getpid(), "worker_pid": worker.pid}), encoding='utf-8'
        )
        try:
            while True:
                time.sleep(0.25)
        finally:
            browser.close()
