
from __future__ import annotations
import webbrowser

from datetime import datetime, timedelta, timezone

import aqt
from anki.decks import DeckId
from aqt import gui_hooks, mw
from aqt.operations import QueryOp
import requests

from .export_manager import get_server_missing_media, start_suggest_missing_media

from .auth_manager import auth_manager
from .utils import get_deck_hash_from_did
from .crowd_anki.anki.adapters.note_model_file_provider import NoteModelFileProvider
from .crowd_anki.representation.deck import Deck
from .var_defs import API_BASE_URL

try:
    from aqt.browser.browser import Browser
except ImportError:
    from aqt.browser import Browser

from aqt.qt import *
from aqt.qt import QMenu, QAction, qconnect  # explicit imports for linters/type checkers

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
        media_result = {
                        "success": False, 
                        "downloaded": 0, 
                        "skipped": 0,
                        "deck_hash": deck_hash,
                        "missing_files": missing_media
                    }
        
        deck._start_media_download_from_main_thread(deck_hash, missing_media, media_result)
        
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
                    aqt.utils.tooltip("Deck timestamp reset")
                    break
            mw.addonManager.writeConfig(__name__, strings_data)
            
    def upload_missing_media():
        if did is None:
            aqt.utils.tooltip("No valid deck!")
            return
        deck_hash = get_deck_hash_from_did(did)
        if deck_hash is None:
            aqt.utils.tooltip("No valid deck!")
            return
        op = QueryOp(
            parent=mw,
            op=lambda _: get_server_missing_media(deck_hash),
            success= lambda result: start_suggest_missing_media(result),
        )
        op.with_progress(
            "Checking for missing media..."
        ).run_in_background()

    def create_deck_link() -> None:
        # Only applicable to subscribed decks
        if did is None:
            aqt.utils.tooltip("No valid deck!")
            return
        subscriber_hash = get_deck_hash_from_did(did)
        if subscriber_hash is None:
            aqt.utils.tooltip("This deck is not subscribed to AnkiCollab.")
            return

        # Ask user for target (base) deck hash
        base_hash, ok = QInputDialog.getText(
            mw,
            "Create Deck Link",
            "Enter target base deck hash:",
        )
        if not ok:
            return
        base_hash = (base_hash or "").strip()
        if not base_hash:
            aqt.utils.tooltip("Deck hash is required.")
            return

        token = auth_manager.get_token()
        if not token:
            aqt.utils.showWarning("You're not logged in.")
            return

        def _post_create_link(_: object):
            payload = {
                "subscriber_deck_hash": subscriber_hash,
                "base_deck_hash": base_hash,
                "token": token,
            }
            try:
                resp = requests.post(f"{API_BASE_URL}/CreateDeckLink", json=payload, timeout=10)
                return resp.status_code, resp.text
            except Exception as e:
                return -1, str(e)

        def _on_success(result):
            status, text = result
            if status == 200 and (text or "").strip() == "Success":
                # Persist link on the subscribed deck's details
                strings_data = mw.addonManager.getConfig(__name__) or {}
                details = strings_data.get(subscriber_hash)
                if not isinstance(details, dict):
                    details = {}
                    strings_data[subscriber_hash] = details

                # Migrate legacy single value if present
                legacy = details.get("linked_deck_hash")
                if isinstance(legacy, str) and legacy:
                    details["linked_deck_hashes"] = list({legacy})
                    try:
                        del details["linked_deck_hash"]
                    except Exception:
                        pass

                hashes = details.get("linked_deck_hashes")
                if not isinstance(hashes, list):
                    hashes = []
                # Append new hash if not already present
                if base_hash not in hashes:
                    hashes.append(base_hash)
                details["linked_deck_hashes"] = hashes
                mw.addonManager.writeConfig(__name__, strings_data)
                aqt.utils.showInfo("Deck link added. Configure notetypes before creating any note links!")
            elif status == 403 or (text or "").upper().find("FORBIDDEN") != -1:
                aqt.utils.showWarning("Forbidden: you don't have permission to link these decks.")
            elif status == -1:
                aqt.utils.showWarning(f"Network error while creating link:\n{text}")
            else:
                aqt.utils.showWarning(f"Failed to create deck link (status {status}).\n{text}")

        QueryOp(parent=mw, op=_post_create_link, success=_on_success) \
            .with_progress("Creating deck link...") \
            .run_in_background()
            
    links_menu = QMenu('AnkiCollab', mw)
    menu.addMenu(links_menu)
    action = links_menu.addAction("Export Media to Disk")
    action2 = links_menu.addAction("Download Missing Media")
    action4 = links_menu.addAction("Upload Missing Media")
    action3 = links_menu.addAction("Reset Deck Timestamp")
    action5 = links_menu.addAction("Create Deck Link")
    qconnect(action.triggered, export_media)
    qconnect(action2.triggered, download_missing_media)
    qconnect(action3.triggered, reset_deck_timestamp)
    qconnect(action4.triggered, upload_missing_media)
    qconnect(action5.triggered, create_deck_link)
    # Action is available; it will validate subscription before proceeding
        

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
    