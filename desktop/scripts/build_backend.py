#!/usr/bin/env python3
"""Build the Python backend as a Tauri sidecar for the current native target."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"
TAURI = DESKTOP / "src-tauri"
DIST = DESKTOP / "dist-backend"
BUILD = DESKTOP / "build-backend"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="Rust target triple used by Tauri")
    return parser.parse_args()


def add_data(source: Path, destination: str) -> str:
    separator = ";" if os.name == "nt" else ":"
    return f"{source}{separator}{destination}"


def main() -> int:
    args = parse_args()
    output_name = f"xianyu-backend-{args.target}"
    if os.name == "nt":
        output_name += ".exe"

    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(BUILD, ignore_errors=True)
    DIST.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    (TAURI / "binaries").mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "xianyu-backend",
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
        "--paths",
        str(ROOT),
        "--add-data",
        add_data(ROOT / "static", "static"),
        "--add-data",
        add_data(ROOT / "global_config.yml", "."),
        "--collect-all",
        "playwright",
        "--collect-all",
        "patchright",
        "--collect-all",
        "DrissionPage",
        "--collect-all",
        "blackboxprotobuf",
        "--collect-submodules",
        "app",
        "--collect-submodules",
        "utils",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
        "--hidden-import",
        "backports.tarfile",
    ]
    if os.name == "nt":
        # The Tauri shell owns the user-facing window; prevent an extra console.
        command.append("--noconsole")
    command.append(str(ROOT / "Start.py"))

    print("Building backend sidecar:", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)

    built = DIST / ("xianyu-backend.exe" if os.name == "nt" else "xianyu-backend")
    if not built.is_file():
        raise FileNotFoundError(f"PyInstaller output not found: {built}")

    destination = TAURI / "binaries" / output_name
    shutil.copy2(built, destination)
    destination.chmod(destination.stat().st_mode | 0o111)
    print(f"Sidecar ready: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
