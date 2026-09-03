#!/usr/bin/env python3
"""Build the Python backend as a Tauri sidecar for the current native target."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"
TAURI = DESKTOP / "src-tauri"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="Rust target triple used by Tauri")
    parser.add_argument("--layout", choices=("onedir", "onefile"), default="onedir" if sys.platform == "darwin" else "onefile")
    parser.add_argument("--output-dir", type=Path, help="Fresh build directory; existing paths are never removed")
    parser.add_argument("--stage-dir", type=Path, default=TAURI)
    parser.add_argument("--static-dir", type=Path, default=ROOT / "static")
    return parser.parse_args()


def add_data(source: Path, destination: str) -> str:
    separator = ";" if os.name == "nt" else ":"
    return f"{source}{separator}{destination}"


def main() -> int:
    args = parse_args()
    output_name = f"xianyu-backend-{args.target}"
    if os.name == "nt":
        output_name += ".exe"

    if args.layout == "onedir" and sys.platform != "darwin":
        raise ValueError("onedir packaging is currently verified on macOS only")
    output = args.output_dir
    if output is None:
        output = Path(tempfile.mkdtemp(prefix="backend-build-", dir=DESKTOP))
    else:
        output.mkdir(parents=True, exist_ok=False)
    dist, build = output / "dist", output / "build"
    destination = (args.stage_dir / "resources" / "backend" if args.layout == "onedir"
                   else args.stage_dir / "binaries" / output_name)
    if destination.exists():
        raise FileExistsError(f"Preserve/move the previous build before staging: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--" + args.layout,
        "--name",
        "xianyu-backend",
        "--distpath",
        str(dist),
        "--workpath",
        str(build),
        "--specpath",
        str(build),
        "--paths",
        str(ROOT),
        "--add-data",
        add_data(args.static_dir, "static"),
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
    subprocess.run(command, cwd=ROOT, check=True,
                   env=dict(os.environ, PYINSTALLER_CONFIG_DIR=str(output / "cache")))

    built = dist / ("xianyu-backend.exe" if os.name == "nt" else "xianyu-backend")
    if not built.exists():
        raise FileNotFoundError(f"PyInstaller output not found: {built}")

    if args.layout == "onedir":
        shutil.copytree(built, destination, symlinks=True)
    else:
        shutil.copy2(built, destination)
        destination.chmod(destination.stat().st_mode | 0o111)
    print(f"Sidecar ready: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
