
from __future__ import annotations
import webbrowser

from datetime import datetime, timedelta, timezone

import aqt
from anki.decks import DeckId
from aqt import gui_hooks, mw
from aqt.operations import QueryOp

from .auth_manager import auth_manager
from .utils import get_deck_hash_from_did
from .crowd_anki.anki.adapters.note_model_file_provider import NoteModelFileProvider
from .crowd_anki.representation.deck import Deck

try:
    from aqt.browser.browser import Browser
except ImportError:
    from aqt.browser import Browser

from aqt.qt import *

from .media_export import DeckMediaExporter, NoteMediaExporter, get_configured_search_field, get_configured_exts, export_with_progress

def on_deck_browser_will_show_options_menu(menu: QMenu, did: int) -> None:
    """Adds a menu item under the gears icon to export a deck's media files."""
    if not auth_manager.is_logged_in():
        return
    
    def export_media() -> None:
        config = mw.addonManager.getConfig(__name__)
        field = get_configured_search_field(config)
        exts = get_configured_exts(config)
        exporter = DeckMediaExporter(mw.col, DeckId(did), field, exts)
        note_count = mw.col.decks.card_count([DeckId(did)], include_subdecks=True)
        export_with_progress(mw, exporter, note_count)            
        
    def download_missing_media() -> None:
        if did is None:
            aqt.utils.tooltip("No valid deck!")
            return
        media_check_output = mw.col.media.check()
        missing_media = media_check_output.missing
        nids = media_check_output.missing_media_notes
        if missing_media is None or len(nids) == 0:
            aqt.utils.tooltip("No media to download")
            return
        deck = Deck(NoteModelFileProvider, mw.col.decks.get(did))
        deck_hash = get_deck_hash_from_did(did)
        if deck_hash is None:
            aqt.utils.tooltip("No valid deck!")
            return
        op = QueryOp(
            parent=mw,
            op=lambda _: deck.process_media_download(deck_hash, missing_media),
            success=deck.on_media_download_done,
        )
        op.with_progress(
            "Downloading missing media..."
        ).run_in_background()
        
    def reset_deck_timestamp():
        deck_hash = get_deck_hash_from_did(did)
        if deck_hash is None:
            aqt.utils.tooltip("No valid deck!")
            return
        strings_data = mw.addonManager.getConfig(__name__)
        if strings_data:
            for sub, details in strings_data.items():
                if sub == deck_hash:
                    details["timestamp"] = (
                        datetime.now(timezone.utc) - timedelta(days=365)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    break
            mw.addonManager.writeConfig(__name__, strings_data)
            
    links_menu = QMenu('AnkiCollab', mw)
    menu.addMenu(links_menu)
    action = links_menu.addAction("Export Media to Disk")
    action2 = links_menu.addAction("Download Missing Media")
    action3 = links_menu.addAction("Reset Deck Timestamp")
    qconnect(action.triggered, export_media)
    qconnect(action2.triggered, download_missing_media)
    qconnect(action3.triggered, reset_deck_timestamp)
        

def add_browser_menu_item(browser: Browser) -> None:
    if not auth_manager.is_logged_in():
        return
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
    