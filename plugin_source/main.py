import os
import time
import sys
import importlib

# Thanks to abdo for this fix
sys.path.append(os.path.join(os.path.dirname(__file__), "dist"))
google_path = os.path.join(os.path.dirname(__file__), "dist", "google")
source = os.path.join(google_path, "__init__.py")
spec = importlib.util.spec_from_file_location(
    "google", source, submodule_search_locations=[]
)
module = importlib.util.module_from_spec(spec)
sys.modules["google"] = module
spec.loader.exec_module(module)

from aqt import mw
from aqt.qt import *

from .menu import *
from .hooks import *

strings_data = mw.addonManager.getConfig(__name__)
if strings_data is not None:
    mw.addonManager.writeConfig(__name__, strings_data)
    strings_data = mw.addonManager.getConfig(__name__)

menu_init()
hooks_init()

# Force update check
aqt.mw.pm.set_last_addon_update_check(int(time.time()) - (60 * 60 * 25))