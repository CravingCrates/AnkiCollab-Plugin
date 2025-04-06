from aqt import gui_hooks, mw
from aqt.browser import Browser, SidebarTreeView, SidebarItem, SidebarItemType
from anki.decks import DeckId
from anki.notes import NoteId
from aqt.qt import *
from anki import hooks
from anki.collection import Collection
from aqt.utils import askUser, showInfo

import json
from typing import Sequence, List # Import List

from .export_manager import *
from .import_manager import *
from .thread import run_function_in_thread

from .gear_menu_setup import add_browser_menu_item, on_deck_browser_will_show_options_menu
from .dialogs import AddChangelogDialog

from .auth_manager import auth_manager

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

    # TODO: Backgroun threading
    try:
        response = requests.post(f"{API_BASE_URL}/requestRemoval", json=payload)
        response.raise_for_status()

        print(f"Removal request response: {response.text}")
        if askUser(f"Successfully requested removal of {len(nids)} note(s) from AnkiCollab.\nDo you want to delete them locally now?", parent=window if window is not None else mw):
             delete_notes(nids)

    except requests.exceptions.RequestException as e:
        showInfo(f"Error requesting note removal: {e}", parent=window if window is not None else mw)
        print(f"Removal request failed: {e}")
    except Exception as e:
        showInfo(f"An unexpected error occurred: {e}", parent=window if window is not None else mw)
        print(f"Unexpected error during removal request: {e}")


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
        print("AnkiCollab: Could not find buttonBox in AddCards dialog, added checkbox to main layout.")


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

    remove_nonexistent_decks()
    handle_pull(None, silent)

def async_update(silent: bool = False) -> None:
    """Asynchronous update check. This is called when the user clicks the update button."""
    if not auth_manager.is_logged_in():
        aqt.utils.tooltip("Log in to AnkiCollab to check for updates.")
        return

    aqt.utils.tooltip("AnkiCollab: Checking for updates...")
    run_function_in_thread(request_update, silent)

def autoUpdate():
    config = mw.addonManager.getConfig(__name__)
    settings = config.get("settings", {}) if config else {}
    startup_check_enabled = bool(settings.get("pull_on_startup", False))

    if startup_check_enabled:
        async_update(True)

def onProfileLoaded():
    """Called when the Anki profile finishes loading."""
    from . import main
    main.media_manager.set_media_folder(mw.col.media.dir())
    autoUpdate()

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
