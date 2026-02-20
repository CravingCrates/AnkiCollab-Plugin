
import webbrowser
import aqt
import aqt.utils
import anki

import json
import requests

from aqt.qt import *
from aqt import mw
from aqt.theme import theme_manager

from .auth_manager import *
from .var_defs import API_BASE_URL
from .ui.colors import get_colors, get_button_style, get_dialog_style, get_input_style

def get_local_deck_from_hash(input_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if hash == input_hash:
                return mw.col.decks.name(details["deckId"])
    return "None"

#legacy
def get_login_token():
    return auth_manager.get_token()

def set_rated_true():
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None and "settings" in strings_data:
        if "rated_addon" in strings_data["settings"]:
            strings_data["settings"]["rated_addon"] = True
            mw.addonManager.writeConfig(__name__, strings_data)

class ChangelogDialog(QDialog):
    def __init__(self, changelog, deck_hash):
        super().__init__()
        local_name = get_local_deck_from_hash(deck_hash)
        self.setWindowTitle(f"AnkiCollab - Changelog for {local_name}")
        self.setModal(True)
        
        colors = get_colors()
        self.setStyleSheet(get_dialog_style())

        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # Header - neutral, not colored
        header = QLabel("Update Available")
        header.setStyleSheet(f"font-size: 15px; font-weight: 500; color: {colors['text_primary']};")
        layout.addWidget(header)

        label = QLabel("The following changes are available:")
        label.setStyleSheet(f"color: {colors['text_secondary']}; margin-bottom: 8px;")
        layout.addWidget(label)

        changelog_text = QTextBrowser()
        changelog_text.setStyleSheet(f"""
            QTextBrowser {{
                border: 1px solid {colors['border']};
                border-radius: 5px;
                padding: 10px;
                background-color: {colors['surface']};
                color: {colors['text_primary']};
            }}
        """)
        
        if not changelog:
            changelog = "No changelog provided for this update."
            
        changelog_text.setPlainText(changelog)
        layout.addWidget(changelog_text)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        skip_button = QPushButton("Skip This Version")
        skip_button.setStyleSheet(get_button_style('neutral'))
        
        later_button = QPushButton("Remind Me Later")
        later_button.setStyleSheet(get_button_style('neutral'))
        
        install_button = QPushButton("Install Update")
        install_button.setStyleSheet(get_button_style('success'))
        
        button_layout.addWidget(skip_button)
        button_layout.addWidget(later_button)
        button_layout.addWidget(install_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)

        install_button.clicked.connect(self.accept)
        later_button.clicked.connect(self.reject)
        skip_button.clicked.connect(self.skip_update)

        self.adjustSize()

    def skip_update(self):
        self.done(2)
        

class OptionalTagsDialog(QDialog):
    checkboxes = {}
    
    def __init__(self, old_tags, new_tags):
        super().__init__()
        
        colors = get_colors()
        self.setStyleSheet(get_dialog_style())
        
        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        self.setWindowTitle("AnkiCollab - Optional Tags")
        
        header = QLabel("Optional Tags")
        header.setStyleSheet(f"font-size: 15px; font-weight: 500; color: {colors['text_primary']};")
        layout.addWidget(header)
        
        label = QLabel("Select which optional tags you want to include:")
        label.setStyleSheet(f"color: {colors['text_secondary']}; margin-bottom: 8px;")
        layout.addWidget(label)
        
        # Checkbox container
        checkbox_container = QWidget()
        checkbox_layout = QVBoxLayout(checkbox_container)
        checkbox_layout.setSpacing(6)
        checkbox_layout.setContentsMargins(0, 0, 0, 0)
        
        for item in new_tags:
            checkbox = QCheckBox(item)
            checkbox.setStyleSheet(f"color: {colors['text_primary']}; padding: 4px;")
            checkbox.setChecked(old_tags.get(item, False))
            self.checkboxes[item] = checkbox
            checkbox_layout.addWidget(checkbox)
        
        layout.addWidget(checkbox_container)
        layout.addStretch()

        button = QPushButton('Save')
        button.setStyleSheet(get_button_style('success'))
        button.clicked.connect(lambda: self.close())
        layout.addWidget(button)

        self.setLayout(layout)
        self.show()

    def get_selected_tags(self):
        result = {}
        for item in self.checkboxes:
            result[item] = self.checkboxes[item].isChecked()

        return result
    
    
# Create a new Login Dialog that allows the user to enter their username and password
class LoginDialog(QDialog):
    def __init__(self, parent=None):
        super(LoginDialog, self).__init__(parent)
        self.setWindowTitle("AnkiCollab - Login")
        self.setModal(True)
        self.resize(320, 260)

        colors = get_colors()
        self.setStyleSheet(get_dialog_style())

        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(25, 25, 25, 25)

        # Title
        title = QLabel("Login to AnkiCollab")
        title.setStyleSheet(f"font-size: 16px; font-weight: 500; color: {colors['text_primary']}; margin-bottom: 10px;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        # Username field
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")
        self.username_input.setStyleSheet(get_input_style())
        
        # Password field
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setStyleSheet(get_input_style())
        
        layout.addWidget(self.username_input)
        layout.addWidget(self.password_input)

        # Buttons
        button_layout = QHBoxLayout()
        
        login_button = QPushButton("Login")
        login_button.setToolTip("Sign in with your AnkiCollab account")
        login_button.setStyleSheet(get_button_style('success'))
        
        cancel_button = QPushButton("Cancel")
        cancel_button.setStyleSheet(get_button_style('neutral'))
        
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(login_button)
        layout.addLayout(button_layout)
        
        # Signup link
        signup_link = QLabel(f'<a href="#" style="color: {colors["primary"]}; text-decoration: underline;">Don\'t have an account? Sign up here</a>')
        signup_link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        signup_link.setStyleSheet(f"color: {colors['primary']}; font-size: 12px; margin-top: 10px;")
        signup_link.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(signup_link)

        # Connect events
        login_button.clicked.connect(self.login)
        cancel_button.clicked.connect(self.reject)
        signup_link.linkActivated.connect(self.open_signup)
        
        # Enter key handling
        self.password_input.returnPressed.connect(self.login)

    def open_signup(self):
        webbrowser.open('https://ankicollab.com/signup')

    def login(self):
        username = self.username_input.text()
        password = self.password_input.text()
        
        if not username or not password:
            aqt.utils.showInfo("Please enter both your username and password to continue.")
            return
        
        payload = {
            'username': username,
            'password': password
        }
        
        try:
            response = requests.post(f"{API_BASE_URL}/login", data=payload, timeout=10)

            if response.status_code == 200:
                # Parse the JSON response
                auth_data = response.json()
                
                if auth_manager.store_login_result(auth_data):
                    self.done(0)
                else:
                    msg_box = QMessageBox()
                    msg_box.setText("Invalid authentication response from server.")
                    msg_box.exec()
            else:
                error_message = f"Login failed: {response.text}"
                aqt.utils.showInfo(error_message)
        except Exception as e:
            error_message = f"Error connecting to server: {str(e)}"
            aqt.utils.showInfo(error_message)
            
class AddChangelogDialog(QDialog):
    def __init__(self, deck_hash, parent=None):
        super().__init__()
        self.setWindowTitle("AnkiCollab - Add Changelog")
        self.setModal(True)
        self.resize(400, 220)

        colors = get_colors()
        self.setStyleSheet(get_dialog_style())
        
        self.deck_hash = deck_hash

        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        header = QLabel("Add Changelog Entry")
        header.setStyleSheet(f"font-size: 15px; font-weight: 500; color: {colors['text_primary']};")
        layout.addWidget(header)

        label = QLabel("Describe the changes you're publishing:")
        label.setStyleSheet(f"color: {colors['text_secondary']}; margin-bottom: 4px;")
        layout.addWidget(label)

        self.changelog_input = QTextEdit()
        self.changelog_input.setStyleSheet(f"""
            QTextEdit {{
                border: 1px solid {colors['border']};
                border-radius: 5px;
                padding: 8px;
                background-color: {colors['surface']};
                color: {colors['text_primary']};
            }}
            QTextEdit:focus {{
                border-color: {colors['border_focus']};
            }}
        """)
        layout.addWidget(self.changelog_input)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        cancel_button = QPushButton("Cancel")
        cancel_button.setStyleSheet(get_button_style('neutral'))
        cancel_button.clicked.connect(self.reject)
        
        publish_button = QPushButton("Publish")
        publish_button.setStyleSheet(get_button_style('success'))
        publish_button.clicked.connect(self.publish)
        
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(publish_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def publish(self):
        changelog_text = self.changelog_input.toPlainText()
        if not changelog_text:
            QMessageBox.warning(self, "Error", "Please enter a changelog message.")
            return

        payload = {
            'deck_hash': self.deck_hash,
            'changelog': changelog_text,
            'token': get_login_token()
        }

        response = requests.post(f"{API_BASE_URL}/submitChangelog", json=payload, timeout=30)
        if response.status_code == 200:
            QMessageBox.information(self, "Information", response.text)
        else:
            QMessageBox.warning(self, "Error", "An unknown error occurred while publishing the changelog.")

        self.accept()
       

class DeletedNotesDialog(QDialog):
    def __init__(self, deleted_notes, deck_hash, parent=None):
        super().__init__(parent)
        local_name = get_local_deck_from_hash(deck_hash)
        self.setWindowTitle(f"AnkiCollab - Notes Removed from {local_name}")
        self.setModal(True)

        colors = get_colors()
        self.setStyleSheet(get_dialog_style())

        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        header = QLabel("Notes Removed")
        header.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {colors['warning']};")
        layout.addWidget(header)

        label = QLabel("The maintainers removed the following notes from the deck. How would you like to proceed?")
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {colors['text_secondary']}; margin-bottom: 8px;")
        layout.addWidget(label)

        scroll_area = QScrollArea()
        scroll_area.setStyleSheet(f"""
            QScrollArea {{
                border: 1px solid {colors['border']};
                border-radius: 5px;
                background-color: {colors['surface']};
            }}
        """)

        deleted_notes_text = QTextBrowser()
        deleted_notes_text.setMaximumHeight(200)
        deleted_notes_text.setStyleSheet(f"""
            QTextBrowser {{
                border: none;
                background-color: {colors['surface']};
                color: {colors['text_primary']};
            }}
        """)

        deleted_notes_str = "\n".join(map(str, deleted_notes))
        deleted_notes_text.setPlainText(deleted_notes_str)

        scroll_area.setWidget(deleted_notes_text)
        scroll_area.setWidgetResizable(True)
        layout.addWidget(scroll_area)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        keep_button = QPushButton("Keep Notes")
        keep_button.setStyleSheet(get_button_style('neutral'))
        keep_button.clicked.connect(lambda: self.done(2))
        
        review_button = QPushButton("Review in Browser")
        review_button.setStyleSheet(get_button_style('neutral'))
        review_button.clicked.connect(self.reject)
        
        delete_button = QPushButton("Delete Notes")
        delete_button.setStyleSheet(get_button_style('danger'))
        delete_button.clicked.connect(self.accept)
        
        button_layout.addWidget(keep_button)
        button_layout.addWidget(review_button)
        button_layout.addWidget(delete_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)
        self.adjustSize()
        
class AskShareStatsDialog(QDialog):
    def __init__(self, deck_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Help Improve the Deck")
        
        colors = get_colors()
        self.setStyleSheet(get_dialog_style())

        self.layout = QVBoxLayout(self)
        self.layout.setSpacing(12)
        self.layout.setContentsMargins(20, 20, 20, 20)
        
        self.deck_name = deck_name
        
        header = QLabel("Share Review Data")
        header.setStyleSheet(f"font-size: 15px; font-weight: 500; color: {colors['text_primary']};")
        self.layout.addWidget(header)
        
        self.message = QLabel(f"The maintainers of '{self.deck_name}' would like to use anonymized review data to improve the deck. Would you like to share your stats?")
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color: {colors['text_primary']}; margin-bottom: 8px;")
        self.layout.addWidget(self.message)

        self.checkbox = QCheckBox("Remember my decision")
        self.checkbox.setStyleSheet(f"color: {colors['text_secondary']};")
        self.layout.addWidget(self.checkbox)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        no_button = QPushButton("No Thanks")
        no_button.setStyleSheet(get_button_style('neutral'))
        no_button.clicked.connect(self.reject)
        
        yes_button = QPushButton("Yes, Share")
        yes_button.setStyleSheet(get_button_style('success'))
        yes_button.clicked.connect(self.accept)
        
        button_layout.addWidget(no_button)
        button_layout.addWidget(yes_button)
        self.layout.addLayout(button_layout)

    def isChecked(self):
        return self.checkbox.isChecked()
        
class RateAddonDialog(QDialog):
    def __init__(self, parent=None):
        super(RateAddonDialog, self).__init__(parent)
        self.setWindowTitle("A Quick Note")
        self.setModal(True)
        self.setMinimumWidth(340)
        
        colors = get_colors()
        self.setStyleSheet(get_dialog_style())

        # Main layout
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # Personal greeting
        greeting = QLabel("Hey there!")
        greeting.setStyleSheet(f"""
            QLabel {{
                font-size: 18px;
                font-weight: 600;
                color: {colors['text_primary']};
            }}
        """)
        greeting.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(greeting)
        
        # Warm message
        message = QLabel(
            "Thanks for using AnkiCollab. We're a small team working to make "
            "studying together easier.\n\n"
            "If you have a moment, a quick rating on AnkiWeb helps others "
            "discover us. It really means a lot."
        )
        message.setStyleSheet(f"""
            QLabel {{
                color: {colors['text_secondary']};
                font-size: 13px;
                line-height: 1.5;
            }}
        """)
        message.setWordWrap(True)
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(message)
        
        # Add some breathing room
        layout.addSpacing(8)
        
        # Primary action - the one thing we're asking for
        love_button = QPushButton("Leave a Rating")
        love_button.setFixedHeight(36)
        love_button.setStyleSheet(get_button_style('success'))
        layout.addWidget(love_button)
        
        # Secondary options, less prominent
        secondary_layout = QHBoxLayout()
        
        help_button = QPushButton("Join Discord")
        help_button.setStyleSheet(get_button_style('neutral', 'small'))
        
        later_button = QPushButton("Not Now")
        later_button.setStyleSheet(get_button_style('neutral', 'small'))
        
        secondary_layout.addWidget(help_button)
        secondary_layout.addWidget(later_button)
        layout.addLayout(secondary_layout)

        # Connect events
        love_button.clicked.connect(self.love_it_button_click)
        help_button.clicked.connect(self.needs_work_button_click)
        later_button.clicked.connect(self.close)
        
        self.adjustSize()

    def love_it_button_click(self):
        self.close()
        webbrowser.open('https://ankiweb.net/shared/review/1957538407')
        set_rated_true()

    def needs_work_button_click(self):
        self.close()
        webbrowser.open('https://discord.gg/9x4DRxzqwM')


class ProtectFieldsDialog(QDialog):
    """Dialog for selecting which fields to protect from AnkiCollab updates."""
    
    def __init__(self, field_names: list, current_protected: list = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AnkiCollab - Protect Fields")
        self.setModal(True)
        self.setMinimumWidth(350)
        
        self.field_names = field_names
        self.current_protected = current_protected or []
        self.result_fields = None
        
        colors = get_colors()
        self.setStyleSheet(get_dialog_style())
        
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # Title
        title = QLabel("Protect Fields from Updates")
        title.setStyleSheet(f"font-size: 14px; font-weight: 500; color: {colors['text_primary']}; margin-bottom: 5px;")
        layout.addWidget(title)
        
        # Description
        desc = QLabel(
            "Select fields to protect from AnkiCollab updates.<br>"
            "Protected fields will not be overwritten when syncing."
        )
        desc.setStyleSheet(f"color: {colors['text_primary']}; font-size: 12px; margin-bottom: 10px;")
        desc.setWordWrap(True)
        layout.addWidget(desc)
        
        # Checkboxes for fields
        self.checkboxes = {}
        
        # "Select All" checkbox
        self.select_all_cb = QCheckBox("Select All Fields")
        self.select_all_cb.setStyleSheet(f"""
            QCheckBox {{
                color: {colors['text_primary']};
                font-weight: bold;
                padding: 5px;
            }}
        """)
        self.select_all_cb.stateChanged.connect(self._on_select_all_changed)
        layout.addWidget(self.select_all_cb)
        
        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background-color: {colors['border']};")
        layout.addWidget(sep)
        
        # Scroll area for field checkboxes
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(250)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                border: 1px solid {colors['border']};
                border-radius: 5px;
                background-color: {colors['surface']};
            }}
        """)
        
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(5)
        
        for field in field_names:
            cb = QCheckBox(field)
            cb.setStyleSheet(f"color: {colors['text_primary']}; padding: 3px;")
            cb.setChecked(field in self.current_protected)
            cb.stateChanged.connect(self._update_select_all_state)
            self.checkboxes[field] = cb
            scroll_layout.addWidget(cb)
        
        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)
        
        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"background-color: {colors['border']};")
        layout.addWidget(sep2)
        
        # Tags protection option
        self.protect_tags_cb = QCheckBox("Protect Tags")
        self.protect_tags_cb.setStyleSheet(f"""
            QCheckBox {{
                color: {colors['text_primary']};
                font-weight: bold;
                padding: 5px;
            }}
        """)
        self.protect_tags_cb.setChecked("Tags" in self.current_protected)
        layout.addWidget(self.protect_tags_cb)
        
        tags_desc = QLabel("Prevents tag changes from being overwritten during sync.")
        tags_desc.setStyleSheet(f"color: {colors['text_secondary']}; font-size: 11px; margin-left: 20px;")
        layout.addWidget(tags_desc)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(get_button_style('neutral'))
        cancel_btn.clicked.connect(self.reject)
        
        save_btn = QPushButton("Save")
        save_btn.setStyleSheet(get_button_style('success'))
        save_btn.clicked.connect(self._on_save)
        
        button_layout.addStretch()
        button_layout.addWidget(cancel_btn)
        button_layout.addWidget(save_btn)
        layout.addLayout(button_layout)
        
        # Update select all state based on current selection
        self._update_select_all_state()
    
    def _on_select_all_changed(self, state):
        """Handle Select All checkbox state change."""
        # Block signals to prevent recursive updates
        for cb in self.checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(state == Qt.CheckState.Checked.value)
            cb.blockSignals(False)
    
    def _update_select_all_state(self):
        """Update Select All checkbox based on individual selections."""
        all_checked = all(cb.isChecked() for cb in self.checkboxes.values())
        none_checked = not any(cb.isChecked() for cb in self.checkboxes.values())
        
        self.select_all_cb.blockSignals(True)
        if all_checked:
            self.select_all_cb.setCheckState(Qt.CheckState.Checked)
        elif none_checked:
            self.select_all_cb.setCheckState(Qt.CheckState.Unchecked)
        else:
            self.select_all_cb.setCheckState(Qt.CheckState.PartiallyChecked)
        self.select_all_cb.blockSignals(False)
    
    def _on_save(self):
        """Save the selected fields."""
        selected = [field for field, cb in self.checkboxes.items() if cb.isChecked()]
        protect_tags = self.protect_tags_cb.isChecked()
        self.result_fields = (selected, protect_tags)
        self.accept()
    
    def get_selected_fields(self):
        """Returns (list of selected field names, protect_tags bool) or None if cancelled."""
        return self.result_fields
