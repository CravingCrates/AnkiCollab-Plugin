import sys
from aqt import gui_hooks, mw
from aqt.browser import Browser, SidebarTreeView, SidebarItem, SidebarItemType
from anki.decks import DeckId
from anki.notes import NoteId
from aqt.qt import *
from aqt.qt import QMenu, QModelIndex, QCheckBox, QDialogButtonBox, QApplication, QInputDialog
from anki import hooks
from anki.collection import Collection
from aqt.utils import askUser, showInfo
from aqt.operations import QueryOp

import json
from typing import Sequence, List, Tuple # Import List

from .export_manager import *
from .import_manager import *
from .thread import run_function_in_thread

from .gear_menu_setup import add_browser_menu_item, on_deck_browser_will_show_options_menu
from .dialogs import AddChangelogDialog

from .auth_manager import auth_manager
from .utils import get_logger
import requests

logger = get_logger("ankicollab.hooks")

added_editor_buttons = [] # Keep track of buttons added to editors
added_addcards_widgets = [] # Keep track of widgets added to AddCards

def add_sidebar_context_menu(
    sidebar: SidebarTreeView, menu: QMenu, item: SidebarItem, index: QModelIndex
) -> None:
    if auth_manager.is_logged_in():
        menu.addSeparator()
        menu.addAction("Suggest on AnkiCollab", lambda: suggest_context_handler(item))
        menu.addAction("Add new Changelog", lambda: changelog_context_handler(item))

def suggest_context_handler(item: SidebarItem):
    # Decide if this needs login. If so, add guard:
    if not auth_manager.is_logged_in():
        showInfo("Please log in to suggest changes.")
        return
    if item.item_type == SidebarItemType.DECK:
        selected_deck = DeckId(item.id)
        suggest_subdeck(selected_deck)
    else:
        aqt.utils.tooltip("Please select a deck")

def changelog_context_handler(item: SidebarItem):
    # Redundant check
    if not auth_manager.is_logged_in():
        return
    if item.item_type == SidebarItemType.DECK:
        selected_did = DeckId(item.id)
        hash_val = get_deck_hash_from_did(selected_did)
        if hash_val is None:
            aqt.utils.tooltip("This deck is not published or not linked.")
            return
        dialog = AddChangelogDialog(hash_val, mw)
        dialog.exec()
    else:
        aqt.utils.tooltip("Please select a deck")

def bulk_suggest_handler(browser: Browser, nids: Sequence[NoteId]) -> None:
    if not auth_manager.is_logged_in():
        showInfo("Please log in to suggest notes.", parent=browser)
        return
    if len(nids) < 2:
        showInfo("Please use the regular suggest button for single notes", parent=browser)
        return
    suggest_notes(nids, 9)

def remove_notes(nids: Sequence[NoteId], window=None) -> None:
    if not auth_manager.is_logged_in():
         showInfo("Please log in to remove notes.", parent=window if window is not None else mw)
         return

    if not nids: return # Nothing to remove

    # Check if all notes belong to the *same* published deck
    first_note_did = aqt.mw.col.get_note(nids[0]).cards()[0].did
    deckHash = get_deck_hash_from_did(first_note_did)

    if deckHash is None:
        showInfo("The selected note(s) do not belong to a published AnkiCollab deck.", parent=window if window is not None else mw)
        return

    for nid in nids[1:]: # Check subsequent notes against the first one's deck hash
        note = aqt.mw.col.get_note(nid)
        if not note or not note.cards(): continue # Skip if note or cards are missing
        current_hash = get_deck_hash_from_did(note.cards()[0].did)
        if current_hash != deckHash:
            showInfo("Please only select cards from the same published deck.", parent=window if window is not None else mw)
            return

    guids = get_guids_from_noteids(nids)
    if not guids:
        showInfo("Could not retrieve unique identifiers for the selected notes.", parent=window if window is not None else mw)
        return

    (rationale, commit_text) = get_commit_info(11)
    if rationale is None:
        return # User cancelled

    payload = {
        'remote_deck': deckHash,
        'note_guids': guids,
        'commit_text': commit_text,
        'token': auth_manager.get_token(),
        'force_overwrite': False # not implemented on the backend yet so we pass false welp
    }

    # TODO: Background threading
    try:
        response = requests.post(f"{API_BASE_URL}/requestRemoval", json=payload)
        response.raise_for_status()
        logger.debug(f"Removal request response: {response.text}")
        if askUser(
            f"Successfully requested removal of {len(nids)} note(s) from AnkiCollab.\nDo you want to delete them locally now?",
            parent=window if window is not None else mw,
        ):
            delete_notes(nids)
    except requests.exceptions.RequestException as e:
        showInfo(f"Error requesting note removal: {e}", parent=window if window is not None else mw)
        logger.exception("Removal request failed")
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(e)
        except Exception:
            pass
    except Exception as e:
        showInfo(f"An unexpected error occurred: {e}", parent=window if window is not None else mw)
        logger.exception("Unexpected error during removal request")
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(e)
        except Exception:
            pass


def request_note_removal(browser: Browser, nids: Sequence[NoteId]) -> None:
    # Guard is inside remove_notes
    if len(nids) < 1:
        showInfo("Please select at least one note to remove.", parent=browser)
        return
    remove_notes(nids, browser)

def context_menu_bulk_suggest(browser: Browser, context_menu: QMenu) -> None:
    if not auth_manager.is_logged_in():
        return # Don't add menu items if not logged in

    selected_nids = browser.selected_notes()
    if not selected_nids: return # Don't add if no notes selected

    context_menu.addSeparator()
    context_menu.addAction(
        "AnkiCollab: Bulk suggest notes",
        lambda: bulk_suggest_handler(browser, nids=selected_nids),
    )
    context_menu.addAction(
        "AnkiCollab: Request note removal",
        lambda: request_note_removal(browser, nids=selected_nids),
    )

    # Conditionally add note link creation if deck is subscribed and linked to one or more base decks
    try:
        first_note = aqt.mw.col.get_note(selected_nids[0])
        if first_note and first_note.cards():
            first_did = first_note.cards()[0].did
            subscriber_hash = get_deck_hash_from_did(first_did)
            if subscriber_hash:
                linked_hashes = _get_linked_base_hashes(subscriber_hash)
                if linked_hashes:
                    # Ensure all selected notes are from this same deck
                    same_deck = True
                    for nid in selected_nids[1:]:
                        note = aqt.mw.col.get_note(nid)
                        if not note or not note.cards():
                            continue
                        if get_deck_hash_from_did(note.cards()[0].did) != subscriber_hash:
                            same_deck = False
                            break
                    if same_deck:
                        context_menu.addAction(
                            "AnkiCollab: Create note link(s)",
                            lambda: create_note_links_handler(browser, selected_nids, subscriber_hash),
                        )
    except Exception:
        pass

def create_note_links_handler(browser: Browser, nids: Sequence[NoteId], subscriber_hash: str, base_hash: str | None = None) -> None:
    if not auth_manager.is_logged_in():
        showInfo("Please log in to link notes.", parent=browser)
        return
    if not nids:
        showInfo("Please select at least one note.", parent=browser)
        return

    # Validate all notes belong to the subscriber deck
    for nid in nids:
        note = aqt.mw.col.get_note(nid)
        if not note or not note.cards():
            continue
        if get_deck_hash_from_did(note.cards()[0].did) != subscriber_hash:
            showInfo("Please select notes from the same subscribed deck.", parent=browser)
            return

    # Resolve base deck hash if not provided, allowing multiple linked base decks
    if base_hash is None:
        linked_hashes = _get_linked_base_hashes(subscriber_hash)
        if not linked_hashes:
            showInfo("This subscribed deck has no linked base decks configured.", parent=browser)
            return
        if len(linked_hashes) == 1:
            base_hash = linked_hashes[0]
        else:
            # Ask the user to pick which base deck these notes belong to
            label = (
                "Multiple base decks are linked to this deck.\n\n"
                "Select the base deck that contains ALL of the selected notes to avoid partial linking mistakes.\n"
                "Do not pick a deck if only some of these notes belong to it."
            )
            base_hash, ok = QInputDialog.getItem(
                browser,
                "Select Base Deck for Note Links",
                label,
                linked_hashes,
                0,
                False,
            )
            if not ok or not base_hash:
                return

    guids = get_guids_from_noteids(nids)
    if not guids:
        showInfo("Could not retrieve note GUIDs.", parent=browser)
        return

    token = auth_manager.get_token()
    if not token:
        showInfo("You're not logged in.", parent=browser)
        return

    def _op(_: object):
        payload = {
            "subscriber_deck_hash": subscriber_hash,
            "base_deck_hash": base_hash,
            "note_guids": guids,
            "token": token,
        }
        try:
            resp = requests.post(f"{API_BASE_URL}/CreateNewNoteLink", json=payload, timeout=30)
            return resp.status_code, resp.text
        except Exception as e:
            return -1, str(e)

    def _on_success(result):
        status, text = result
        if status == 200:
            # Expecting a JSON string like: {"linked": <usize>, "skipped": [guid, ...]}
            linked = 0
            skipped = []
            try:
                payload = json.loads(text or "{}")
                linked = int(payload.get("linked", 0))
                skipped = payload.get("skipped", []) or []
            except Exception:
                # Fallback if server returned plain text
                pass

            if linked or skipped:
                if skipped:
                    skipped_list = ", ".join(map(str, skipped))
                    showInfo(
                        f"Linked {linked} note(s). Skipped {len(skipped)} note(s):\n{skipped_list}",
                        parent=browser,
                    )
                    # Offer to show skipped notes in the Browser
                    if askUser("Open skipped notes in Browser?", parent=browser):
                        nids = []
                        for g in skipped:
                            try:
                                nid = get_note_id_from_guid(g)
                                if nid:
                                    nids.append(nid)
                            except Exception:
                                continue
                        if nids:
                            open_browser_with_nids(nids)
                        else:
                            showInfo("Could not find the skipped notes locally.", parent=browser)
                else:
                    showInfo(f"Linked {linked} note(s) successfully.", parent=browser)
                return
            # If we couldn't parse, just show generic success
            showInfo("Note link(s) created.", parent=browser)
        elif status == 403 or (text or "").upper().find("FORBIDDEN") != -1:
            showInfo("Forbidden: you don't have permission to link these notes.", parent=browser)
        elif status == -1:
            showInfo(f"Network error while creating note link(s):\n{text}", parent=browser)
        else:
            showInfo(f"Failed to create note link(s) (status {status}).\n{text}", parent=browser)

    QueryOp(parent=browser, op=_op, success=_on_success) \
        .with_progress("Creating note link(s)...") \
        .run_in_background()

def _get_linked_base_hashes(subscriber_hash: str) -> List[str]:
    """Return list of linked base deck hashes for a subscribed deck.
    Migrates old single-value config (linked_deck_hash) to a list (linked_deck_hashes) on read.
    """
    try:
        strings_data = mw.addonManager.getConfig(__name__) or {}
        details = strings_data.get(subscriber_hash)
        if not isinstance(details, dict):
            return []
        hashes = details.get("linked_deck_hashes")
        if isinstance(hashes, list) and hashes:
            return [str(h) for h in hashes if h]
        # Fallback to migrate from legacy single hash
        legacy = details.get("linked_deck_hash")
        if isinstance(legacy, str) and legacy:
            details["linked_deck_hashes"] = [legacy]
            # Optionally remove legacy key to avoid confusion
            try:
                del details["linked_deck_hash"]
            except Exception:
                pass
            mw.addonManager.writeConfig(__name__, strings_data)
            return [legacy]
        return []
    except Exception:
        return []

def init_editor_card(buttons: List[str], editor):
    # This hook adds a button PERMANENTLY to the editor instance.
    # We need to check login status *at the time the editor opens*.
    # If the user logs in *while* the editor is open, this button won't appear
    # until a new editor window is opened. This is usually acceptable.
    if not auth_manager.is_logged_in():
        return buttons

    # Avoid duplicates in the "Add" Window
    if isinstance(editor.parentWindow, aqt.addcards.AddCards):
         return buttons

    b = editor.addButton(
        None, # icon_path
        "AnkiCollab",
        lambda editor=editor: suggest_notes([editor.note.id], 0, editor=editor),
        tip="Suggest Changes (AnkiCollab)",
        keys=None, # shortcut
        disables=False # disables automatically when no note is selected
    )
    
    buttons.append(b)
    return buttons

def init_add_card(addCardsDialog):
    if not auth_manager.is_logged_in():
        return

    # Avoid adding multiple times if the hook fires unexpectedly often
    if hasattr(addCardsDialog, "ankicollab_suggest_checkbox"):
        return

    checkbox = QCheckBox("Suggest on AnkiCollab")
    addCardsDialog.ankicollab_suggest_checkbox = checkbox # Store reference on the dialog instance

    button_box = None
    if hasattr(addCardsDialog.form, 'buttonBox'):
         button_box = addCardsDialog.form.buttonBox
    else:
         # Fallback search if needed (less robust)
         for widget in addCardsDialog.findChildren(QDialogButtonBox):
             button_box = widget
             break

    if button_box:
        button_box.layout().insertWidget(0, checkbox) # Insert at the beginning
        added_addcards_widgets.append(checkbox) # Store reference if needed later
    else:
        # Fallback: add to the main layout if button box not found
        addCardsDialog.layout().addWidget(checkbox)
        logger.debug("Could not find buttonBox in AddCards dialog, added checkbox to main layout.")


def make_new_card(note: NoteId):
    """Called after a note is added via AddCards."""
    # Check if the checkbox exists and is checked
    if not auth_manager.is_logged_in():
        return # Should not be reachable if checkbox wasn't added, but safe check

    # Access the checkbox via the main window's AddCards instance (if available)
    add_cards_window = getattr(mw, "add_cards_dialog", None) # Assuming you store the ref somewhere, or find it
    if not add_cards_window:
        # Try finding the active AddCards window (less reliable)
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, aqt.addcards.AddCards):
                add_cards_window = widget
                break

    checkbox = getattr(add_cards_window, "ankicollab_suggest_checkbox", None) if add_cards_window else None

    if checkbox and checkbox.isChecked():
        suggest_notes([note.id], 6) # New card rationale


def request_update(silent) -> None:
    if not auth_manager.is_logged_in():
        aqt.utils.tooltip("Log in to AnkiCollab to check for updates.")
        return

    handle_pull(None, silent)

def async_update(silent: bool = False) -> None:
    """Asynchronous update check. This is called when the user clicks the update button."""
    if not auth_manager.is_logged_in():
        aqt.utils.tooltip("Log in to AnkiCollab to check for updates.")
        return

    request_update(silent)

def autoUpdate():
    config = mw.addonManager.getConfig(__name__)
    settings = config.get("settings", {}) if config else {}
    startup_check_enabled = bool(settings.get("pull_on_startup", False))

    if startup_check_enabled:
        # Run the update once the ankiweb sync is done
        if mw.pm.auto_syncing_enabled() and mw.pm.sync_auth() and not mw.safeMode:
        
            def on_sync_finished_hk():
                # run reload_data AFTER sync is finished
                async_update(True)
                
                try:
                    gui_hooks.sync_did_finish.remove(on_sync_finished_hk)
                except ValueError:
                    pass  # Hook wasn't registered, ignore

            gui_hooks.sync_did_finish.append(on_sync_finished_hk)
        else:
            # No auto-sync or not configured for sync, run immediately
            async_update(True)
        

import struct

original_get_image_dimensions_ioe = None
ioe_imghdr = None
def hk_get_image_dimensions(image_path: str) -> Tuple[int, int]:
    global original_get_image_dimensions_ioe
    if image_path.endswith(".webp"):
        height = -1
        width = -1
        with open(image_path, 'rb') as fhandle:
            head = fhandle.read(31)
            size = len(head)
            if size >= 12 and head.startswith(b'RIFF') and head[8:12] == b'WEBP':
                if head[12:16] == b"VP8 ":
                    width, height = struct.unpack("<HH", head[26:30])
                elif head[12:16] == b"VP8X":
                    width = struct.unpack("<I", head[24:27] + b"\0")[0]
                    height = struct.unpack("<I", head[27:30] + b"\0")[0]
                elif head[12:16] == b"VP8L":
                    b = head[21:25]
                    width = (((b[1] & 63) << 8) | b[0]) + 1
                    height = (((b[3] & 15) << 10) | (b[2] << 2) | ((b[1] & 192) >> 6)) + 1
                else:
                    raise ValueError("Unsupported WebP file")
                return width, height
    return original_get_image_dimensions_ioe(image_path)
    
def patch_image_occlusion_enhanced():
    utils_module = "1374772155.utils"
    add_module = "1374772155.add"
    ioe_utils = sys.modules.get(utils_module)
    ioe_add = sys.modules.get(add_module)
    if ioe_utils is None or ioe_add is None:
        logger.warning("Image Occlusion Enhanced Add-on not loaded, skipping patch.")
        return False
    global original_get_image_dimensions_ioe
    original_get_image_dimensions_ioe = ioe_utils.get_image_dimensions
    ioe_utils.get_image_dimensions = hk_get_image_dimensions
    ioe_add.get_image_dimensions = hk_get_image_dimensions
    return True
    
def onProfileLoaded():
    """Called when the Anki profile finishes loading."""
    from . import main
    main.media_manager.set_media_folder(mw.col.media.dir())
    autoUpdate()
    patch_successful = patch_image_occlusion_enhanced()
    logger.info(f"Image Occlusion Enhanced patch: {patch_successful}")

def update_hooks_for_login_state(logged_in: bool):
    #placeholder for future use
    pass

# --- Hook Registration ---
def hooks_init():
    """Registers all hooks. Internal checks within callbacks manage behavior."""
    gui_hooks.profile_did_open.append(onProfileLoaded)

    # Add Cards related
    gui_hooks.add_cards_did_init.append(init_add_card)
    gui_hooks.add_cards_did_add_note.append(make_new_card)

    # Editor related
    gui_hooks.editor_did_init_buttons.append(init_editor_card)

    # Browser related
    gui_hooks.deck_browser_will_show_options_menu.append(
        on_deck_browser_will_show_options_menu
    )
    gui_hooks.browser_menus_did_init.append(add_browser_menu_item)

    # Context Menus (callbacks have internal checks)
    gui_hooks.browser_sidebar_will_show_context_menu.append(add_sidebar_context_menu)
    gui_hooks.browser_will_show_context_menu.append(context_menu_bulk_suggest)
