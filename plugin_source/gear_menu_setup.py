
from __future__ import annotations
import webbrowser

import aqt
from anki.decks import DeckId
from aqt import gui_hooks, mw

try:
    from aqt.browser.browser import Browser
except ImportError:
    from aqt.browser import Browser

from aqt.qt import *

from .media_export import DeckMediaExporter, NoteMediaExporter, get_configured_search_field, get_configured_exts, export_with_progress

from .export_manager import get_deck_hash_from_did, get_gdrive_data, upload_media_with_progress
from .import_manager import handle_media_import

from .google_drive_api import GoogleDriveAPI

def on_deck_browser_will_show_options_menu(menu: QMenu, did: int) -> None:
    """Adds a menu item under the gears icon to export a deck's media files."""

    def get_gdrive():
        deckHash = get_deck_hash_from_did(did)        
        if deckHash is None:
            aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("AnkiCollab: Deck not found.", parent=QApplication.focusWidget()))
            return None
        return get_gdrive_data(deckHash)

    def export_media() -> None:
        config = mw.addonManager.getConfig(__name__)
        field = get_configured_search_field(config)
        exts = get_configured_exts(config)
        exporter = DeckMediaExporter(mw.col, DeckId(did), field, exts)
        note_count = mw.col.decks.card_count([DeckId(did)], include_subdecks=True)
        export_with_progress(mw, exporter, note_count)
        
    def gdrive_upload_missing() -> None:
        gdrive_data = get_gdrive()
        if gdrive_data is not None:
            exporter = DeckMediaExporter(mw.col, DeckId(did))
            all_media = exporter.get_list_of_media() # this is filtered later
            upload_media_with_progress(deckHash, all_media)
        else:
            aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("No Google Drive folder set for this deck.", parent=QApplication.focusWidget()))    
        
    def gdrive_download_missing() -> None:
        gdrive_data = get_gdrive()
        if gdrive_data is not None:
            exporter = DeckMediaExporter(mw.col, DeckId(did))
            api = GoogleDriveAPI(
                service_account=gdrive_data['service_account'],
                folder_id=gdrive_data['folder_id'],
            )
            all_media = exporter.get_list_of_media() # this is filtered in the handle_media function to only download missing media
            handle_media_import(all_media, api)
        else:
            aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("No Google Drive folder set for this deck.", parent=QApplication.focusWidget()))

    def open_gdrive_folder() -> None:
        gdrive_data = get_gdrive()
        if gdrive_data is not None:
            webbrowser.open(f"https://drive.google.com/drive/u/1/folders/{gdrive_data['folder_id']}")
        else:
            aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("No Google Drive folder set for this deck.", parent=QApplication.focusWidget()))
            
            
    links_menu = QMenu('AnkiCollab', mw)
    menu.addMenu(links_menu)
    action = links_menu.addAction("Export Media to Disk")
    action2 = links_menu.addAction("Download Missing Media")
    action3 = links_menu.addAction("Upload New Media")
    action4 = links_menu.addAction("Open Folder in Browser")
    qconnect(action.triggered, export_media)
    qconnect(action2.triggered, gdrive_download_missing)
    qconnect(action3.triggered, gdrive_upload_missing)
    qconnect(action4.triggered, open_gdrive_folder)
    

def add_browser_menu_item(browser: Browser) -> None:
    def export_selected() -> None:
        config = mw.addonManager.getConfig(__name__)
        field = get_configured_search_field(config)
        exts = get_configured_exts(config)
        selected_notes = [mw.col.get_note(nid) for nid in browser.selected_notes()]
        exporter = NoteMediaExporter(mw.col, selected_notes, field, exts)
        note_count = len(selected_notes)
        export_with_progress(browser, exporter, note_count)

    action = QAction("AnkiCollab: Export Media to Disk", browser)
    qconnect(action.triggered, export_selected)
    browser.form.menu_Notes.addAction(action)