
from aqt import gui_hooks, mw
from aqt.browser import Browser, SidebarTreeView, SidebarItem, SidebarItemType
from anki.decks import DeckId
from anki.notes import NoteId
from aqt.qt import *
from anki import hooks
from anki.collection import Collection

import json
from typing import Sequence

from .export_manager import *
from .import_manager import *
from .thread import run_function_in_thread

from .gear_menu_setup import add_browser_menu_item, on_deck_browser_will_show_options_menu
from .dialogs import AddChangelogDialog, get_login_token


def is_logged_in():
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None and "settings" in strings_data and strings_data["settings"]["token"] != "":
        return True
    return False

def add_sidebar_context_menu(
    sidebar: SidebarTreeView, menu: QMenu, item: SidebarItem, index: QModelIndex
) -> None:
    menu.addSeparator()
    menu.addAction("Suggest on AnkiCollab", lambda: suggest_context_handler(item))
    if is_logged_in():
        menu.addAction("Add new Changelog", lambda: changelog_context_handler(item))

def suggest_context_handler(item: SidebarItem):
    if item.item_type == SidebarItemType.DECK:
        selected_deck = DeckId(item.id)      
        suggest_subdeck(selected_deck)
    else:
        aqt.utils.tooltip("Please select a deck")
        
def changelog_context_handler(item: SidebarItem):
    if item.item_type == SidebarItemType.DECK:
        selected_did = DeckId(item.id)      
        hash = get_deck_hash_from_did(selected_did)
        if hash is None:
            aqt.utils.tooltip("This deck is not published")
            return
        dialog = AddChangelogDialog(hash, mw)
        dialog.exec()
    else:
        aqt.utils.tooltip("Please select a deck")
        
def bulk_suggest_handler(browser: Browser, nids: Sequence[NoteId]) -> None:
    if len(nids) < 2:
        aqt.utils.showInfo("Please use the regular suggest button for single notes", parent=browser)
        return
    bulk_suggest_notes(nids)   
    
def remove_notes(nids: Sequence[NoteId], window=None) -> None:
    deckHash = get_deck_hash_from_did(aqt.mw.col.get_note(nids[0]).cards()[0].did)
    
    if deckHash is None:
        aqt.utils.showInfo("Cannot find the Subscription Key for this Deck", parent=window if window is not None else mw)
        return
    
    guids = get_guids_from_noteids(nids)
    
    (rationale, commit_text) = get_commit_info(11)
    if rationale is None:
        return
    
    payload = {
        'remote_deck': deckHash,
        'note_guids': guids,
        'commit_text': commit_text,
        'token': get_login_token(),
        'force_overwrite': False
    }

    response = requests.post("https://plugin.ankicollab.com/requestRemoval", json=payload)
    if response.status_code == 200:
        print(response.text)
        delete_notes(nids)
    else:
        print(response.text)
    
def request_note_removal(browser: Browser, nids: Sequence[NoteId]) -> None:
    if len(nids) < 1:
        aqt.utils.showInfo("Please select a note", parent=browser)
        return    
    remove_notes(nids, browser)

def context_menu_bulk_suggest(browser: Browser, context_menu: QMenu) -> None:
    selected_nids = browser.selected_notes()    
    context_menu.addSeparator()    
    context_menu.addAction(
        "AnkiCollab: Bulk suggest notes",
        lambda: bulk_suggest_handler(browser, nids=selected_nids),
    )
    context_menu.addAction(
        "AnkiCollab: Remove note(s)",
        lambda: request_note_removal(browser, nids=selected_nids),
    )
    
def init_editor_card(buttons, editor):
    if isinstance(editor.parentWindow, aqt.addcards.AddCards): # avoid duplicates in the "Add" Window
        return buttons
    
    b = editor.addButton(
        None,
        "AnkiCollab",
        lambda editor: prep_suggest_card(editor.note, None),
        tip="Suggest Changes (AnkiCollab)",
    )

    buttons.append(b)
    return buttons

def init_add_card(addCardsDialog):
    mw.form.invokeAfterAddCheckbox = QCheckBox("Suggest on AnkiCollab")
    button_box = addCardsDialog.form.buttonBox
    button_box.addButton(mw.form.invokeAfterAddCheckbox, QDialogButtonBox.ButtonRole.DestructiveRole)
    
def request_update():
    remove_nonexistent_decks()
    handle_pull(None)
            
def onProfileLoaded():
    aqt.utils.tooltip("Retrieving latest data from AnkiCollab...")
    run_function_in_thread(request_update)
    
# Broken. Threading issue? #TODO
# def onDeleteNotes(col: Collection, ids: Sequence[anki.notes.NoteId]):
#     if len(ids) < 1:
#         return
#     if not aqt.utils.askUser("Do you want to remove the selected Notes from AnkiCollab?"):
#         return
    
#     remove_notes(ids)
    

def hooks_init():
    strings_data = mw.addonManager.getConfig(__name__)
    startup_hook = False
    if strings_data is not None:
        startup_hook = bool(strings_data["settings"]["pull_on_startup"])
        
    if startup_hook:
        gui_hooks.profile_did_open.append(onProfileLoaded)
    
    gui_hooks.add_cards_did_init.append(init_add_card)
    gui_hooks.editor_did_init_buttons.append(init_editor_card)
    gui_hooks.add_cards_did_add_note.append(make_new_card)

    gui_hooks.deck_browser_will_show_options_menu.append(
        on_deck_browser_will_show_options_menu
    )
    gui_hooks.browser_menus_did_init.append(add_browser_menu_item)
    gui_hooks.browser_sidebar_will_show_context_menu.append(add_sidebar_context_menu)
    gui_hooks.browser_will_show_context_menu.append(context_menu_bulk_suggest)
    
    # hooks.notes_will_be_deleted.append(onDeleteNotes)