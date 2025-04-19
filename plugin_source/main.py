import os
import time
import sys
import platform
import importlib
import logging

sys.path.append(os.path.join(os.path.dirname(__file__), "dist"))

# Pillow specific stuff
base_path = os.path.dirname(__file__)
arch = platform.machine()

if sys.platform.startswith("win"):
    sys.path.insert(0, os.path.join(base_path, "dist/windows"))
elif sys.platform.startswith("linux"):
    if arch == "x86_64":
        sys.path.insert(0, os.path.join(base_path, "dist/linux/x86_64"))
    elif arch in ("aarch64", "arm64"):  # Some ARM systems report 'arm64'
        sys.path.insert(0, os.path.join(base_path, "dist/linux/aarch64"))
    else:
        raise RuntimeError(f"Unsupported Linux architecture: {arch}")
elif sys.platform.startswith("darwin"):
    if arch == "arm64":
        sys.path.insert(0, os.path.join(base_path, "dist/macos/arm64"))
    elif arch == "x86_64":
        sys.path.insert(0, os.path.join(base_path, "dist/macos/x86_64"))
    else:
        raise RuntimeError(f"Unsupported macOS architecture: {arch}")

from aqt import mw
from aqt.qt import *
from anki.utils import point_version

from .menu import *
from .hooks import *
from .var_defs import API_BASE_URL
from .media_manager import MediaManager

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ankicollab")

logger.info("AnkiCollab Add-on Loading...")

strings_data = mw.addonManager.getConfig(__name__)
if strings_data is not None:
    mw.addonManager.writeConfig(__name__, strings_data)
    strings_data = mw.addonManager.getConfig(__name__)

media_manager = MediaManager(
    api_base_url=API_BASE_URL,
    media_folder=""
)

if point_version() < 50:
    logger.error("Anki version unsupported.")
    raise RuntimeError("AnkiCollab does not run on this version. Please update to a newer version.")

try:
    menu_init()
    logger.info("Menu initialized.")
except Exception as e:
    logger.error(f"Failed to initialize menu: {e}", exc_info=True)

try:
    hooks_init()
    logger.info("Hooks initialized.")
except Exception as e:
    logger.error(f"Failed to initialize hooks: {e}", exc_info=True)

logger.info("AnkiCollab Add-on Loaded Successfully.")

# Force update check
aqt.mw.pm.set_last_addon_update_check(int(time.time()) - (60 * 60 * 25))