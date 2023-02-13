
from aqt import gui_hooks, mw
from aqt.browser import SidebarTreeView, SidebarItem, SidebarItemType
from anki.decks import DeckId
from aqt.qt import *
import json
import configparser
from datetime import datetime
import requests
import webbrowser

from .thread import run_function_in_thread
from .import_export import *

collab_menu = QMenu('AnkiCollab', mw)
mw.form.menubar.addMenu(collab_menu)

edit_list_action = QAction('Edit Subscriptions', mw)
collab_menu.addAction(edit_list_action)

push_deck_action = QAction('Publish new Deck', mw)
collab_menu.addAction(push_deck_action)

pull_changes_action = QAction('Check for Updates', mw)
collab_menu.addAction(pull_changes_action)

website_action = QAction('Website', mw)
collab_menu.addAction(website_action)

donation_action = QAction('Donate', mw)
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
    if strings_data:
        for sub, details in strings_data.items():
            details["timestamp"] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        mw.addonManager.writeConfig(__name__, strings_data)
            
def onProfileLoaded():
    aqt.utils.tooltip("Fetching data from AnkiCollab...")
    run_function_in_thread(request_update)

gui_hooks.profile_did_open.append(onProfileLoaded)
gui_hooks.add_cards_did_init.append(init_add_card)
gui_hooks.editor_did_init_buttons.append(init_editor_card)
gui_hooks.add_cards_did_add_note.append(make_new_card)


def delete_selected_rows(table):
    selected_rows = [index.row() for index in table.selectedIndexes()]
    for row in selected_rows:
        if table.item(row, 0) is not None:
            strings_data.pop(table.item(row, 0).text())
    for row in reversed(selected_rows):
        table.removeRow(row)
    mw.addonManager.writeConfig(__name__, strings_data)

def add_to_table(line_edit, table):
    string = line_edit.text()
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

def on_edit_list():
    dialog = QDialog(mw)
    dialog.setWindowTitle('Edit list')
    layout = QVBoxLayout()
    dialog.setLayout(layout)
    
    table = QTableWidget()
    if strings_data is not None:
        table.setRowCount(len(strings_data))
    table.setColumnCount(1)
    table.setHorizontalHeaderLabels(['Deckname'])    
    table.setColumnWidth(0, table.width() * 0.7)
    
    if strings_data is not None:
        for row, (string, data) in enumerate(strings_data.items()):        
            item = QTableWidgetItem(string)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            table.setItem(row, 0, item)
    
    layout.addWidget(table)
    
    delete_button = QPushButton('Delete Subscription')
    delete_button.clicked.connect(lambda: delete_selected_rows(table))
    
    line_edit = QLineEdit()
    add_button = QPushButton('Add Subscription')
    add_button.clicked.connect(lambda: add_to_table(line_edit, table))
    
    disclaimer = QLabel("The download may take a long time and Anki may seem unresponsive. Just be patient and do not close it.")
    
    add_layout = QHBoxLayout()
    add_layout.addWidget(line_edit)
    add_layout.addWidget(add_button)

    layout.addLayout(add_layout)
    layout.addWidget(disclaimer)
    layout.addWidget(delete_button)
    
    dialog.exec()

edit_list_action.triggered.connect(on_edit_list)


def on_push_deck_action(self):
    dialog = QDialog(mw)
    dialog.setWindowTitle("Publish Deck")
    
    deck_label = QLabel("Deck:")
    deck_combo_box = QComboBox()
    
    decks = mw.col.decks.all()
    deck_names = [deck['name'] for deck in decks]
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