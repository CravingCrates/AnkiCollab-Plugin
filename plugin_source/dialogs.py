
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
        self.setWindowTitle(f"AnkiCollab - Changelog for Deck {local_name}")
        self.setModal(True)

        layout = QVBoxLayout()

        label = QLabel("The following changes are available:")
        layout.addWidget(label)

        changelog_text = QTextBrowser()
        
        if not changelog:
            changelog = "The maintainer left no changelog message for this update."
            
        changelog_text.setPlainText(changelog)
        layout.addWidget(changelog_text)

        button_box = QDialogButtonBox()
        install_button = button_box.addButton("Install Now", QDialogButtonBox.ButtonRole.AcceptRole)
        later_button = button_box.addButton("Decide Later", QDialogButtonBox.ButtonRole.RejectRole)
        skip_button = QPushButton("Skip this Update")
        button_box.addButton(skip_button, QDialogButtonBox.ButtonRole.ActionRole)

        layout.addWidget(button_box)

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
        layout = QVBoxLayout()

        self.setWindowTitle("AnkiCollab - Optional Tags")
        label = QLabel("You can subscribe to the following optional tags:")
        layout.addWidget(label)
        
        for item in new_tags:
            checkbox = QCheckBox(item)
            #set checked to the old value if it exists in the old tags, otherwise set it to false
            checkbox.setChecked(old_tags.get(item, False))
            self.checkboxes[item] = checkbox
            layout.addWidget(checkbox)

        button = QPushButton('Save')
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

        # Theme-aware colors
        dark_mode = theme_manager.night_mode
        
        if dark_mode:
            colors = {
                'background': '#2d2d2d',
                'surface': '#3d3d3d', 
                'primary': '#64B5F6',
                'accent': '#4CAF50',
                'text': '#ffffff',
                'border': '#555555'
            }
        else:
            colors = {
                'background': '#ffffff',
                'surface': '#f5f5f5',
                'primary': '#2196F3',
                'accent': '#4CAF50',
                'text': '#212121',
                'border': '#ddd'
            }

        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(25, 25, 25, 25)

        # Title
        title = QLabel("🔐 Login to AnkiCollab")
        title.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {colors['primary']}; margin-bottom: 10px;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        # Username field
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")
        self.username_input.setStyleSheet(f"""
            QLineEdit {{
                padding: 10px;
                border: 1px solid {colors['border']};
                border-radius: 5px;
                font-size: 14px;
                background-color: {colors['surface']};
                color: {colors['text']};
            }}
            QLineEdit:focus {{ border-color: {colors['primary']}; }}
        """)
        
        # Password field
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setStyleSheet(f"""
            QLineEdit {{
                padding: 10px;
                border: 1px solid {colors['border']};
                border-radius: 5px;
                font-size: 14px;
                background-color: {colors['surface']};
                color: {colors['text']};
            }}
            QLineEdit:focus {{ border-color: {colors['primary']}; }}
        """)
        
        layout.addWidget(self.username_input)
        layout.addWidget(self.password_input)

        # Buttons
        button_layout = QHBoxLayout()
        
        login_button = QPushButton("Login")
        login_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {colors['accent']};
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #45a049; }}
        """)
        
        cancel_button = QPushButton("Cancel")
        cancel_button.setStyleSheet(f"""
            QPushButton {{
                background-color: #666;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
            }}
            QPushButton:hover {{ background-color: #555; }}
        """)
        
        button_layout.addWidget(login_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
        
        # Signup link
        signup_link = QLabel('<a href="#" style="color: #64B5F6; text-decoration: underline;">Don\'t have an account? Sign up here</a>')
        signup_link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        signup_link.setStyleSheet(f"color: {colors['primary']}; font-size: 12px; margin-top: 10px;")
        signup_link.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(signup_link)
        
        self.setStyleSheet(f"QDialog {{ background-color: {colors['background']}; color: {colors['text']}; }}")

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
            aqt.utils.showInfo("Please enter a username and password.")
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
        self.resize(400, 200)

        self.deck_hash = deck_hash

        layout = QVBoxLayout()

        label = QLabel("Please enter the changelog message:")
        layout.addWidget(label)

        self.changelog_input = QTextEdit()
        layout.addWidget(self.changelog_input)

        button_box = QDialogButtonBox()
        publish_button = button_box.addButton("Publish", QDialogButtonBox.ButtonRole.AcceptRole)
        button_box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)

        layout.addWidget(button_box)

        self.setLayout(layout)

        publish_button.clicked.connect(self.publish)

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

        response = requests.post(f"{API_BASE_URL}/submitChangelog", json=payload)
        if response.status_code == 200:
            QMessageBox.information(self, "Information", response.text)
        else:
            QMessageBox.warning(self, "Error", "An unknown error occurred while publishing the changelog.")

        self.accept()
       

class DeletedNotesDialog(QDialog):
    def __init__(self, deleted_notes, deck_hash, parent=None):
        super().__init__(parent)
        local_name = get_local_deck_from_hash(deck_hash)
        self.setWindowTitle(f"AnkiCollab - Notes Removed from Deck {local_name}")
        self.setModal(True)

        layout = QVBoxLayout()

        label = QLabel("The maintainers removed the following notes from the deck. How do you want to proceed?\n")
        layout.addWidget(label)

        scroll_area = QScrollArea()

        deleted_notes_text = QTextBrowser()
        deleted_notes_text.setMaximumHeight(200)

        deleted_notes_str = "\n".join(map(str, deleted_notes))
        deleted_notes_text.setPlainText(deleted_notes_str)

        scroll_area.setWidget(deleted_notes_text)
        scroll_area.setWidgetResizable(True)  # Allow the QTextBrowser to expand within the scroll area

        layout.addWidget(scroll_area)

        button_box = QDialogButtonBox()
        delete_button = button_box.addButton("Delete Notes", QDialogButtonBox.ButtonRole.AcceptRole)
        open_in_browser_button = button_box.addButton("Show in Browser", QDialogButtonBox.ButtonRole.RejectRole)
        button_box.addButton("Keep Notes", QDialogButtonBox.ButtonRole.ActionRole)

        layout.addWidget(button_box)

        self.setLayout(layout)

        delete_button.clicked.connect(self.accept)
        open_in_browser_button.clicked.connect(self.reject)

        self.adjustSize()
        
class AskShareStatsDialog(QDialog):
    def __init__(self, deck_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Help Improve the Deck!")

        self.layout = QVBoxLayout(self)
        self.deck_name = deck_name
        self.message = QLabel(f"The maintainers of '{self.deck_name}' would like to use anonymized review data to improve the deck. Would you like to share your stats?")
        self.layout.addWidget(self.message)

        self.checkbox = QCheckBox("Remember my decision")
        self.layout.addWidget(self.checkbox)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)

    def isChecked(self):
        return self.checkbox.isChecked()
        
class RateAddonDialog(QDialog):
    def __init__(self, parent=None):
        super(RateAddonDialog, self).__init__(parent)
        self.setWindowTitle("Help AnkiCollab!")
        self.setModal(True)
        
        # Theme-aware colors
        dark_mode = theme_manager.night_mode
        
        if dark_mode:
            colors = {
                'background': '#2d2d2d',
                'primary': '#64B5F6',
                'accent': '#4CAF50',
                'text': '#ffffff'
            }
        else:
            colors = {
                'background': '#ffffff',
                'primary': '#2196F3',
                'accent': '#4CAF50',
                'text': '#212121'
            }

        # Main layout with reduced margins and spacing
        layout = QVBoxLayout(self)
        layout.setSpacing(12)  # Reduced from 20
        layout.setContentsMargins(20, 20, 20, 20)  # Reduced from 30

        # Smaller title
        title = QLabel("Hello from AnkiCollab!")
        title.setStyleSheet(f"""
            QLabel {{
                font-size: 16px;
                font-weight: bold;
                color: {colors['primary']};
                margin-bottom: 8px;
            }}
        """)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        # Message with reduced margins
        message = QLabel("Your review helps make AnkiCollab more discoverable and motivates us to keep improving.\n\n"
                        "If you're enjoying our add-on, please rate us. If you have any issues, let us know.")
        message.setStyleSheet(f"""
            QLabel {{
                color: {colors['text']};
                font-size: 13px;
                margin-bottom: 10px;
                line-height: 1.3;
            }}
        """)
        message.setWordWrap(True)
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(message)
        
        # Smaller buttons
        love_button = QPushButton("💖 Love it? Rate us!")
        love_button.setFixedHeight(32)  # Reduced from 40
        love_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {colors['accent']};
                color: white;
                border: none;
                padding: 6px 16px;
                border-radius: 5px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #45a049; }}
        """)
        
        help_button = QPushButton("🔧 Need help? Join community")
        help_button.setFixedHeight(32)  # Reduced from 40
        help_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {colors['primary']};
                color: white;
                border: none;
                padding: 6px 16px;
                border-radius: 5px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #1976D2; }}
        """)
        
        later_button = QPushButton("Maybe later")
        later_button.setFixedHeight(28)  # Reduced from 35
        later_button.setStyleSheet("""
            QPushButton {
                background-color: #666;
                color: white;
                border: none;
                padding: 6px 14px;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #555; }
        """)
        
        layout.addWidget(love_button)
        layout.addWidget(help_button)
        layout.addWidget(later_button)
        
        # Set dialog background
        self.setStyleSheet(f"QDialog {{ background-color: {colors['background']}; color: {colors['text']}; }}")

        # Connect events
        love_button.clicked.connect(self.love_it_button_click)
        help_button.clicked.connect(self.needs_work_button_click)
        later_button.clicked.connect(self.close)
        
        # Auto-adjust size to fit content properly
        self.adjustSize()

    def love_it_button_click(self):
        self.close()
        webbrowser.open('https://ankiweb.net/shared/review/1957538407')
        set_rated_true()

    def needs_work_button_click(self):
        self.close()
        webbrowser.open('https://discord.gg/9x4DRxzqwM')