
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

def on_deck_browser_will_show_options_menu(menu: QMenu, did: int) -> None:
    """Adds a menu item under the gears icon to export a deck's media files."""

    def export_media() -> None:
        config = mw.addonManager.getConfig(__name__)
        field = get_configured_search_field(config)
        exts = get_configured_exts(config)
        exporter = DeckMediaExporter(mw.col, DeckId(did), field, exts)
        note_count = mw.col.decks.card_count([DeckId(did)], include_subdecks=True)
        export_with_progress(mw, exporter, note_count)            
            
    links_menu = QMenu('AnkiCollab', mw)
    menu.addMenu(links_menu)
    action = links_menu.addAction("Export Media to Disk")
    qconnect(action.triggered, export_media)
    

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