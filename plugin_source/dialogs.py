import webbrowser
import aqt
import aqt.utils
import anki

import json
import requests

from aqt.qt import *
from aqt import mw
from aqt.theme import theme_manager

from . import config_manager
from .auth_manager import *
from .utils import get_logger
from .var_defs import API_BASE_URL, WEBSITE_BASE_URL
from .ui.colors import get_colors, get_button_style, get_dialog_style, get_input_style

logger = get_logger("ankicollab.dialogs")


def get_local_deck_from_hash(input_hash):
    strings_data = config_manager.get_config()
    if strings_data:
        for hash, details in strings_data.items():
            if hash == input_hash:
                return mw.col.decks.name(details["deckId"])
    return "None"


# legacy
def get_login_token():
    return auth_manager.get_token()


def set_rated_true():
    strings_data = config_manager.get_stored_config()
    if "settings" in strings_data:
        if "rated_addon" in strings_data["settings"]:
            strings_data["settings"]["rated_addon"] = True
            config_manager.write_config(strings_data)


class ChangelogDialog(QDialog):
    def __init__(self, changelog, deck_hash, parent=None):
        super().__init__(parent)
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
        header.setStyleSheet(
            f"font-size: 15px; font-weight: 500; color: {colors['text_primary']};"
        )
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
        skip_button.setStyleSheet(get_button_style("neutral"))

        later_button = QPushButton("Remind Me Later")
        later_button.setStyleSheet(get_button_style("neutral"))

        install_button = QPushButton("Install Update")
        install_button.setStyleSheet(get_button_style("success"))

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

    def __init__(self, old_tags, new_tags, parent=None):
        super().__init__(parent)
        self.checkboxes = {}

        colors = get_colors()
        self.setStyleSheet(get_dialog_style())

        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        self.setWindowTitle("AnkiCollab - Optional Tags")

        header = QLabel("Optional Tags")
        header.setStyleSheet(
            f"font-size: 15px; font-weight: 500; color: {colors['text_primary']};"
        )
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

        button = QPushButton("Save")
        button.setStyleSheet(get_button_style("success"))
        button.clicked.connect(lambda: self.close())
        layout.addWidget(button)

        self.setLayout(layout)

    def get_selected_tags(self):
        result = {}
        for item in self.checkboxes:
            result[item] = self.checkboxes[item].isChecked()

        return result


_PAIR_TTL_SECONDS = 300
_PAIR_POLL_INTERVAL_MS = 2000


def pairing_url(code):
    return f"{WEBSITE_BASE_URL}/pair?code={code}"


def interpret_poll(status_code, body):

    if status_code == 200:
        status = body.get("status") if isinstance(body, dict) else ""
        if status == "approved":
            return "approved"
        if status == "pending":
            return "pending"
        return "retry"

    if status_code == 403:
        return "rejected"

    if status_code in (404, 410):
        return "expired"

    return "retry"


def _connection_error_text(error=None):
    if isinstance(error, requests.Timeout):
        return "AnkiCollab did not answer in time. Check your connection and try again."
    return "Could not reach AnkiCollab. Check your connection and try again."


class _PairInitThread(QThread):

    result = pyqtSignal(object)

    def __init__(self, parent, device_id):
        super().__init__(parent)
        self._device_id = device_id
        self.finished.connect(self.deleteLater)

    def run(self):
        payload = {"ok": False, "error": _connection_error_text()}
        try:
            response = requests.post(
                f"{API_BASE_URL}/pair/init",
                json={"device_id": self._device_id},
                timeout=10,
                verify=True,
            )
            if response.status_code == 200:
                data = response.json()
                payload = {
                    "ok": True,
                    "code": str(data.get("code", "")),
                    "poll_token": str(data.get("poll_token", "")),
                    "expires_in": int(data.get("expires_in", _PAIR_TTL_SECONDS)),
                }
            else:
                payload = {
                    "ok": False,
                    "error": f"AnkiCollab refused the request ({response.status_code}).",
                }
        except Exception as err:  # noqa: BLE001 - every failure here is "try again"
            payload = {"ok": False, "error": _connection_error_text(err)}
        self.result.emit(payload)


class _PairPollThread(QThread):

    result = pyqtSignal(object)

    def __init__(self, parent, poll_token):
        super().__init__(parent)
        self._poll_token = poll_token
        self.finished.connect(self.deleteLater)

    def run(self):
        try:
            response = requests.post(
                f"{API_BASE_URL}/pair/poll",
                json={"token": self._poll_token},
                timeout=8,
                verify=True,
            )
            try:
                body = response.json()
            except ValueError:
                body = {}
            self.result.emit(
                {
                    "outcome": interpret_poll(response.status_code, body),
                    "body": body,
                    "token": self._poll_token,
                }
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("Pairing poll failed: %s", err)
            self.result.emit(
                {"outcome": "retry", "body": {}, "token": self._poll_token}
            )


class LoginDialog(QDialog):

    def __init__(self, parent=None):
        super(LoginDialog, self).__init__(parent)
        self.setWindowTitle("AnkiCollab - Login")
        self.setModal(True)

        colors = get_colors()
        self.setStyleSheet(get_dialog_style())

        self._poll_token = ""
        self._poll_outstanding = ""

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        layout.setSpacing(12)
        layout.setContentsMargins(25, 25, 25, 25)

        # Title
        title = QLabel("Login to AnkiCollab")
        title.setStyleSheet(
            f"font-size: 16px; font-weight: 500; color: {colors['text_primary']};"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self.pairing_block = QWidget()
        pairing_layout = QVBoxLayout(self.pairing_block)
        pairing_layout.setContentsMargins(0, 0, 0, 0)
        pairing_layout.setSpacing(10)

        self.subtitle = QLabel("Sign in without typing your password.")
        self.subtitle.setWordWrap(True)
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setStyleSheet(
            f"color: {colors['text_secondary']}; font-size: 12px;"
        )
        pairing_layout.addWidget(self.subtitle)

        self.code_label = QLabel("")
        self.code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_label.setMinimumHeight(56)
        self.code_label.setStyleSheet(
            f"font-size: 26px; font-weight: 600; letter-spacing: 4px;"
            f" color: {colors['text_primary']}; padding: 10px 0;"
            f" border: 1px solid {colors['border']}; border-radius: 6px;"
            f" background-color: {colors['surface']};"
        )
        pairing_layout.addWidget(self.code_label)

        self.status_label = QLabel("Asking AnkiCollab for a code...")
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(
            f"color: {colors['text_secondary']}; font-size: 12px;"
        )
        pairing_layout.addWidget(self.status_label)

        self.open_button = QPushButton("Approve in your browser")
        self.open_button.setToolTip(
            "Opens ankicollab.com so you can approve this login"
        )
        self.open_button.setStyleSheet(get_button_style("success"))
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_pairing_page)
        pairing_layout.addWidget(self.open_button)

        self.new_code_button = QPushButton("Get a new code")
        self.new_code_button.setStyleSheet(get_button_style("neutral"))
        self.new_code_button.clicked.connect(self.start_pairing)
        self.new_code_button.setVisible(False)
        pairing_layout.addWidget(self.new_code_button)

        # Only shown if the browser refuses to open, so the link is still usable.
        self.url_label = QLabel("")
        self.url_label.setWordWrap(True)
        self.url_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.url_label.setStyleSheet(
            f"color: {colors['text_secondary']}; font-size: 11px;"
        )
        self.url_label.setVisible(False)
        pairing_layout.addWidget(self.url_label)

        layout.addWidget(self.pairing_block)

        self.password_toggle = QPushButton("Sign in with a password instead")
        self.password_toggle.setFlat(True)
        self.password_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.password_toggle.setStyleSheet(
            f"border: none; background: none; color: {colors['primary']};"
            f" font-size: 12px; text-decoration: underline;"
        )
        self.password_toggle.clicked.connect(self.toggle_password_form)
        layout.addWidget(self.password_toggle)

        # ------------------------------------------------ password form, hidden
        self.password_form = QWidget()
        form_layout = QVBoxLayout(self.password_form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(10)

        # Username field
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")
        self.username_input.setStyleSheet(get_input_style())

        # Password field
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setStyleSheet(get_input_style())

        form_layout.addWidget(self.username_input)
        form_layout.addWidget(self.password_input)

        login_button = QPushButton("Login")
        login_button.setToolTip("Sign in with your AnkiCollab account")
        login_button.setStyleSheet(get_button_style("success"))
        login_button.clicked.connect(self.login)
        form_layout.addWidget(login_button)

        self.password_form.setVisible(False)
        layout.addWidget(self.password_form)

        # Signup link
        signup_link = QLabel(
            f'<a href="#" style="color: {colors["primary"]}; text-decoration: underline;">Don\'t have an account? Sign up here</a>'
        )
        signup_link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        signup_link.setStyleSheet(
            f"color: {colors['primary']}; font-size: 12px; margin-top: 6px;"
        )
        signup_link.setCursor(Qt.CursorShape.PointingHandCursor)
        signup_link.linkActivated.connect(self.open_signup)
        layout.addWidget(signup_link)

        # Enter key handling
        self.password_input.returnPressed.connect(self.login)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_PAIR_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self.poll_once)

        self._expiry_timer = QTimer(self)
        self._expiry_timer.setSingleShot(True)
        self._expiry_timer.timeout.connect(self.on_code_expired)

        self.start_pairing()

    # ------------------------------------------------------------------ pairing

    def start_pairing(self):
        """Ask for a fresh code and start waiting for it to be approved."""
        self.stop_timers()
        self._poll_token = ""
        self._poll_outstanding = ""
        self.code_label.setText("")
        self.url_label.setVisible(False)
        self.new_code_button.setVisible(False)
        self.open_button.setEnabled(False)
        self.status_label.setText("Asking AnkiCollab for a code...")

        thread = _PairInitThread(mw, config_manager.get_device_id())
        thread.result.connect(self.on_code_received)
        thread.start()

    def on_code_received(self, payload):
        if self.password_form.isVisible():
            return

        if not payload.get("ok"):
            self.status_label.setText(payload.get("error", "Could not start pairing."))
            self.new_code_button.setVisible(True)
            self.adjustSize()
            return

        self._poll_token = payload.get("poll_token", "")
        self.code_label.setText(payload.get("code", ""))
        self.status_label.setText("Waiting for you to approve the code...")
        self.open_button.setEnabled(True)

        self._poll_timer.start()
        self._expiry_timer.start(
            int(payload.get("expires_in", _PAIR_TTL_SECONDS)) * 1000
        )
        self.adjustSize()

    def open_pairing_page(self):
        url = pairing_url(self.code_label.text())
        self.url_label.setText(url)
        try:
            opened = webbrowser.open(url)
        except Exception as err:  # noqa: BLE001 - a missing browser is not a crash
            logger.warning("Could not open the pairing page: %s", err)
            opened = False
        if not opened:
            self.url_label.setVisible(True)
            self.status_label.setText("Open this link to approve the code:")
            self.adjustSize()

    def poll_once(self):
        if not self._poll_token:
            return
        if self._poll_outstanding == self._poll_token:
            return
        self._poll_outstanding = self._poll_token

        thread = _PairPollThread(mw, self._poll_token)
        thread.result.connect(self.on_poll_result)
        thread.start()

    def on_poll_result(self, payload):
        if payload.get("token") != self._poll_token:
            return
        self._poll_outstanding = ""

        outcome = payload.get("outcome")

        if outcome == "approved":
            if auth_manager.store_login_result(payload.get("body") or {}):
                self.accept()
            else:
                self.fail_pairing("AnkiCollab sent a response we could not read.")
            return

        if outcome == "rejected":
            self.fail_pairing("That request was denied in the browser.")
            return

        if outcome == "expired":
            self.on_code_expired()
            return

    def on_code_expired(self):
        self.fail_pairing("That code expired. Get a new one to try again.")

    def fail_pairing(self, message):
        self._poll_token = ""
        self.stop_timers()
        self.open_button.setEnabled(False)
        self.status_label.setText(message)
        self.new_code_button.setVisible(True)
        self.adjustSize()

    def stop_timers(self):
        self._poll_timer.stop()
        self._expiry_timer.stop()

    def toggle_password_form(self):
        showing = not self.password_form.isVisible()
        self.password_form.setVisible(showing)
        self.pairing_block.setVisible(not showing)
        self.password_toggle.setText(
            "Sign in with a browser instead"
            if showing
            else "Sign in with a password instead"
        )
        if showing:
            self.stop_timers()
            self._poll_token = ""
            self.username_input.setFocus()
        else:
            self.start_pairing()
        self.adjustSize()

    # ------------------------------------------------------------------- dialog

    def done(self, result):
        self.stop_timers()
        self._poll_token = ""
        super().done(result)

    def closeEvent(self, event):
        self.stop_timers()
        self._poll_token = ""
        super().closeEvent(event)

    # ----------------------------------------------------------------- password

    def open_signup(self):
        webbrowser.open(f"{WEBSITE_BASE_URL}/signup")
        self.accept()

    def login(self):
        username = self.username_input.text()
        password = self.password_input.text()

        if not username or not password:
            aqt.utils.showInfo(
                "Please enter both your username and password to continue."
            )
            return

        payload = {
            "username": username,
            "password": password,
            "device_id": config_manager.get_device_id(),
        }

        try:
            response = requests.post(
                f"{API_BASE_URL}/login", json=payload, timeout=10, verify=True
            )

            if response.status_code == 200:
                # Parse the JSON response
                auth_data = response.json()

                if auth_manager.store_login_result(auth_data):
                    self.accept()
                else:
                    msg_box = QMessageBox(self)
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
        super().__init__(parent)
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
        header.setStyleSheet(
            f"font-size: 15px; font-weight: 500; color: {colors['text_primary']};"
        )
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
        cancel_button.setStyleSheet(get_button_style("neutral"))
        cancel_button.clicked.connect(self.reject)

        publish_button = QPushButton("Publish")
        publish_button.setStyleSheet(get_button_style("success"))
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

        from .api_client import api_client

        payload = {
            "deck_hash": self.deck_hash,
            "changelog": changelog_text,
        }

        response = api_client.post_json("/submitChangelog", payload, timeout=30)
        if response.status_code == 200:
            QMessageBox.information(self, "Information", response.text)
        else:
            QMessageBox.warning(
                self,
                "Error",
                "An unknown error occurred while publishing the changelog.",
            )

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
        header.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {colors['warning']};"
        )
        layout.addWidget(header)

        label = QLabel(
            "The maintainers removed the following notes from the deck. How would you like to proceed?"
        )
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {colors['text_secondary']}; margin-bottom: 8px;")
        layout.addWidget(label)

        deleted_notes_text = QTextBrowser()
        deleted_notes_text.setMaximumHeight(200)
        deleted_notes_text.setStyleSheet(f"""
            QTextBrowser {{
                border: 1px solid {colors['border']};
                border-radius: 5px;
                padding: 8px;
                background-color: {colors['surface']};
                color: {colors['text_primary']};
            }}
        """)

        deleted_notes_str = "\n".join(map(str, deleted_notes))
        deleted_notes_text.setPlainText(deleted_notes_str)
        layout.addWidget(deleted_notes_text)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        keep_button = QPushButton("Keep Notes")
        keep_button.setStyleSheet(get_button_style("neutral"))
        keep_button.clicked.connect(lambda: self.done(2))

        review_button = QPushButton("Review in Browser")
        review_button.setStyleSheet(get_button_style("neutral"))
        review_button.clicked.connect(self.reject)

        delete_button = QPushButton("Delete Notes")
        delete_button.setStyleSheet(get_button_style("danger"))
        delete_button.clicked.connect(self.accept)

        button_layout.addWidget(keep_button)
        button_layout.addWidget(review_button)
        button_layout.addWidget(delete_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)
        self.adjustSize()


class ReviewDataConsentDialog(QDialog):
    """Ask once whether review data may be shared with a deck's maintainers."""

    def __init__(self, deck_name, deck_hash, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Share your review data?")
        self.setModal(True)

        colors = get_colors()
        self.setStyleSheet(get_dialog_style())

        self.deck_name = deck_name
        self.deck_hash = deck_hash
        self._recorded = False

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(18, 18, 18, 18)

        header = QLabel("Share your review data?")
        header.setStyleSheet(
            f"font-size: 15px; font-weight: 500; color: {colors['text_primary']};"
        )
        header.ensurePolished()
        main_layout.addWidget(header)

        # The body scrolls so the buttons stay on screen on short displays.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._scroll = scroll

        body = QWidget()
        body.setObjectName("reviewConsentBody")
        body.setAutoFillBackground(False)
        body.setStyleSheet("#reviewConsentBody { background: transparent; }")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        self._body = body

        message = QLabel(
            f"AnkiCollab can send the people who maintain <b>{self.deck_name}</b> "
            "statistics from your reviews, so they can see which notes are "
            "working and which aren't."
        )
        message.setWordWrap(True)
        message.setStyleSheet(f"color: {colors['text_primary']};")
        body_layout.addWidget(message)

        details = QLabel(
            "<b>What's sent:</b> per-note retention, lapse and repetition counts "
            "(averaged per deck), the deck names, and a "
            "note identifier for each note.<br><br>"
            "<b>Who sees it:</b> the maintainers of this deck, and the "
            "AnkiCollab team."
        )
        details.setWordWrap(True)
        details.setStyleSheet(f"color: {colors['text_secondary']};")
        body_layout.addWidget(details)

        linkage = QLabel(
            "This data is tied to your AnkiCollab account. The maintainers see "
            "the statistics, not your username.<br><br>"
            "We may also use it for deck statistics and academic research, as "
            "described in the Privacy Policy."
        )
        linkage.setWordWrap(True)
        linkage.setStyleSheet(f"color: {colors['text_secondary']};")
        body_layout.addWidget(linkage)

        withdrawal = QLabel(
            "You can change your mind at any time in <b>Manage Subscriptions "
            "&rarr; Configure &rarr; Review data</b>. Turning it off stops "
            "future uploads and schedules the statistics you've already "
            "shared for deletion."
        )
        withdrawal.setWordWrap(True)
        withdrawal.setStyleSheet(f"color: {colors['text_secondary']};")
        body_layout.addWidget(withdrawal)

        link_color = colors["primary"]
        policy_link = QLabel(
            f'<a href="{WEBSITE_BASE_URL}/privacy" style="color: {link_color}; '
            f'text-decoration: none;">Read our Privacy Policy</a>'
        )
        policy_link.setOpenExternalLinks(True)
        body_layout.addWidget(policy_link)

        body_layout.addStretch()
        scroll.setWidget(body)
        main_layout.addWidget(scroll, 1)

        button_layout = QHBoxLayout()
        button_layout.addStretch()

        no_button = QPushButton("Don't share")
        no_button.setStyleSheet(get_button_style("neutral"))
        no_button.setAutoDefault(False)
        no_button.clicked.connect(self.reject)

        yes_button = QPushButton("Share review data")
        yes_button.setStyleSheet(get_button_style("neutral"))
        yes_button.setAutoDefault(False)
        yes_button.clicked.connect(self.accept)

        button_layout.addWidget(no_button)
        button_layout.addWidget(yes_button)
        main_layout.addLayout(button_layout)

        self._header = header
        self._button_layout = button_layout
        self._fit_to_screen()

    def _fit_to_screen(self):
        """Size the dialog to its content: wide enough to stay short, and never
        taller than the display, in which case the body scrolls instead."""
        screen = None
        get_screen = getattr(self, "screen", None)
        if get_screen is not None:
            screen = get_screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return

        available = screen.availableGeometry()
        width = min(660, int(available.width() * 0.9))
        self.setMinimumWidth(width)
        self.setMaximumWidth(width)

        layout = self.layout()
        margins = layout.contentsMargins()
        chrome = (
            margins.top()
            + margins.bottom()
            + self._header.sizeHint().height()
            + self._button_layout.sizeHint().height()
            + 2 * layout.spacing()
        )

        self.layout().activate()
        content_width = (
            self._scroll.viewport().width() or width - margins.left() - margins.right()
        )
        content_height = self._body.heightForWidth(content_width)
        if content_height < 0:
            content_height = self._body.sizeHint().height()

        max_height = int(available.height() * 0.85)
        body_height = min(content_height, max(0, max_height - chrome))
        self._scroll.setFixedHeight(body_height)
        self.setFixedHeight(chrome + body_height)

    def _record(self, granted):
        if self._recorded:
            return
        self._recorded = True
        from .consent import record_consent

        record_consent(self.deck_hash, granted)

    def accept(self):
        self._record(True)
        super().accept()

    def reject(self):
        self._record(False)
        super().reject()


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

        layout.addSpacing(8)

        love_button = QPushButton("Leave a Rating")
        love_button.setFixedHeight(36)
        love_button.setStyleSheet(get_button_style("success"))
        layout.addWidget(love_button)

        secondary_layout = QHBoxLayout()

        help_button = QPushButton("Join Discord")
        help_button.setStyleSheet(get_button_style("neutral", "small"))

        later_button = QPushButton("Not Now")
        later_button.setStyleSheet(get_button_style("neutral", "small"))

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
        webbrowser.open("https://ankiweb.net/shared/review/1957538407")
        set_rated_true()

    def needs_work_button_click(self):
        self.close()
        webbrowser.open("https://discord.gg/9x4DRxzqwM")


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
        title.setStyleSheet(
            f"font-size: 14px; font-weight: 500; color: {colors['text_primary']}; margin-bottom: 5px;"
        )
        layout.addWidget(title)

        # Description
        desc = QLabel(
            "Select fields to protect from AnkiCollab updates.<br>"
            "Protected fields will not be overwritten when syncing."
        )
        desc.setStyleSheet(
            f"color: {colors['text_primary']}; font-size: 12px; margin-bottom: 10px;"
        )
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
        tags_desc.setStyleSheet(
            f"color: {colors['text_secondary']}; font-size: 11px; margin-left: 20px;"
        )
        layout.addWidget(tags_desc)

        # Buttons
        button_layout = QHBoxLayout()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(get_button_style("neutral"))
        cancel_btn.clicked.connect(self.reject)

        save_btn = QPushButton("Save")
        save_btn.setStyleSheet(get_button_style("success"))
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
