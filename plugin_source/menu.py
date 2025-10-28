from aqt import mw
from aqt.utils import askUser, showInfo
from aqt.qt import *
# Explicit imports to satisfy static analyzers (aqt.qt re-exports Qt classes)
from aqt.qt import (
    QMenu,
    QAction,
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QGroupBox,
    QLineEdit,
    QComboBox,
    QLabel,
    QPushButton,
    QCheckBox,
    QRadioButton,
    QAbstractItemView,
    QHeaderView,
    QSizePolicy,
    QApplication,
    Qt,
)
from aqt.theme import theme_manager
from datetime import datetime, timezone
import requests
import webbrowser

from .identifier import subscribe_to_deck, unsubscribe_from_deck
from .var_defs import DEFAULT_PROTECTED_TAGS
from .utils import get_local_deck_from_hash, DeckManager
from .import_manager import *
from .export_manager import handle_export
from .media_import import on_media_btn
from .hooks import async_update, update_hooks_for_login_state
from .dialogs import LoginDialog
from .auth_manager import auth_manager
from .sentry_integration import init_sentry
from anki.utils import point_version

collab_menu = QMenu('AnkiCollab', mw)
links_menu = QMenu('Links', mw)

# Main Actions
edit_list_action = QAction('Edit Subscriptions', mw)
push_deck_action = QAction('Publish New Deck', mw)
push_all_stats_action = QAction('Submit All Review History', mw)
pull_changes_action = QAction('Update Decks', mw)
login_manager_action = QAction('Login', mw) # Default text is Login
media_import_action = QAction('Import Media from Folder', mw)

# Links Actions
community_action = QAction('Join the Community', mw)
website_action = QAction('Open Website', mw)
donation_action = QAction('Leave a review', mw)

def force_logout(with_dialog = True):
    auth_manager.logout()
    update_ui_for_login_state() # Update UI after logout
    if with_dialog:
        showInfo("Your login expired. Please log in again.")
    mw.reset()

def delete_selected_rows(table, dialog):
    if table.selectedIndexes() == []:
        showInfo("Please select at least one subscription to delete.")
        return
    dialog.accept()
    strings_data = mw.addonManager.getConfig(__name__)
    selected_rows = sorted(set(index.row() for index in table.selectedIndexes()), reverse=True)
    if not strings_data:
        strings_data = {}
    for row in selected_rows:
        if table.item(row, 0) is not None:
            deck_hash = table.item(row, 0).text()
            logger.debug(f"found: {deck_hash}")
            op = QueryOp(
                parent=mw,
                op=lambda col: unsubscribe_from_deck(deck_hash),
                success=lambda result: None
            )
            if point_version() >= 231000:
                op = op.without_collection()
            op.run_in_background()
            
            if deck_hash in strings_data:
                logger.debug(f"Unsubscribing from deck: {deck_hash}")
                strings_data.pop(deck_hash)
        table.removeRow(row)
    mw.addonManager.writeConfig(__name__, strings_data)
    on_edit_list() # This will create a fresh DeckManager() and table


def validate_deck_hash(deck_hash: str) -> bool:
    if not deck_hash:
        return False
    
    parts = deck_hash.split('-')
    
    # Validate deck hash format: 3, 5, or 6 words (legacy and standard formats)
    if len(parts) not in (3, 5, 6):
        return False
    
    # Each part must be alphabetic only (lowercase for standard, mixed case for legacy)
    if not all(part and part.isalpha() for part in parts):
        return False
    
    return True


def add_to_table(line_edit, table, dialog):
    
    string = line_edit.text().strip() # just to prevent issues for copy paste errors
    if not validate_deck_hash(string):
        showInfo("That deck subscription key doesn’t look right. Please double-check it.")
        return
        
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
    dialog.setWindowTitle('AnkiCollab - Manage Subscriptions')
    dialog.setMinimumSize(800, 500)
    layout = QVBoxLayout()
    dialog.setLayout(layout)

    # Header
    header_label = QLabel("Manage Your AnkiCollab Subscriptions")
    header_label.setStyleSheet("font-size: 16px; font-weight: bold; margin: 10px 0; color: #2196F3;")
    header_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(header_label)

    table = QTableWidget()
    strings_data = mw.addonManager.getConfig(__name__)

    # Filter out settings/auth keys before counting rows
    filtered_keys = [k for k in strings_data if k not in ["settings", "auth"]] if strings_data else []
    table.setRowCount(len(filtered_keys))
    table.setColumnCount(5)
    table.setHorizontalHeaderLabels(['Subscription Key', 'Local Deck', 'New Notes Deck', 'Last Updated', 'Actions'])
    
    # Style the table
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.verticalHeader().setVisible(False)
    
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

    row = 0
    decks = DeckManager()
    for deck_hash, details in decks:
        item1 = QTableWidgetItem(deck_hash)
        item1.setFlags(item1.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 0, item1)

        # Local Deck
        local_deck_name = get_local_deck_from_hash(deck_hash)
        item2 = QTableWidgetItem(local_deck_name if local_deck_name else "Not Set")
        item2.setFlags(item2.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 1, item2)

        # New Notes Deck
        new_notes_deck_id = details.get("new_notes_home_deck", None)
        if new_notes_deck_id and mw.col:
            try:
                new_notes_deck_name = mw.col.decks.name_if_exists(new_notes_deck_id) or "Same as Local Deck"
            except:
                new_notes_deck_name = "Same as Local Deck"
        else:
            new_notes_deck_name = "Same as Local Deck"
        item3 = QTableWidgetItem(new_notes_deck_name)
        item3.setFlags(item3.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 2, item3)

        # Last Updated
        timestamp = details.get("timestamp", "Never")
        item4 = QTableWidgetItem(str(timestamp))
        item4.setFlags(item4.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 3, item4)

        # Actions
        actions_widget = QWidget()
        actions_layout = QHBoxLayout(actions_widget)
        actions_layout.setContentsMargins(5, 2, 5, 2)
        
        edit_button = QPushButton("⚙️")
        edit_button.setToolTip("Edit subscription settings")
        edit_button.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; border: none; padding: 6px 8px; border-radius: 3px; font-size: 12px; } QPushButton:hover { background-color: #45a049; }")
        edit_button.clicked.connect(lambda checked, h=deck_hash, d=dialog: edit_subscription_details(h, d))
        actions_layout.addWidget(edit_button)
        
        table.setCellWidget(row, 4, actions_widget)

        row += 1

    layout.addWidget(table)

    # Add new subscription section    
    add_section = QGroupBox("Add New Subscription")
    add_section.setStyleSheet("""
        QGroupBox { 
            font-weight: bold; 
            margin-top: 15px;
            padding-top: 15px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 5px;
            margin-top: 0px;
        }
    """)
    add_layout = QVBoxLayout(add_section)
    add_layout.setContentsMargins(10, 10, 10, 10)  # Add some internal padding
    
    add_input_layout = QHBoxLayout()
    line_edit = QLineEdit()
    line_edit.setPlaceholderText("Enter Subscription Key...")
    line_edit.setStyleSheet("QLineEdit { padding: 8px; border: 2px solid #ddd; border-radius: 4px; } QLineEdit:focus { border-color: #2196F3; }")
    add_button = QPushButton('Add Subscription')
    add_button.setStyleSheet("QPushButton { background-color: #2196F3; color: white; border: none; padding: 8px 16px; border-radius: 4px; font-weight: bold; } QPushButton:hover { background-color: #1976D2; }")
    add_button.clicked.connect(lambda: add_to_table(line_edit, table, dialog))
    add_input_layout.addWidget(line_edit)
    add_input_layout.addWidget(add_button)
    add_layout.addLayout(add_input_layout)

    disclaimer = QLabel("⚠️ Adding a subscription may take time to download. Anki might seem unresponsive during this process.")
    disclaimer.setWordWrap(True)
    disclaimer.setStyleSheet("color: #ff9800; font-style: italic; margin: 5px 0;")
    add_layout.addWidget(disclaimer)
    
    layout.addWidget(add_section)

    # Bottom buttons
    button_layout = QHBoxLayout()
    
    delete_button = QPushButton('🗑️ Delete Selected')
    delete_button.setStyleSheet("QPushButton { background-color: #f44336; color: white; border: none; padding: 8px 16px; border-radius: 4px; } QPushButton:hover { background-color: #d32f2f; }")
    delete_button.clicked.connect(lambda: delete_selected_rows(table, dialog))
    button_layout.addWidget(delete_button)
    
    settings_button = QPushButton('🛠️ Global Settings')
    settings_button.setStyleSheet("QPushButton { background-color: #2196F3; color: white; border: none; padding: 8px 16px; border-radius: 4px; } QPushButton:hover { background-color: #1976D2; }")
    settings_button.clicked.connect(lambda: show_global_settings_dialog(dialog))
    button_layout.addWidget(settings_button)
    
    button_layout.addStretch()
    
    close_button = QPushButton('Close')
    close_button.setStyleSheet("QPushButton { background-color: #607D8B; color: white; border: none; padding: 8px 16px; border-radius: 4px; } QPushButton:hover { background-color: #455A64; }")
    close_button.clicked.connect(dialog.accept)
    button_layout.addWidget(close_button)

    layout.addLayout(button_layout)

    dialog.exec()


def edit_subscription_details(deck_hash, parent_dialog):
    """Edit detailed settings for a specific subscription"""
    strings_data = mw.addonManager.getConfig(__name__)
    if not strings_data or deck_hash not in strings_data:
        showInfo("Subscription not found!")
        return

    details = strings_data[deck_hash]
    
    dialog = QDialog(parent_dialog)
    dialog.setWindowTitle(f'Edit Subscription Settings')
    dialog.setMinimumSize(500, 400)
    layout = QVBoxLayout()
    dialog.setLayout(layout)

    # Guard against missing collection to avoid crashes on startup
    if not mw.col:
        showInfo("Anki collection is not available. Please try again.")
        dialog.deleteLater()
        return

    # Header
    header_label = QLabel(f"Subscription Settings")
    header_label.setStyleSheet("font-size: 14px; font-weight: bold; margin: 10px 0; color: #2196F3;")
    header_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(header_label)

    # Hash info
    hash_info = QLabel(f"Subscription Key: {deck_hash}")
    hash_info.setStyleSheet("font-size: 10px; color: #666; margin-bottom: 15px;")
    hash_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(hash_info)

    # Local Deck Selection
    local_deck_group = QGroupBox("Local Deck (for existing notes)")
    local_deck_layout = QVBoxLayout(local_deck_group)
    
    local_deck_combo = QComboBox()
    decks = mw.col.decks.all_names_and_ids(skip_empty_default=True)
    deck_names = sorted([d.name for d in decks])
    local_deck_combo.addItems(deck_names)
    
    # Pre-select current deck
    current_deck_id = details.get("deckId", 0)
    if current_deck_id and mw.col:
        try:
            current_deck_name = mw.col.decks.name_if_exists(current_deck_id)
            if current_deck_name and current_deck_name in deck_names:
                local_deck_combo.setCurrentText(current_deck_name)
        except:
            pass
    
    local_deck_layout.addWidget(QLabel("Choose where existing notes will be placed:"))
    local_deck_layout.addWidget(local_deck_combo)
    layout.addWidget(local_deck_group)

    # New Notes Deck Selection
    new_notes_group = QGroupBox("New Notes Deck (for notes not in your collection)")
    new_notes_layout = QVBoxLayout(new_notes_group)
    
    # Option: Same as local deck
    same_deck_radio = QRadioButton("Use same deck as local deck")
    same_deck_radio.setChecked(True)  # Default
    
    # Option: Custom deck
    custom_deck_radio = QRadioButton("Use a different deck:")
    new_notes_combo = QComboBox()
    new_notes_combo.addItems(deck_names)
    new_notes_combo.setEnabled(False)  # Initially disabled
    
    # Pre-select current new notes deck if set
    current_new_notes_deck_id = details.get("new_notes_home_deck", None)
    if current_new_notes_deck_id and mw.col:
        try:
            current_new_notes_deck_name = mw.col.decks.name_if_exists(current_new_notes_deck_id)
            if current_new_notes_deck_name and current_new_notes_deck_name in deck_names:
                custom_deck_radio.setChecked(True)
                same_deck_radio.setChecked(False)
                new_notes_combo.setEnabled(True)
                new_notes_combo.setCurrentText(current_new_notes_deck_name)
        except:
            pass
    
    # Radio button functionality
    def on_radio_changed():
        new_notes_combo.setEnabled(custom_deck_radio.isChecked())
    
    same_deck_radio.toggled.connect(on_radio_changed)
    custom_deck_radio.toggled.connect(on_radio_changed)
    
    new_notes_layout.addWidget(QLabel("Choose where new notes will be placed:"))
    new_notes_layout.addWidget(same_deck_radio)
    new_notes_layout.addWidget(custom_deck_radio)
    new_notes_layout.addWidget(new_notes_combo)
    layout.addWidget(new_notes_group)

    # Buttons
    button_layout = QHBoxLayout()
    save_button = QPushButton('Save Changes')
    save_button.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; border: none; padding: 10px 20px; border-radius: 4px; font-weight: bold; } QPushButton:hover { background-color: #45a049; }")
    cancel_button = QPushButton('Cancel')
    cancel_button.setStyleSheet("QPushButton { background-color: #f44336; color: white; border: none; padding: 10px 20px; border-radius: 4px; } QPushButton:hover { background-color: #d32f2f; }")
    
    def save_changes():
        try:
            # Check if collection is available
            if not mw.col:
                showInfo("Anki collection is not available. Please try again.")
                return
            
            # Validate deck selection
            local_deck_name = local_deck_combo.currentText()
            if not local_deck_name:
                showInfo("Please select a local deck.")
                return
            
            # Save local deck
            local_deck_id = mw.col.decks.id(local_deck_name)
            details["deckId"] = local_deck_id
            
            # Save new notes deck
            if same_deck_radio.isChecked():
                # Remove new_notes_home_deck if it exists
                if "new_notes_home_deck" in details:
                    del details["new_notes_home_deck"]
            else:
                # Validate new notes deck selection
                new_notes_deck_name = new_notes_combo.currentText()
                if not new_notes_deck_name:
                    showInfo("Please select a new notes deck.")
                    return
                
                # Set custom new notes deck
                new_notes_deck_id = mw.col.decks.id(new_notes_deck_name)
                details["new_notes_home_deck"] = new_notes_deck_id
            
            # Save to config
            mw.addonManager.writeConfig(__name__, strings_data)
            dialog.accept()
            parent_dialog.accept()  # Close parent dialog
            on_edit_list()  # Reopen with updated data
            
        except Exception as e:
            showInfo(f"Error saving changes: {str(e)}")
            logger.exception("Error in save_changes")  # For debugging
    
    save_button.clicked.connect(save_changes)
    cancel_button.clicked.connect(dialog.reject)
    
    button_layout.addWidget(save_button)
    button_layout.addWidget(cancel_button)
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

    # Check if collection is available
    if not mw.col:
        showInfo("Anki collection is not available. Please try again.")
        return

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
    dialog.setWindowTitle("📤 Publish Deck to AnkiCollab")
    #dialog.setMinimumSize(520, 600)  # Allow expansion to prevent cropping
    dialog.resize(520, 650)  # Set initial size but allow resizing

    # Check if collection is available
    if not mw.col:
        showInfo("Anki collection is not available. Please try again.")
        return

    # Theme-aware colors
    dark_mode = theme_manager.night_mode
    
    # Beautiful color scheme that adapts to theme
    if dark_mode:
        colors = {
            'background': '#1e1e1e',
            'surface': '#2d2d2d', 
            'primary': '#64B5F6',  # Lighter blue for dark mode
            'primary_dark': '#2196F3',
            'primary_light': '#E3F2FD',
            'secondary': '#FF9800',
            'secondary_dark': '#F57C00', 
            'accent': '#4CAF50',
            'accent_dark': '#388E3C',
            'text': '#ffffff',
            'text_secondary': '#b0b0b0',
            'border': '#404040',
            'hover': '#3d3d3d',
            'error': '#F44336'
        }
    else:
        colors = {
            'background': '#fafafa',
            'surface': '#ffffff',
            'primary': '#2196F3', 
            'primary_dark': '#1976D2',
            'primary_light': '#E3F2FD',
            'secondary': '#FF9800',
            'secondary_dark': '#F57C00',
            'accent': '#4CAF50', 
            'accent_dark': '#388E3C',
            'text': '#212121',
            'text_secondary': '#757575',
            'border': '#e0e0e0',
            'hover': '#f5f5f5',
            'error': '#F44336'
        }

    # Main layout with proper spacing
    main_layout = QVBoxLayout()
    dialog.setLayout(main_layout)
    main_layout.setSpacing(12)  # Reduced spacing to fit better
    main_layout.setContentsMargins(20, 20, 20, 20)  # Reduced margins

    # Header section with beautiful styling
    header_widget = QWidget()
    header_layout = QVBoxLayout(header_widget)
    header_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    header_layout.setContentsMargins(0, 0, 0, 10)
    
    title_label = QLabel("📤 Share Your Knowledge with the World")
    title_label.setStyleSheet(f"""
        QLabel {{
            font-size: 18px;
            font-weight: bold;
            color: {colors['primary']};
            margin-bottom: 5px;
        }}
    """)
    title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    
    subtitle_label = QLabel("Publish your deck to help other Anki users learn and grow")
    subtitle_label.setStyleSheet(f"""
        QLabel {{
            font-size: 12px;
            color: {colors['text_secondary']};
            margin-bottom: 10px;
        }}
    """)
    subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    subtitle_label.setWordWrap(True)
    
    header_layout.addWidget(title_label)
    header_layout.addWidget(subtitle_label)
    main_layout.addWidget(header_widget)

    # Deck selection section
    deck_section = QGroupBox("📚 Select Deck to Publish")
    deck_section.setStyleSheet(f"""
        QGroupBox {{
            font-weight: bold;
            font-size: 14px;
            color: {colors['text']};
            border: 2px solid {colors['border']};
            border-radius: 8px;
            margin-top: 15px;
            padding-top: 15px;
            background-color: {colors['surface']};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 15px;
            padding: 5px 10px;
            color: {colors['primary']};
            background: {colors['surface']};
        }}
    """)
    deck_layout = QVBoxLayout(deck_section)
    deck_layout.setSpacing(8)
    
    deck_help_label = QLabel("Choose the deck you want to share with the AnkiCollab community:")
    deck_help_label.setStyleSheet(f"color: {colors['text_secondary']}; font-size: 12px; margin-bottom: 8px;")
    deck_help_label.setWordWrap(True)
    deck_layout.addWidget(deck_help_label)
    
    deck_combo_box = QComboBox()
    deck_combo_box.setStyleSheet(f"""
        QComboBox {{
            padding: 10px 12px;
            border: 2px solid {colors['border']};
            border-radius: 6px;
            font-size: 14px;
            background-color: {colors['surface']};
            color: {colors['text']};
            min-height: 18px;
        }}
        QComboBox:focus {{
            border-color: {colors['primary']};
            outline: none;
        }}
        QComboBox:hover {{
            background-color: {colors['hover']};
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 25px;
            border-left: 1px solid {colors['border']};
            border-top-right-radius: 6px;
            border-bottom-right-radius: 6px;
            background: {colors['hover']};
        }}
        QComboBox::down-arrow {{
            width: 0;
            height: 0;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 6px solid {colors['text']};
            margin: 0px 6px;
        }}
        QComboBox QAbstractItemView {{
            border: 1px solid {colors['border']};
            border-radius: 4px;
            background-color: {colors['surface']};
            color: {colors['text']};
            selection-background-color: {colors['primary']};
        }}
    """)
    
    decks = mw.col.decks.all_names_and_ids(include_filtered=False)
    deck_names = sorted([d.name for d in decks if "::" not in d.name and d.id != 1])
    if not deck_names:
        deck_combo_box.addItem("No suitable decks found")
        deck_combo_box.setEnabled(False)
    else:
        deck_combo_box.addItems(deck_names)
    
    deck_layout.addWidget(deck_combo_box)
    main_layout.addWidget(deck_section)

    # Author information section
    author_section = QGroupBox("👤 Author Information")
    author_section.setStyleSheet(f"""
        QGroupBox {{
            font-weight: bold;
            font-size: 14px;
            color: {colors['text']};
            border: 2px solid {colors['border']};
            border-radius: 8px;
            margin-top: 15px;
            padding-top: 15px;
            background-color: {colors['surface']};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 15px;
            padding: 5px 10px;
            color: {colors['secondary']};
            background: {colors['surface']};
        }}
    """)
    author_layout = QVBoxLayout(author_section)
    author_layout.setSpacing(8)
    
    username_help_label = QLabel("Your username will be the deck owner:")
    username_help_label.setStyleSheet(f"color: {colors['text_secondary']}; font-size: 12px; margin-bottom: 8px;")
    username_help_label.setWordWrap(True)
    author_layout.addWidget(username_help_label)
    
    username_field = QLineEdit()
    username_field.setPlaceholderText("Enter your AnkiCollab username...")
    username_field.setStyleSheet(f"""
        QLineEdit {{
            padding: 10px 12px;
            border: 2px solid {colors['border']};
            border-radius: 6px;
            font-size: 14px;
            background-color: {colors['surface']};
            color: {colors['text']};
        }}
        QLineEdit:focus {{
            border-color: {colors['secondary']};
            outline: none;
        }}
        QLineEdit:hover {{
            background-color: {colors['hover']};
        }}
    """)
    
    if auth_manager.is_logged_in():
        # TODO: Add username retrieval logic if available
        # username = auth_manager.get_username()
        # if username:
        #    username_field.setText(username)
        #    username_field.setReadOnly(True)
        pass 
    
    author_layout.addWidget(username_field)
    main_layout.addWidget(author_section)

    # Legal declarations section
    legal_section = QGroupBox("⚖️ Legal Declarations")
    legal_section.setStyleSheet(f"""
        QGroupBox {{
            font-weight: bold;
            font-size: 14px;
            color: {colors['text']};
            border: 2px solid {colors['border']};
            border-radius: 8px;
            margin-top: 15px;
            padding-top: 15px;
            background-color: {colors['surface']};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 15px;
            padding: 5px 10px;
            color: {colors['error']};
            background: {colors['surface']};
        }}
    """)
    legal_layout = QVBoxLayout(legal_section)
    legal_layout.setSpacing(15)  # Increased spacing between items

    # Copyright disclaimer
    disclaimer_container = QWidget()
    disclaimer_layout = QHBoxLayout(disclaimer_container)
    disclaimer_layout.setContentsMargins(0, 0, 0, 0)
    disclaimer_layout.setSpacing(12)  # Increased spacing to prevent overlap
    disclaimer_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    
    disclaimer_checkbox = QCheckBox()
    disclaimer_checkbox.setFixedSize(20, 20)  # Fixed size to prevent issues
    disclaimer_checkbox.setStyleSheet(f"""
        QCheckBox::indicator {{
            width: 18px;
            height: 18px;
        }}
        QCheckBox::indicator:unchecked {{
            border: 2px solid {colors['border']};
            border-radius: 3px;
            background: {colors['surface']};
        }}
        QCheckBox::indicator:checked {{
            border: 2px solid {colors['accent']};
            border-radius: 3px;
            background: {colors['accent']};
        }}
    """)
    
    disclaimer_text = QLabel("""I declare under penalty of perjury that the material I am sharing is 
entirely my own work, or I have obtained a license from the 
intellectual property holder(s) to share it on AnkiCollab.""")
    disclaimer_text.setWordWrap(True)
    disclaimer_text.setStyleSheet(f"""
        QLabel {{
            color: {colors['text']};
            font-size: 13px;
            line-height: 1.4;
        }}
    """)
    disclaimer_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    
    disclaimer_layout.addWidget(disclaimer_checkbox)
    disclaimer_layout.addWidget(disclaimer_text, 1)
    legal_layout.addWidget(disclaimer_container)

    # Terms and Privacy Policy
    terms_container = QWidget()
    terms_layout = QHBoxLayout(terms_container)
    terms_layout.setContentsMargins(0, 0, 0, 0)
    terms_layout.setSpacing(12)  # Increased spacing to prevent overlap
    terms_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    
    terms_checkbox = QCheckBox()
    terms_checkbox.setFixedSize(20, 20)  # Fixed size to prevent issues
    terms_checkbox.setStyleSheet(f"""
        QCheckBox::indicator {{
            width: 18px;
            height: 18px;
        }}
        QCheckBox::indicator:unchecked {{
            border: 2px solid {colors['border']};
            border-radius: 3px;
            background: {colors['surface']};
        }}
        QCheckBox::indicator:checked {{
            border: 2px solid {colors['accent']};
            border-radius: 3px;
            background: {colors['accent']};
        }}
    """)
    
    terms_text_widget = QWidget()
    terms_text_layout = QHBoxLayout(terms_text_widget)
    terms_text_layout.setContentsMargins(0, 0, 0, 0)
    terms_text_layout.setSpacing(5)
    
    terms_text = QLabel("I agree to the")
    terms_text.setStyleSheet(f"color: {colors['text']}; font-size: 13px;")
    
    # Use theme-aware link colors
    link_color = colors['primary']
    terms_link = QLabel(f'<a href="https://ankicollab.com/terms" style="color: {link_color}; text-decoration: none;">Terms of Service</a>')
    terms_link.setOpenExternalLinks(True)
    terms_link.setStyleSheet("font-size: 13px;")
    
    and_text = QLabel("and")
    and_text.setStyleSheet(f"color: {colors['text']}; font-size: 13px;")
    
    privacy_link = QLabel(f'<a href="https://ankicollab.com/privacy" style="color: {link_color}; text-decoration: none;">Privacy Policy</a>')
    privacy_link.setOpenExternalLinks(True)
    privacy_link.setStyleSheet("font-size: 13px;")
    
    terms_text_layout.addWidget(terms_text)
    terms_text_layout.addWidget(terms_link)
    terms_text_layout.addWidget(and_text)
    terms_text_layout.addWidget(privacy_link)
    terms_text_layout.addStretch()
    
    terms_layout.addWidget(terms_checkbox)
    terms_layout.addWidget(terms_text_widget, 1)
    legal_layout.addWidget(terms_container)
    
    main_layout.addWidget(legal_section)

    # Action buttons section
    button_section = QWidget()
    button_layout = QHBoxLayout(button_section)
    button_layout.setContentsMargins(0, 20, 0, 0)  # Top margin for spacing
    
    # Cancel button with theme-aware styling
    cancel_button = QPushButton("Cancel")
    cancel_button.setStyleSheet(f"""
        QPushButton {{
            background-color: {colors['text_secondary']};
            color: {colors['surface']};
            border: 2px solid {colors['text_secondary']};
            padding: 12px 24px;
            border-radius: 6px;
            font-size: 14px;
            font-weight: bold;
            min-width: 100px;
        }}
        QPushButton:hover {{
            background-color: {colors['border']};
            border-color: {colors['border']};
        }}
        QPushButton:pressed {{
            background-color: {colors['text']};
        }}
    """)
    cancel_button.clicked.connect(dialog.reject)
    
    # Publish button with beautiful gradient styling
    publish_button = QPushButton("Publish Deck")
    if dark_mode:
        publish_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {colors['accent']};
                color: white;
                border: 2px solid {colors['accent']};
                padding: 12px 24px;
                border-radius: 6px;
                font-size: 14px;
                font-weight: bold;
                min-width: 140px;
            }}
            QPushButton:hover {{
                background-color: {colors['accent_dark']};
                border-color: {colors['accent_dark']};
            }}
            QPushButton:pressed {{
                background-color: #2E7D32;
                border-color: #2E7D32;
            }}
            QPushButton:disabled {{
                background-color: {colors['border']};
                color: {colors['text_secondary']};
                border-color: {colors['border']};
            }}
        """)
    else:
        publish_button.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 {colors['accent']}, stop: 1 {colors['accent_dark']});
                color: white;
                border: none;
                padding: 12px 24px;
                border-radius: 6px;
                font-size: 14px;
                font-weight: bold;
                min-width: 140px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 {colors['accent_dark']}, stop: 1 #2E7D32);
            }}
            QPushButton:pressed {{
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #2E7D32, stop: 1 #1B5E20);
            }}
            QPushButton:disabled {{
                background: {colors['border']};
                color: {colors['text_secondary']};
            }}
        """)

    def validate_and_enable_publish():
        """Enable publish button only when all requirements are met"""
        deck_selected = bool(deck_combo_box.currentText() and deck_combo_box.currentText() != "No suitable decks found")
        username_filled = bool(username_field.text().strip())
        disclaimer_checked = disclaimer_checkbox.isChecked()
        terms_checked = terms_checkbox.isChecked()
        
        all_valid = deck_selected and username_filled and disclaimer_checked and terms_checked
        publish_button.setEnabled(all_valid)
        
        if all_valid:
            publish_button.setToolTip("Ready to publish your deck!")
        else:
            missing = []
            if not deck_selected: missing.append("deck selection")
            if not username_filled: missing.append("username")
            if not disclaimer_checked: missing.append("copyright declaration")
            if not terms_checked: missing.append("terms agreement")
            publish_button.setToolTip(f"Please complete: {', '.join(missing)}")

    # Connect validation to all inputs
    deck_combo_box.currentTextChanged.connect(validate_and_enable_publish)
    username_field.textChanged.connect(validate_and_enable_publish)
    disclaimer_checkbox.toggled.connect(validate_and_enable_publish)
    terms_checkbox.toggled.connect(validate_and_enable_publish)
    
    # Initial validation
    validate_and_enable_publish()

    def on_publish_button_clicked():
        if not disclaimer_checkbox.isChecked() or not terms_checkbox.isChecked():
            showInfo(
                "You must agree to both the copyright declaration and the terms/privacy policy to publish your deck.",
                parent=dialog
            )
            return

        # Check if collection is available
        if not mw.col:
            showInfo("Anki collection is not available. Please try again.", parent=dialog)
            return

        selected_deck_name = deck_combo_box.currentText()
        username = username_field.text().strip()

        if not selected_deck_name or selected_deck_name == "No suitable decks found":
            showInfo("Please select a valid deck.", parent=dialog)
            return
        if not username:
            showInfo("Please enter your username.", parent=dialog)
            return

        deck_id = mw.col.decks.id(selected_deck_name)
        if not deck_id:
            showInfo(f"Could not find deck ID for '{selected_deck_name}'.", parent=dialog)
            return

        try:
            # Show confirmation dialog
            if askUser(
                f"Are you ready to publish '{selected_deck_name}' to AnkiCollab?\n\n"
                f"Author: {username}\n"
                f"This will make your deck available to the community.",
                title="Confirm Publication"
            ):
                handle_export(deck_id, username)            
                dialog.accept()
                
        except Exception as e:
            showInfo(f"An error occurred during publishing: {e}", parent=dialog, title="Publication Error")
            logger.exception("Publishing error")

    publish_button.clicked.connect(on_publish_button_clicked)

    button_layout.addStretch()
    button_layout.addWidget(cancel_button)
    button_layout.addWidget(publish_button)
    
    main_layout.addWidget(button_section)

    # Set dialog properties with beautiful theme-aware background
    dialog.setStyleSheet(f"""
        QDialog {{
            background-color: {colors['background']};
            color: {colors['text']};
        }}
    """)

    dialog.exec()

def show_global_settings_dialog(parent_dialog):
    """Show global settings dialog (formerly deck structure settings)"""
    dialog = QDialog(parent_dialog)
    dialog.setWindowTitle('Global Settings')
    dialog.setMinimumSize(420, 420)
    layout = QVBoxLayout()
    dialog.setLayout(layout)

    # Header
    header_label = QLabel("\ud83d\udcdd Global Settings")
    header_label.setStyleSheet("font-size: 16px; font-weight: bold; margin: 10px 0; color: #2196F3;")
    header_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(header_label)

    # Get current settings
    strings_data = mw.addonManager.getConfig(__name__)
    if not strings_data:
        strings_data = {}
    if "settings" not in strings_data:
        strings_data["settings"] = {}
    settings = strings_data["settings"]

    # Ensure legacy configs default to preserving deck structure
    if settings.get("preserve_deck_structure") is False:
        settings["preserve_deck_structure"] = True
        mw.addonManager.writeConfig(__name__, strings_data)

    # Global config checkboxes
    global_group = QGroupBox("General Settings")
    global_layout = QVBoxLayout(global_group)
    pull_on_startup_cb = QCheckBox("Update Decks on startup")
    pull_on_startup_cb.setChecked(bool(settings.get("pull_on_startup", False)))
    pull_on_startup_cb.setToolTip("Automatically check for updates from AnkiCollab when Anki starts.")
    suspend_new_cards_cb = QCheckBox("Automatically suspend new Cards")
    suspend_new_cards_cb.setChecked(bool(settings.get("suspend_new_cards", False)))
    suspend_new_cards_cb.setToolTip("Automatically suspend new cards imported from subscriptions so they won't enter your review queue until you enable them.")
    move_cards_cb = QCheckBox("Do not move Cards automatically")
    move_cards_cb.setChecked(bool(settings.get("auto_move_cards", False)))
    move_cards_cb.setToolTip("Prevent the add-on from automatically moving cards between decks or positions when syncing cloud changes.")
    keep_empty_subdecks_cb = QCheckBox("Keep empty subdecks")
    keep_empty_subdecks_cb.setChecked(bool(settings.get("keep_empty_subdecks", False)))
    keep_empty_subdecks_cb.setToolTip("Skip cleanup of empty subdecks after imports. Enable if you rely on placeholder decks.")
    auto_approve_cb = QCheckBox("Auto-approve changes (maintainer only)")
    auto_approve_cb.setChecked(bool(auth_manager.get_auto_approve()))
    auto_approve_cb.setToolTip("Automatically approve outgoing changes for your decks. Only works if you are a maintainer.")
    error_reporting_cb = QCheckBox("Send anonymous error reports (recommended)")
    error_reporting_cb.setChecked(bool(settings.get("error_reporting_enabled", False)))
    error_reporting_cb.setToolTip("Send anonymized crash and error reports to help improve the add-on.")
    
    global_layout.addWidget(pull_on_startup_cb)
    global_layout.addWidget(suspend_new_cards_cb)
    global_layout.addWidget(move_cards_cb)
    global_layout.addWidget(keep_empty_subdecks_cb)
    global_layout.addWidget(auto_approve_cb)
    
    # Add to group
    global_layout.addWidget(error_reporting_cb)
    layout.addWidget(global_group)

    # Info section
    info_label = QLabel("\u2139\ufe0f These settings apply globally to all your subscriptions. Changes take effect on the next import.")
    info_label.setWordWrap(True)
    info_label.setStyleSheet("background-color: #E3F2FD; border: 1px solid #BBDEFB; border-radius: 4px; padding: 10px; margin: 10px 0; color: #1976D2;")
    layout.addWidget(info_label)

    # Buttons
    button_layout = QHBoxLayout()
    save_button = QPushButton('Save Settings')
    save_button.setStyleSheet("QPushButton { background-color: #2196F3; color: white; border: none; padding: 10px 20px; border-radius: 4px; font-weight: bold; } QPushButton:hover { background-color: #1976D2; }")
    cancel_button = QPushButton('Cancel')
    cancel_button.setStyleSheet("QPushButton { background-color: #607D8B; color: white; border: none; padding: 10px 20px; border-radius: 4px; } QPushButton:hover { background-color: #455A64; }")

    def save_global_settings():
        settings["preserve_deck_structure"] = True
        settings["pull_on_startup"] = pull_on_startup_cb.isChecked()
        settings["suspend_new_cards"] = suspend_new_cards_cb.isChecked()
        settings["auto_move_cards"] = move_cards_cb.isChecked()
        settings["keep_empty_subdecks"] = keep_empty_subdecks_cb.isChecked()
        settings["error_reporting_enabled"] = error_reporting_cb.isChecked()
        mw.addonManager.writeConfig(__name__, strings_data)
        auth_manager.set_auto_approve(auto_approve_cb.isChecked())
        # Apply telemetry setting immediately
        try:
            init_sentry()
        except Exception:
            pass
        showInfo("Global settings saved!")
        dialog.accept()

    save_button.clicked.connect(save_global_settings)
    cancel_button.clicked.connect(dialog.reject)

    button_layout.addWidget(save_button)
    button_layout.addWidget(cancel_button)
    layout.addLayout(button_layout)

    dialog.exec()
    
def on_push_all_stats_action():
    decks = DeckManager()

    for deck_hash, details in decks:
        if details.get("stats_enabled", False):
            # Only upload stats if the user wants to share them
            share_data, _ = wants_to_share_stats(deck_hash)
            if share_data:
                rh = ReviewHistory(deck_hash)
                op = QueryOp(
                    parent=mw,
                    op=lambda _: rh.upload_review_history(0),
                    success=on_stats_upload_done
                )
                op.with_progress(
                    "Uploading Review History..."
                ).run_in_background()
                update_stats_timestamp(deck_hash)
  
def open_community_site():
    webbrowser.open('https://discord.gg/9x4DRxzqwM')

def open_support_site():
    #webbrowser.open('https://www.ankicollab.com/leavereview')
    rated_dialog = RateAddonDialog(mw)
    result = rated_dialog.exec()

def open_website():
    webbrowser.open('https://www.ankicollab.com/')

def on_login_manager_btn():
    if auth_manager.is_logged_in():
        force_logout(False)
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
        "pull_on_startup": False,
        "suspend_new_cards": False,
        "auto_move_cards": False, # Note: Action text is "Do not move", so False means "Do move"
    "keep_empty_subdecks": False,
        "rated_addon": False,
        "last_ratepls": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "pull_counter": 0,
        "push_counter": 0,
        # Error reporting (Sentry)
        "error_reporting_enabled": False,
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
    push_all_stats_action.setVisible(logged_in)    
    media_import_action.setVisible(logged_in)

    login_manager_action.setText("Logout" if logged_in else "Login")

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
    collab_menu.addAction(push_all_stats_action)
    collab_menu.addAction(media_import_action)
    collab_menu.addSeparator()
    collab_menu.addMenu(links_menu)
    collab_menu.addSeparator()
    collab_menu.addAction(login_manager_action)

    links_menu.addAction(community_action)
    links_menu.addAction(website_action)
    links_menu.addAction(donation_action) # Review link

    edit_list_action.triggered.connect(on_edit_list)
    push_deck_action.triggered.connect(on_push_deck_action)
    push_all_stats_action.triggered.connect(on_push_all_stats_action)
    pull_changes_action.triggered.connect(async_update)
    media_import_action.triggered.connect(on_media_btn)
    website_action.triggered.connect(open_website)
    donation_action.triggered.connect(open_support_site)
    community_action.triggered.connect(open_community_site)
    login_manager_action.triggered.connect(on_login_manager_btn)

    update_ui_for_login_state()
