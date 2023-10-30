
from aqt import mw

from aqt.qt import *
from datetime import datetime
import requests
import webbrowser

from .export_manager import *
from .import_manager import *

from .media_import import on_media_btn
from .hooks import onProfileLoaded
from .dialogs import LoginDialog

pull_on_startup_action = QAction('Check for Updates on Startup', mw)
auto_approve_action = QAction('Auto Approve Changes (Maintainer only)', mw)
login_manager_action = QAction('Logout', mw)
collab_menu = QMenu('AnkiCollab', mw)
settings_menu = QMenu('Settings', mw)

# Prevent macOS menu bar merging into Preferences by string matching "settings"
# by setting MenuRole to NoRole from the default TextHeuristicRole.
settings_menu.menuAction().setMenuRole(QAction.MenuRole.NoRole)
# Also set this for the settings menu actions to be safe.
pull_on_startup_action.setMenuRole(QAction.MenuRole.NoRole)
auto_approve_action.setMenuRole(QAction.MenuRole.NoRole)

def add_maintainer_checkbox():
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None:
        if "settings" in strings_data and strings_data["settings"]["token"] != "":
            auto_approve_action.setCheckable(True)            
            auto_approve_action.setChecked(bool(strings_data["settings"]["auto_approve"]))
            
            def toggle_auto_approve(checked):
                strings_data = mw.addonManager.getConfig(__name__)
                strings_data["settings"]["auto_approve"] = checked
                mw.addonManager.writeConfig(__name__, strings_data)

            auto_approve_action.triggered.connect(toggle_auto_approve)
            
            if auto_approve_action not in collab_menu.actions():
                settings_menu.addAction(auto_approve_action)
               
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
            'deckId': 0,
            'optional_tags': {},
            'gdrive': {},
        }
        mw.addonManager.writeConfig(__name__, strings_data)
        line_edit.setText('')
        num_rows = table.rowCount()
        table.insertRow(num_rows)
        table.setItem(num_rows, 0, QTableWidgetItem(string))
        dialog.accept()
        handle_pull(string)
        #on_edit_list() # we could reopen the dialog with updated data

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
        if "settings" in strings_data:
            table.setRowCount(len(strings_data) - 1)
        else:
            table.setRowCount(len(strings_data))
    table.setColumnCount(2) # set number of columns to 2
    table.setHorizontalHeaderLabels(['Subscription Key', 'Local Deck']) # add column headers   
    table.setColumnWidth(0, int(table.width() * 0.4)) # adjust column widths
    table.setColumnWidth(1, int(table.width() * 0.4))
    
    if strings_data is not None:
        row = 0
        for string, data in strings_data.items():
            if string == "settings":
                continue
            
            item1 = QTableWidgetItem(string)
            item1.setFlags(item1.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item1)
            
            input_hash = string
            local_deck_name = get_local_deck_from_hash(input_hash)
            item2 = QTableWidgetItem(local_deck_name)
            item2.setFlags(item2.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 1, item2)
            
            row += 1

    
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
        strings_data = mw.addonManager.getConfig(__name__)
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

def open_community_site():
    webbrowser.open('https://discord.gg/9x4DRxzqwM')

def open_donation_site():
    webbrowser.open('https://www.ankicollab.com/donate')
    
def open_website():
    webbrowser.open('https://www.ankicollab.com/')
        
def on_login_manager_btn():
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None:
        if "settings" in strings_data and strings_data["settings"]["token"] != "":
            # Logout
            requests.get("https://plugin.ankicollab.com/removeToken/" + strings_data["settings"]["token"])  
            strings_data["settings"]["token"] = ""
            login_manager_action.setText("Login")
            if auto_approve_action in collab_menu.actions():
                collab_menu.removeAction(auto_approve_action)
            mw.addonManager.writeConfig(__name__, strings_data)
            aqt.utils.showInfo("You have been logged out.")
        else:
            # Popup login dialog
            dialog = LoginDialog(mw)
            dialog.exec()
            strings_data = mw.addonManager.getConfig(__name__)
            if "settings" in strings_data and strings_data["settings"]["token"] != "": # Login was Successful                
                login_manager_action.setText("Logout")
                add_maintainer_checkbox()
     
def store_default_config():
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None:
        if "settings" not in strings_data:
            strings_data["settings"] = {}
        if "token" not in strings_data["settings"]:
            strings_data["settings"]["token"] = ""
        if "auto_approve" not in strings_data["settings"]:
            strings_data["settings"]["auto_approve"] = False
        if "pull_on_startup" not in strings_data["settings"]:
            strings_data["settings"]["pull_on_startup"] = False
    mw.addonManager.writeConfig(__name__, strings_data)
       
def menu_init():                
    mw.form.menubar.addMenu(collab_menu)
    store_default_config()

    edit_list_action = QAction('Edit Subscriptions', mw)
    collab_menu.addAction(edit_list_action)

    push_deck_action = QAction('Publish new Deck', mw)
    collab_menu.addAction(push_deck_action)

    pull_changes_action = QAction('Check for New Content', mw)
    collab_menu.addAction(pull_changes_action)

    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None:
        if "settings" in strings_data and "token" in strings_data["settings"]:
            if strings_data["settings"]["token"] != "":
                login_manager_action.setText("Logout")
                add_maintainer_checkbox()
            else:
                login_manager_action.setText("Login")
            
        if "settings" in strings_data and "pull_on_startup" in strings_data["settings"]:
            pull_on_startup_action.setCheckable(True)
            pull_on_startup_action.setChecked(bool(strings_data["settings"]["pull_on_startup"]))

    collab_menu.addAction(login_manager_action)

    media_import_action = QAction('Import Media from Folder', mw)
    collab_menu.addAction(media_import_action)

    def toggle_startup_pull(checked):
        strings_data = mw.addonManager.getConfig(__name__)
        if "settings" not in strings_data:
            strings_data["settings"] = {}
        strings_data["settings"]["pull_on_startup"] = checked
        mw.addonManager.writeConfig(__name__, strings_data)

    pull_on_startup_action.triggered.connect(toggle_startup_pull)
    settings_menu.addAction(pull_on_startup_action)
            
    collab_menu.addMenu(settings_menu)
    
    links_menu = QMenu('Links', mw)
    collab_menu.addMenu(links_menu)    

    community_action = QAction('Join the Community', mw)
    links_menu.addAction(community_action)

    website_action = QAction('Open Website', mw)
    links_menu.addAction(website_action)

    donation_action = QAction('Support us', mw)
    links_menu.addAction(donation_action)
    
    edit_list_action.triggered.connect(on_edit_list)
    push_deck_action.triggered.connect(on_push_deck_action)
    pull_changes_action.triggered.connect(onProfileLoaded)
    media_import_action.triggered.connect(on_media_btn)
    website_action.triggered.connect(open_website)
    donation_action.triggered.connect(open_donation_site)
    community_action.triggered.connect(open_community_site)
    login_manager_action.triggered.connect(on_login_manager_btn)
