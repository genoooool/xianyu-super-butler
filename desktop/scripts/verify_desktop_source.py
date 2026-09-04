#!/usr/bin/env python3
"""Fail-fast checks for the desktop packaging contract."""

from __future__ import annotations

import json
import ast
import plistlib
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
        TAURI / "Entitlements.plist",
        TAURI / "src" / "main.rs",
        TAURI / "capabilities" / "default.json",
        TAURI / "tauri.macos.conf.json",
        TAURI / "tauri.windows.conf.json",
        TAURI / "binaries" / ".gitkeep",
        TAURI / "resources" / "playwright",
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
    mac_config = json.loads((TAURI / 'tauri.macos.conf.json').read_text(encoding='utf-8'))
    require(mac_config['bundle']['externalBin'] == [] and
            mac_config['bundle']['resources'].get('resources/backend/') == 'backend/',
            'macOS must ship the complete one-directory backend without a redundant onefile sidecar')
    windows_config = json.loads((TAURI / 'tauri.windows.conf.json').read_text(encoding='utf-8'))
    require(windows_config['bundle']['externalBin'] == [] and
            windows_config['bundle']['resources'].get('resources/backend/') == 'backend/',
            'Windows must ship the complete one-directory backend without a redundant onefile sidecar')
    require(
        bundle.get("windows", {}).get("nsis", {}).get("installMode") == "currentUser",
        "Windows installer must use current-user mode",
    )
    require(
        bundle.get("macOS", {}).get("entitlements") == "./Entitlements.plist",
        "macOS signing must preserve the embedded Python library-loading entitlement",
    )
    with (TAURI / "Entitlements.plist").open("rb") as file:
        entitlements = plistlib.load(file)
    require(
        entitlements == {"com.apple.security.cs.disable-library-validation": True},
        "macOS entitlements must be limited to embedded Python library loading",
    )

    start_source = (ROOT / "Start.py").read_text(encoding="utf-8")
    startup = ast.parse(start_source).body[1]
    require(
        isinstance(startup, ast.If)
        and isinstance(startup.body[1], ast.Expr)
        and isinstance(startup.body[1].value, ast.Call)
        and isinstance(startup.body[1].value.func, ast.Attribute)
        and startup.body[1].value.func.attr == "freeze_support",
        "Frozen multiprocessing helpers must dispatch before application imports",
    )
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
