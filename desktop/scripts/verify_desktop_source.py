#!/usr/bin/env python3
"""Fail-fast checks for the desktop packaging contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAURI = ROOT / "desktop" / "src-tauri"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> int:
    required = [
        ROOT / "Start.py",
        ROOT / "app" / "runtime_paths.py",
        ROOT / "app" / "reply_server.py",
        ROOT / "static" / "index.html",
        ROOT / "global_config.yml",
        ROOT / "desktop" / "app-icon.svg",
        ROOT / "desktop" / "ui" / "index.html",
        TAURI / "Cargo.toml",
        TAURI / "src" / "main.rs",
        TAURI / "capabilities" / "default.json",
        TAURI / "binaries" / ".gitkeep",
        TAURI / "resources" / "playwright" / ".gitkeep",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    require(not missing, "Missing desktop packaging files: " + ", ".join(missing))

    config = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
    bundle = config.get("bundle", {})
    require(
        bundle.get("externalBin") == ["binaries/xianyu-backend"],
        "tauri.conf.json must bundle the xianyu-backend sidecar",
    )
    resources = bundle.get("resources", {})
    require(
        resources.get("resources/playwright/") == "playwright/",
        "tauri.conf.json must bundle Playwright Chromium as a resource",
    )
    require(
        bundle.get("windows", {}).get("nsis", {}).get("installMode") == "currentUser",
        "Windows installer must use current-user mode",
    )

    start_source = (ROOT / "Start.py").read_text(encoding="utf-8")
    require("prepare_desktop_working_directory()" in start_source, "Start.py does not prepare the desktop data directory")
    require("default_host = '127.0.0.1'" in start_source, "Desktop backend must default to loopback")

    server_source = (ROOT / "app" / "reply_server.py").read_text(encoding="utf-8")
    require("XIANYU_DESKTOP_TOKEN" in server_source, "Desktop bootstrap token guard is missing")
    require("/desktop/bootstrap" in server_source, "Desktop bootstrap endpoint is missing")

    utility_source = (ROOT / "utils" / "xianyu_utils.py").read_text(encoding="utf-8")
    require("import execjs" not in utility_source, "Unused PyExecJS startup dependency remains")

    print("Desktop source contract verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
