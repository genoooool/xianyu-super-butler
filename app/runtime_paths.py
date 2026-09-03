"""Runtime path helpers shared by source, Docker and desktop builds.

The upstream project historically assumes the current working directory is the
repository root. A desktop installer has two different roots instead:

* bundled resources are read-only and live inside the app bundle;
* databases, browser profiles, logs and user uploads must live in the user's
  application-data directory.

The Tauri launcher supplies ``XIANYU_DATA_DIR`` and the frozen Python backend
uses ``sys._MEIPASS`` for bundled resources. Source and Docker deployments keep
their existing behaviour.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _resource_root() -> Path:
    explicit = os.getenv("XIANYU_RESOURCE_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root).resolve()

    return Path(__file__).resolve().parent.parent


def _data_root() -> Path:
    explicit = os.getenv("XIANYU_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path.cwd().resolve()


RESOURCE_ROOT = _resource_root()
DATA_ROOT = _data_root()
STATIC_ROOT = RESOURCE_ROOT / "static"
DATA_STATIC_ROOT = DATA_ROOT / "static"
UPLOADS_ROOT = DATA_STATIC_ROOT / "uploads"
UPLOAD_IMAGES_ROOT = UPLOADS_ROOT / "images"
LOGS_ROOT = DATA_ROOT / "logs"
BACKUPS_ROOT = DATA_ROOT / "backups"
BROWSER_DATA_ROOT = DATA_ROOT / "browser_data"
CONFIG_PATH = DATA_ROOT / "global_config.yml"
DEFAULT_CONFIG_PATH = RESOURCE_ROOT / "global_config.yml"


def ensure_runtime_layout() -> None:
    """Create writable runtime directories and seed the editable config."""

    for directory in (
        DATA_ROOT,
        DATA_ROOT / "data",
        DATA_STATIC_ROOT,
        UPLOADS_ROOT,
        UPLOAD_IMAGES_ROOT,
        LOGS_ROOT,
        BACKUPS_ROOT,
        BROWSER_DATA_ROOT,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists() and DEFAULT_CONFIG_PATH.exists():
        shutil.copy2(DEFAULT_CONFIG_PATH, CONFIG_PATH)


def prepare_desktop_working_directory() -> None:
    """Move desktop builds into their writable app-data directory.

    Most legacy paths are intentionally relative (``data/``, ``logs/`` and
    ``browser_data/``). Changing the process working directory preserves those
    semantics while preventing writes into Program Files or a macOS app bundle.
    """

    ensure_runtime_layout()
    if os.getenv("XIANYU_DESKTOP", "").lower() in {"1", "true", "yes"}:
        os.chdir(DATA_ROOT)


def writable_keywords_path() -> Path:
    """Return a user-editable keyword file, seeding it when available."""

    target = DATA_ROOT / "回复关键字.txt"
    source = RESOURCE_ROOT / "回复关键字.txt"
    if not target.exists() and source.exists():
        shutil.copy2(source, target)
    return target if target.exists() else source
