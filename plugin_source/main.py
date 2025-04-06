import os
import time
import sys
import importlib
import logging

sys.path.append(os.path.join(os.path.dirname(__file__), "dist"))

# Pillow specific stuff
base_path = os.path.dirname(__file__)
if sys.platform.startswith("win"):
    sys.path.insert(0, os.path.join(base_path, "dist/windows"))
elif sys.platform.startswith("linux"):
    sys.path.insert(0, os.path.join(base_path, "dist/linux"))
elif sys.platform.startswith("darwin"):  # macOS
    sys.path.insert(0, os.path.join(base_path, "dist/macos"))

from aqt import mw
from aqt.qt import *

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