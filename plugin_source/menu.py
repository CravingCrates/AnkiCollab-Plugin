from aqt import mw
from aqt.utils import askUser, showInfo
from aqt.qt import *
from datetime import datetime, timezone
import requests
import webbrowser

from .identifier import subscribe_to_deck, unsubscribe_from_deck
from .var_defs import DEFAULT_PROTECTED_TAGS
from .utils import get_local_deck_from_hash
from .import_manager import *
from .export_manager import handle_export
from .media_import import on_media_btn
from .hooks import async_update, update_hooks_for_login_state
from .dialogs import LoginDialog
from .auth_manager import auth_manager

collab_menu = QMenu('AnkiCollab', mw)
settings_menu = QMenu('Settings', mw)
links_menu = QMenu('Links', mw)

# Main Actions
edit_list_action = QAction('Edit Subscriptions', mw)
push_deck_action = QAction('Publish new Deck', mw)
pull_changes_action = QAction('Check for New Content', mw)
login_manager_action = QAction('Login', mw) # Default text is Login
media_import_action = QAction('Import Media from Folder', mw)

# Settings Actions
pull_on_startup_action = QAction('Check for Updates on Startup', mw)
suspend_new_cards_action = QAction('Automatically suspend new Cards', mw)
move_cards_action = QAction('Do not move Cards automatically', mw)
auto_approve_action = QAction('Auto Approve Changes (Maintainer only)', mw) # Maintainer setting

# Links Actions
community_action = QAction('Join the Community', mw)
website_action = QAction('Open Website', mw)
donation_action = QAction('Leave a review', mw)

settings_menu.menuAction().setMenuRole(QAction.MenuRole.NoRole)
pull_on_startup_action.setMenuRole(QAction.MenuRole.NoRole)
suspend_new_cards_action.setMenuRole(QAction.MenuRole.NoRole)
move_cards_action.setMenuRole(QAction.MenuRole.NoRole)
auto_approve_action.setMenuRole(QAction.MenuRole.NoRole)

def force_logout():
    auth_manager.logout()
    update_ui_for_login_state() # Update UI after logout
    showInfo("Your login expired. Please log in again.")
    mw.reset()

def delete_selected_rows(table):
    strings_data = mw.addonManager.getConfig(__name__)
    selected_rows = [index.row() for index in table.selectedIndexes()]
    for row in selected_rows:
        if table.item(row, 0) is not None:
            deck_hash = table.item(row, 0).text()
            unsubscribe_from_deck(deck_hash)
            if deck_hash in strings_data:
                strings_data.pop(deck_hash)
    for row in reversed(selected_rows):
        table.removeRow(row)
    mw.addonManager.writeConfig(__name__, strings_data)


def add_to_table(line_edit, table, dialog):
    if not askUser(
            (
                "Proceeding will download and install a file from the internet that is potentially malicious!<br>"
                "We are not able to check every upload, so only download and install Decks that you know and trust!<br><br>"
                "Do you want to proceed?"
            ),
            title="AnkiCollab",
        ):
        return
    strings_data = mw.addonManager.getConfig(__name__)
    string = line_edit.text().strip() # just to prevent issues for copy paste errors
    if string:
        # Check if already subscribed
        if string in strings_data:
            showInfo(f"You are already subscribed to '{string}'.")
            line_edit.setText('')
            return

        strings_data[string] = {
            'timestamp': '2022-12-31 23:59:59',
            'deckId': 0,
            'optional_tags': {},
            'personal_tags': DEFAULT_PROTECTED_TAGS,
        }
        mw.addonManager.writeConfig(__name__, strings_data)
        line_edit.setText('')
        num_rows = table.rowCount()
        table.insertRow(num_rows)
        table.setItem(num_rows, 0, QTableWidgetItem(string))
        dialog.accept()
        subscribe_to_deck(string)
        handle_pull(string)
        #on_edit_list() # we could reopen the dialog with updated data

def update_local_deck(input_hash, new_deck, popup_dialog, subs_dialog):
    strings_data = mw.addonManager.getConfig(__name__)
    deck_id = mw.col.decks.id(new_deck) # Get deck ID
    if strings_data and input_hash in strings_data:
        strings_data[input_hash]["deckId"] = deck_id
        mw.addonManager.writeConfig(__name__, strings_data)
        popup_dialog.accept()
        subs_dialog.accept()
        on_edit_list() # Reopen with updated data
    else:
        showInfo(f"Error updating local deck for hash: {input_hash}. Configuration not found.")
        popup_dialog.accept()


def on_edit_list():
    dialog = QDialog(mw)
    dialog.setWindowTitle('Edit Subscriptions')
    layout = QVBoxLayout()
    dialog.setLayout(layout)

    table = QTableWidget()
    strings_data = mw.addonManager.getConfig(__name__)

    # Filter out settings/auth keys before counting rows
    filtered_keys = [k for k in strings_data if k not in ["settings", "auth"]] if strings_data else []
    table.setRowCount(len(filtered_keys))
    table.setColumnCount(2)
    table.setHorizontalHeaderLabels(['Subscription Key', 'Local Deck'])
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    # table.setColumnWidth(0, int(dialog.width() * 0.45)) # Initial guess, stretch is better
    # table.setColumnWidth(1, int(dialog.width() * 0.45))
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows) # Select whole rows

    if strings_data is not None:
        row = 0
        for string, data in strings_data.items():
            if string == "settings" or string == "auth":
                continue

            item1 = QTableWidgetItem(string)
            item1.setFlags(item1.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item1)

            local_deck_name = get_local_deck_from_hash(string)
            item2 = QTableWidgetItem(local_deck_name if local_deck_name else "Not Set")
            item2.setFlags(item2.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 1, item2)

            row += 1

    layout.addWidget(table)

    add_layout = QHBoxLayout()
    line_edit = QLineEdit()
    line_edit.setPlaceholderText("Enter Subscription Key...")
    add_button = QPushButton('Add Subscription')
    add_button.clicked.connect(lambda: add_to_table(line_edit, table, dialog))
    add_layout.addWidget(line_edit)
    add_layout.addWidget(add_button)

    disclaimer = QLabel("Adding a subscription may take time to download. Anki might seem unresponsive.")
    disclaimer.setWordWrap(True)

    button_layout = QHBoxLayout()
    delete_button = QPushButton('Delete Selected')
    delete_button.clicked.connect(lambda: delete_selected_rows(table))
    edit_button = QPushButton('Set Local Deck for Selected')
    edit_button.clicked.connect(lambda: edit_local_deck(table, dialog))
    button_layout.addWidget(delete_button)
    button_layout.addWidget(edit_button)
    button_layout.addStretch()

    layout.addLayout(add_layout)
    layout.addWidget(disclaimer)
    layout.addLayout(button_layout)

    dialog.exec()


def edit_local_deck(table, parent_dialog):
    selected_rows = list(set(index.row() for index in table.selectedIndexes())) # Get unique selected rows
    if len(selected_rows) != 1:
         showInfo("Please select exactly one subscription to edit.")
         return

    selected_row = selected_rows[0]
    input_hash = table.item(selected_row, 0).text()
    # current_local_deck_name = table.item(selected_row, 1).text() # Not needed directly

    # create popup dialog
    dialog = QDialog(mw)
    dialog.setWindowTitle('Set Local Deck')
    layout = QVBoxLayout()
    dialog.setLayout(layout)

    deck_label = QLabel(f"Choose local deck for subscription:\n'{input_hash}'")
    deck_combo_box = QComboBox()

    # Use all_names_and_ids for consistency and potential future use of IDs
    decks = mw.col.decks.all_names_and_ids(skip_empty_default=True) # Skip "[Default]" if empty
    deck_names = sorted([d.name for d in decks])
    deck_combo_box.addItems(deck_names)

    # Try to pre-select the current deck
    current_deck_name = get_local_deck_from_hash(input_hash)
    if current_deck_name and current_deck_name in deck_names:
        deck_combo_box.setCurrentText(current_deck_name)
    elif deck_names:
         deck_combo_box.setCurrentIndex(0) # Default to first if not set or not found

    layout.addWidget(deck_label)
    layout.addWidget(deck_combo_box)

    save_button = QPushButton('Save')
    save_button.clicked.connect(lambda: update_local_deck(input_hash, deck_combo_box.currentText(), dialog, parent_dialog))
    layout.addWidget(save_button)

    dialog.exec()

def on_push_deck_action():
    dialog = QDialog(mw)
    dialog.setWindowTitle("Publish Deck")

    # Layouts
    form_layout = QFormLayout()
    checkbox_layout = QVBoxLayout()
    button_layout = QHBoxLayout()
    main_layout = QVBoxLayout()

    # Deck Selection
    deck_combo_box = QComboBox()
    decks = mw.col.decks.all_names_and_ids(include_filtered=False)
    deck_names = sorted([d.name for d in decks if "::" not in d.name and d.id != 1])
    deck_combo_box.addItems(deck_names)
    form_layout.addRow("Deck:", deck_combo_box)

    # Username (Consider pre-filling if available from auth_manager)
    username_field = QLineEdit()
    if auth_manager.is_logged_in():
        # TODO: Add username retrieval logic if available
        # username = auth_manager.get_username()
        # if username:
        #    username_field.setText(username)
        #    username_field.setReadOnly(True) # Maybe make it read-only if logged in
        pass 
    form_layout.addRow("Username:", username_field)


    # Disclaimer Checkbox
    disclaimer_checkbox = QCheckBox("I declare under penalty of perjury that the material I am sharing is\n"
                                "entirely my own work, or I have obtained a license from the\n"
                                "intellectual property holder(s) to share it on AnkiCollab.")

    disclaimer_checkbox.setMaximumWidth(600)
    disclaimer_checkbox.setStyleSheet("""
        QCheckBox::indicator {
            width: 13px;
            height: 13px;
        }
        QCheckBox {
            background: transparent;
            color: black;  /* Set text color */
        }
        QCheckBox:hover, QCheckBox:checked {
            color: black;  /* Maintain text color on hover/check */
        }
        QCheckBox:focus {
            outline: none;  /* Remove focus outline */
        }
    """)

    # Terms Checkbox with Links
    terms_checkbox = QCheckBox()
    terms_checkbox.setStyleSheet("""
        QCheckBox::indicator {
            width: 13px;
            height: 13px;
        }
        QCheckBox {
            background: transparent;
            color: black;
        }
        QCheckBox:hover, QCheckBox:checked {
            color: black;
        }
        QCheckBox:focus {
            outline: none;  /* Remove focus outline */
        }
    """)
    terms_link = QLabel('<a href="https://ankicollab.com/terms">Terms of Service</a>')
    terms_link.setOpenExternalLinks(True)
    privacy_link = QLabel('<a href="https://ankicollab.com/privacy">Privacy Policy</a>')
    privacy_link.setOpenExternalLinks(True)

    terms_layout = QHBoxLayout()
    terms_layout.setContentsMargins(0, 0, 0, 0)
    terms_layout.addWidget(terms_checkbox)
    terms_layout.addWidget(QLabel("I agree to the"))
    terms_layout.addWidget(terms_link)
    terms_layout.addWidget(QLabel("and"))
    terms_layout.addWidget(privacy_link)
    terms_layout.addStretch()

    checkbox_layout.addWidget(disclaimer_checkbox)
    checkbox_layout.addLayout(terms_layout)

    # Publish Button
    publish_button = QPushButton("Publish Deck")

    def on_publish_button_clicked():
        if not disclaimer_checkbox.isChecked() or not terms_checkbox.isChecked():
            showInfo(
                "You must agree to both the copyright declaration and the terms/privacy policy to publish your deck.",
                parent=dialog
            )
            return

        selected_deck_name = deck_combo_box.currentText()
        username = username_field.text().strip() # Ensure no leading/trailing spaces

        if not selected_deck_name:
            showInfo("Please select a deck.", parent=dialog)
            return
        if not username:
            showInfo("Please enter your username.", parent=dialog)
            return

        deck_id = mw.col.decks.id(selected_deck_name)
        if not deck_id:
            showInfo(f"Could not find deck ID for '{selected_deck_name}'.", parent=dialog)
            return

        try:
            handle_export(deck_id, username)            
            dialog.accept()
        except Exception as e:
            #mw.progress.finish()
            showInfo(f"An error occurred during publishing: {e}", parent=dialog)
            print(f"Publishing error: {e}")

    publish_button.clicked.connect(on_publish_button_clicked)

    button_layout.addStretch()
    button_layout.addWidget(publish_button)

    # Assemble Main Layout
    main_layout.addLayout(form_layout)
    main_layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))
    main_layout.addLayout(checkbox_layout)
    main_layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))
    main_layout.addLayout(button_layout)
    dialog.setLayout(main_layout)

    dialog.exec()


def open_community_site():
    webbrowser.open('https://discord.gg/9x4DRxzqwM')

def open_support_site():
    webbrowser.open('https://www.ankicollab.com/leavereview')

def open_website():
    webbrowser.open('https://www.ankicollab.com/')

def on_login_manager_btn():
    if auth_manager.is_logged_in():
        force_logout()
        showInfo("You have been logged out.")
    else:
        # Show Login Dialog
        dialog = LoginDialog(mw)
        result = dialog.exec()
        if auth_manager.is_logged_in(): # Verify login status *after* dialog closes
            showInfo("Login successful!")
            update_ui_for_login_state() # Update UI after successful login


def store_default_config():
    config = mw.addonManager.getConfig(__name__)
    if config is None:
        config = {}

    if "settings" not in config:
        config["settings"] = {}
    if "auth" not in config:
        config["auth"] = {} # Should be managed by auth_manager, but ensure key exists

    defaults = {
        "auto_approve": False,
        "pull_on_startup": False,
        "suspend_new_cards": False,
        "auto_move_cards": False, # Note: Action text is "Do not move", so False means "Do move"
        "rated_addon": False,
        "last_ratepls": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "pull_counter": 0,
        "push_counter": 0,
    }

    settings = config["settings"]
    updated = False
    for key, value in defaults.items():
        if key not in settings:
            settings[key] = value
            updated = True

    if updated:
        mw.addonManager.writeConfig(__name__, config)

def update_ui_for_login_state():
    logged_in = auth_manager.is_logged_in()
    config = mw.addonManager.getConfig(__name__)
    settings = config.get("settings", {}) if config else {}

    # Update Menu Items Visibility
    edit_list_action.setVisible(logged_in)
    push_deck_action.setVisible(logged_in)
    pull_changes_action.setVisible(logged_in)
    settings_menu.menuAction().setVisible(logged_in) # Hide the whole settings submenu
    media_import_action.setVisible(logged_in)

    login_manager_action.setText("Logout" if logged_in else "Login")

    if logged_in:
        pull_on_startup_action.setCheckable(True)
        pull_on_startup_action.setChecked(bool(settings.get("pull_on_startup", False)))
        suspend_new_cards_action.setCheckable(True)
        suspend_new_cards_action.setChecked(bool(settings.get("suspend_new_cards", False)))
        move_cards_action.setCheckable(True)
        move_cards_action.setChecked(bool(settings.get("auto_move_cards", False)))

        # Handle Maintainer Checkbox (Auto Approve)
        is_maintainer = True # maybe we can do a better check in the future here idk
        auto_approve_action.setVisible(is_maintainer)
        if is_maintainer:
            auto_approve_action.setCheckable(True)
            auto_approve_action.setChecked(bool(auth_manager.get_auto_approve())) # Use getter from auth_manager

    # Update Hooks
    update_hooks_for_login_state(logged_in)

    # Ensure menu bar is updated if actions were added/removed/hidden
    # This might not be strictly necessary if only visibility changes, but can help
    mw.form.menubar.repaint()

def menu_init():
    store_default_config()

    mw.form.menubar.addMenu(collab_menu)

    collab_menu.addAction(pull_changes_action)
    collab_menu.addSeparator()
    collab_menu.addAction(edit_list_action)
    collab_menu.addAction(push_deck_action)
    collab_menu.addSeparator()
    collab_menu.addMenu(settings_menu)
    collab_menu.addMenu(links_menu)
    collab_menu.addSeparator()
    collab_menu.addAction(login_manager_action)

    settings_menu.addAction(media_import_action)
    settings_menu.addSeparator()
    settings_menu.addAction(pull_on_startup_action)
    settings_menu.addAction(suspend_new_cards_action)
    settings_menu.addAction(move_cards_action)
    settings_menu.addSeparator()
    settings_menu.addAction(auto_approve_action)

    links_menu.addAction(community_action)
    links_menu.addAction(website_action)
    links_menu.addAction(donation_action) # Review link

    edit_list_action.triggered.connect(on_edit_list)
    push_deck_action.triggered.connect(on_push_deck_action)
    pull_changes_action.triggered.connect(async_update)
    media_import_action.triggered.connect(on_media_btn)
    website_action.triggered.connect(open_website)
    donation_action.triggered.connect(open_support_site)
    community_action.triggered.connect(open_community_site)
    login_manager_action.triggered.connect(on_login_manager_btn)

    # Settings toggles
    def toggle_setting(setting_key, checked):
        config = mw.addonManager.getConfig(__name__)
        print(f"AnkiCollab: Toggling setting '{setting_key}' to {checked}.")
        if config and "settings" in config:
            config["settings"][setting_key] = checked
            mw.addonManager.writeConfig(__name__, config)
        else:
            # Handle case where config might be missing unexpectedly
            print(f"AnkiCollab: Error toggling setting '{setting_key}'. Config not found.")

    pull_on_startup_action.triggered.connect(lambda checked: toggle_setting("pull_on_startup", checked))
    suspend_new_cards_action.triggered.connect(lambda checked: toggle_setting("suspend_new_cards", checked))
    move_cards_action.triggered.connect(lambda checked: toggle_setting("auto_move_cards", checked))
    auto_approve_action.triggered.connect(auth_manager.set_auto_approve)

    update_ui_for_login_state()
