
from __future__ import annotations

import aqt
from anki.decks import DeckId
from aqt import gui_hooks, mw

try:
    from aqt.browser.browser import Browser
except ImportError:
    from aqt.browser import Browser

from aqt.qt import *

from .media_export import DeckMediaExporter, MediaExporter, NoteMediaExporter, get_configured_search_field, get_configured_exts, export_with_progress

from .export_manager import get_deck_hash_from_did, get_gdrive_data
from .import_manager import handle_media_import

from .google_drive_api import GoogleDriveAPI

def on_deck_browser_will_show_options_menu(menu: QMenu, did: int) -> None:
    """Adds a menu item under the gears icon to export a deck's media files."""

    def export_media() -> None:
        config = mw.addonManager.getConfig(__name__)
        field = get_configured_search_field(config)
        exts = get_configured_exts(config)
        exporter = DeckMediaExporter(mw.col, DeckId(did), field, exts)
        note_count = mw.col.decks.card_count([DeckId(did)], include_subdecks=True)
        export_with_progress(mw, exporter, note_count)
        
    def gdrive_download_missing() -> None:
        deckHash = get_deck_hash_from_did(did)        
        if deckHash is None:
            aqt.utils.tooltip("AnkiCollab: Deck not found.")
            return
        gdrive_data = get_gdrive_data(deckHash)
        if gdrive_data is not None:
            exporter = DeckMediaExporter(mw.col, DeckId(did))
            api = GoogleDriveAPI(
                service_account=gdrive_data['service_account'],
                folder_id=gdrive_data['folder_id'],
            )
            all_media = exporter.get_list_of_media() # this is filtered in the handle_media function to only download missing media
            handle_media_import(all_media, api)
        else:
            aqt.utils.tooltip("No Google Drive folder set for this deck.")

    action = menu.addAction("Export Media")
    action2 = menu.addAction("AnkiCollab: Download Missing Media")
    qconnect(action.triggered, export_media)
    qconnect(action2.triggered, gdrive_download_missing)
    

def add_browser_menu_item(browser: Browser) -> None:
    def export_selected() -> None:
        config = mw.addonManager.getConfig(__name__)
        field = get_configured_search_field(config)
        exts = get_configured_exts(config)
        selected_notes = [mw.col.get_note(nid) for nid in browser.selected_notes()]
        exporter = NoteMediaExporter(mw.col, selected_notes, field, exts)
        note_count = len(selected_notes)
        export_with_progress(browser, exporter, note_count)

    action = QAction("Export Media", browser)
    qconnect(action.triggered, export_selected)
    browser.form.menu_Notes.addAction(action)