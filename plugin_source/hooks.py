
from aqt import gui_hooks, mw
from aqt.browser import Browser, SidebarTreeView, SidebarItem, SidebarItemType
from anki.decks import DeckId
from anki.notes import NoteId
from aqt.qt import *

import json
from typing import Sequence

from .export_manager import *
from .import_manager import *

from .gear_menu_setup import add_browser_menu_item, on_deck_browser_will_show_options_menu
from .dialogs import AddChangelogDialog


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

def context_menu_bulk_suggest(browser: Browser, context_menu: QMenu) -> None:
    selected_nids = browser.selected_notes()    
    context_menu.addSeparator()    
    context_menu.addAction(
        "AnkiCollab: Bulk suggest notes",
        lambda: bulk_suggest_handler(browser, nids=selected_nids),
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

def hooks_init():
    #gui_hooks.profile_did_open.append(onProfileLoaded)
    gui_hooks.add_cards_did_init.append(init_add_card)
    gui_hooks.editor_did_init_buttons.append(init_editor_card)
    gui_hooks.add_cards_did_add_note.append(make_new_card)

    gui_hooks.deck_browser_will_show_options_menu.append(
        on_deck_browser_will_show_options_menu
    )
    gui_hooks.browser_menus_did_init.append(add_browser_menu_item)
    gui_hooks.browser_sidebar_will_show_context_menu.append(add_sidebar_context_menu)
    gui_hooks.browser_will_show_context_menu.append(context_menu_bulk_suggest)