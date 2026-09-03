# Desktop packaging implementation status

This source tree contains a build-ready Tauri 2 desktop wrapper for the existing
React + FastAPI application.

Implemented:

- Windows x64 NSIS installer workflow;
- macOS Apple Silicon and Intel DMG workflows;
- Python/FastAPI backend packaged as a Tauri sidecar;
- Playwright Chromium bundled as an application resource;
- writable per-user data, logs, database, uploads and browser profiles;
- loopback-only backend with a per-launch bootstrap secret;
- system tray, close-to-tray behaviour and controlled backend shutdown;
- packaged-backend health smoke test and focused regression tests.

Current verification in the preparation environment:

- Python compileall: passed;
- desktop source contract: passed;
- 33 focused desktop, database, delivery, log-sanitization and SKU tests: passed;
- JSON, TOML and GitHub Actions YAML parsing: passed;
- native Tauri, Windows and macOS builds: delegated to the included GitHub
  Actions workflow because installers must be built on their target operating
  systems.

The generated macOS packages use ad-hoc signing and are not notarized. The
Windows installer is not Authenticode-signed. These are suitable for controlled
testing; public distribution requires the respective signing credentials.
