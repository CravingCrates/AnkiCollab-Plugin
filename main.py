
import os
import sys

from sys import platform
if platform == "linux" or platform == "linux2":
    sys.path.append(os.path.join(os.path.dirname(__file__), "dist/linux"))
elif platform == "darwin":
    sys.path.append(os.path.join(os.path.dirname(__file__), "dist/osx"))
elif platform == "win32":
    sys.path.append(os.path.join(os.path.dirname(__file__), "dist/win"))

from aqt import gui_hooks, mw
from aqt.browser import SidebarTreeView, SidebarItem, SidebarItemType
from anki.decks import DeckId
from aqt.qt import *
import json
import configparser
from datetime import datetime
import requests
import webbrowser
from concurrent.futures import Future

from .thread import run_function_in_thread
from .import_export import *

collab_menu = QMenu('AnkiCollab', mw)
mw.form.menubar.addMenu(collab_menu)

edit_list_action = QAction('Edit Subscriptions', mw)
collab_menu.addAction(edit_list_action)

push_deck_action = QAction('Publish new Deck', mw)
collab_menu.addAction(push_deck_action)

pull_changes_action = QAction('Check for New Content', mw)
collab_menu.addAction(pull_changes_action)

website_action = QAction('Open Website', mw)
collab_menu.addAction(website_action)

donation_action = QAction('Support us', mw)
collab_menu.addAction(donation_action)


strings_data = mw.addonManager.getConfig(__name__)
if strings_data is not None:
    mw.addonManager.writeConfig(__name__, strings_data)
    strings_data = mw.addonManager.getConfig(__name__)


def add_sidebar_context_menu(
    sidebar: SidebarTreeView, menu: QMenu, item: SidebarItem, index: QModelIndex
) -> None:
    menu.addSeparator()
    menu.addAction("Suggest on AnkiCollab", lambda: context_handler(item))

def context_handler(item: SidebarItem):
    if item.item_type == SidebarItemType.DECK:
        selected_deck = DeckId(item.id)      
        suggest_subdeck(selected_deck)
    else:
        aqt.utils.tooltip("Please select a deck")

gui_hooks.browser_sidebar_will_show_context_menu.append(add_sidebar_context_menu)

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
    button_box.addButton(mw.form.invokeAfterAddCheckbox, QDialogButtonBox.DestructiveRole)

def request_update():
    handle_pull(None)
            
def onProfileLoaded():
    aqt.utils.tooltip("Retrieving latest data from AnkiCollab...")
    run_function_in_thread(request_update)

def on_syncing_done(future: Future):
        if exc := future.exception():
            raise exc
        aqt.utils.tooltip("AnkiCollab: Done.")
        
#gui_hooks.profile_did_open.append(onProfileLoaded)
gui_hooks.add_cards_did_init.append(init_add_card)
gui_hooks.editor_did_init_buttons.append(init_editor_card)
gui_hooks.add_cards_did_add_note.append(make_new_card)


def delete_selected_rows(table):
    strings_data = mw.addonManager.getConfig(__name__)
    selected_rows = [index.row() for index in table.selectedIndexes()]
    for row in selected_rows:
        if table.item(row, 0) is not None:
            deck_hash = table.item(row, 0).text()
            requests.get("https://plugin.ankicollab.com/RemoveSubscription/" + deck_hash)      
            strings_data.pop(deck_hash)
    for row in reversed(selected_rows):
        table.removeRow(row)
    mw.addonManager.writeConfig(__name__, strings_data)

def add_to_table(line_edit, table, dialog):
    strings_data = mw.addonManager.getConfig(__name__)
    string = line_edit.text().replace(" ", "") # just to prevent issues for copy paste errors
    if string:
        strings_data[string] = {
            'timestamp': '2022-12-31 23:59:59',
            'deckId': 0
        }
        mw.addonManager.writeConfig(__name__, strings_data)
        line_edit.setText('')
        num_rows = table.rowCount()
        table.insertRow(num_rows)
        table.setItem(num_rows, 0, QTableWidgetItem(string))
        handle_pull(string)
        dialog.accept()
        #on_edit_list() # we could reopen the dialog with updated data

def get_local_deck_from_hash(input_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if hash == input_hash:
                return mw.col.decks.name(details["deckId"])
    return "None"

def update_local_deck(input_hash, new_deck, popup_dialog, subs_dialog):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if hash == input_hash:
                details["deckId"] = aqt.mw.col.decks.id(new_deck)
    mw.addonManager.writeConfig(__name__, strings_data)
    popup_dialog.accept()
    subs_dialog.accept()
    on_edit_list() #reopen with updated data

def on_edit_list():
    dialog = QDialog(mw)
    dialog.setWindowTitle('Edit list')
    layout = QVBoxLayout()
    dialog.setLayout(layout)
    
    table = QTableWidget()
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None:
        table.setRowCount(len(strings_data))
    table.setColumnCount(2) # set number of columns to 2
    table.setHorizontalHeaderLabels(['Subscription Key', 'Local Deck']) # add column headers   
    table.setColumnWidth(0, table.width() * 0.4) # adjust column widths
    table.setColumnWidth(1, table.width() * 0.4)
    
    if strings_data is not None:
        for row, (string, data) in enumerate(strings_data.items()):        
            item1 = QTableWidgetItem(string)
            item1.setFlags(item1.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item1)
            
            # add local deck name to column 2
            input_hash = string
            local_deck_name = get_local_deck_from_hash(input_hash)
            item2 = QTableWidgetItem(local_deck_name)
            item2.setFlags(item2.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 1, item2)
    
    layout.addWidget(table)
    
    delete_button = QPushButton('Delete Subscription')
    delete_button.clicked.connect(lambda: delete_selected_rows(table))
    
    edit_button = QPushButton('Edit Local Deck')
    edit_button.clicked.connect(lambda: edit_local_deck(table, dialog))
    
    line_edit = QLineEdit()
    add_button = QPushButton('Add Subscription')
    add_button.clicked.connect(lambda: add_to_table(line_edit, table, dialog))
    
    disclaimer = QLabel("The download may take a long time and Anki may seem unresponsive. Just be patient and do not close it.")
    
    add_layout = QHBoxLayout()
    add_layout.addWidget(line_edit)
    add_layout.addWidget(add_button)

    layout.addLayout(add_layout)
    layout.addWidget(disclaimer)
    layout.addWidget(delete_button)
    layout.addWidget(edit_button)
    
    dialog.exec()
    
    
def edit_local_deck(table, parent_dialog):
    selected_items = table.selectedItems()
    if len(selected_items) > 0:
        selected_row = selected_items[0].row()
        input_hash = table.item(selected_row, 0).text()
        local_deck_name = table.item(selected_row, 1).text()
        
        # create popup dialog
        dialog = QDialog(mw)
        dialog.setWindowTitle('Edit Local Deck')
        layout = QVBoxLayout()
        dialog.setLayout(layout)
        
        deck_label = QLabel("Deck:")
        deck_combo_box = QComboBox()
        
        decks = mw.col.decks.all()
        deck_names = [deck['name'] for deck in decks]
        deck_names.sort()
        deck_combo_box.addItems(deck_names)
        deck_combo_box.setCurrentText(local_deck_name) # set current deck name in combo box
        
        layout.addWidget(deck_label)
        layout.addWidget(deck_combo_box)
        
        save_button = QPushButton('Save')
        save_button.clicked.connect(lambda: update_local_deck(input_hash, deck_combo_box.currentText(), dialog, parent_dialog))
        layout.addWidget(save_button)
        
        dialog.exec()


edit_list_action.triggered.connect(on_edit_list)


def on_push_deck_action(self):
    dialog = QDialog(mw)
    dialog.setWindowTitle("Publish Deck")
    
    deck_label = QLabel("Deck:")
    deck_combo_box = QComboBox()
    
    decks = mw.col.decks.all()
    deck_names = [deck['name'] for deck in decks]
    deck_names.sort()
    deck_combo_box.addItems(deck_names)
    
    email_label = QLabel("Email: (Make sure to create an account on the website first)")
    email_field = QLineEdit()
    
    publish_button = QPushButton("Publish Deck")    
    disclaimer = QLabel("Processing can take a few minutes on the website. Be patient, please.")
    
    def on_publish_button_clicked():
        selected_deck_name = deck_combo_box.currentText()
        email = email_field.text()
        deck_id = None
        for deck in decks:
            if deck['name'] == selected_deck_name:
                deck_id = deck['id']
                break  
        uuid = handle_export(deck_id, email)
        if uuid:
            strings_data[uuid] = { 'timestamp': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'deckId': deck_id }
            mw.addonManager.writeConfig(__name__, strings_data)
    
    publish_button.clicked.connect(on_publish_button_clicked)
    
    layout = QVBoxLayout()
    layout.addWidget(deck_label)
    layout.addWidget(deck_combo_box)
    layout.addWidget(email_label)
    layout.addWidget(email_field)
    layout.addWidget(disclaimer)
    button_layout = QHBoxLayout()
    button_layout.addStretch()
    button_layout.addWidget(publish_button)
    layout.addLayout(button_layout)
    dialog.setLayout(layout)

    dialog.exec()


def open_donation_site():
    webbrowser.open('https://ko-fi.com/ankicollab')
    
def open_website():
    webbrowser.open('https://www.ankicollab.com/')
    
push_deck_action.triggered.connect(on_push_deck_action)
pull_changes_action.triggered.connect(onProfileLoaded)
donation_action.triggered.connect(open_donation_site)
website_action.triggered.connect(open_website)