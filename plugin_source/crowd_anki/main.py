import os
import sys

from aqt import mw, QAction, QFileDialog

from .anki.hook_vendor import HookVendor
from .anki.ui.action_vendor import ActionVendor
from .config.config_dialog import ConfigDialog
from .config.config_settings import ConfigSettings