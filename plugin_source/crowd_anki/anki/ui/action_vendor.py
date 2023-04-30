from dataclasses import dataclass, field
from typing import Callable, Any, Optional
from aqt import mw

from ...importer.anki_importer import AnkiJsonImporter
from ...config.config_settings import ConfigSettings


@dataclass
class ActionVendor:
    window: Any
    config: ConfigSettings
    action_supplier: Callable[[str, Any], Any]
    directory_vendor: Callable[[str], Optional[str]]

    def __post_init__(self):
        pass

    def action(self, name, handler):
        action = self.action_supplier(name, self.window)
        action.triggered.connect(handler)
        return action

    def actions(self):
        pass

    def import_action(self):
        return self.action('CrowdAnki: Import from disk',
                           lambda: AnkiJsonImporter.import_deck(self.window.col, self.directory_vendor))

    def github_import(self):
        pass

    def snapshot(self):
        pass

    def _snapshot_and_exit(self):
        pass

    def snapshot_and_exit(self):
        return self.action('CrowdAnki: Snapshot and Exit', self._snapshot_and_exit)
