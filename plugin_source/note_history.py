"""AnkiCollab - Note History Dialog.

Fetches and displays note commit history, author justifications (rationale),
and field diffs directly inside Anki.
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional

import aqt
from aqt import mw
from aqt.qt import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QShortcut,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    Qt,
    QKeySequence,
)

from .api_client import api_client
from .ui.colors import get_colors
from .utils import get_logger

logger = get_logger("ankicollab.note_history")


class NoteHistoryDialog(QDialog):
    """Clean, sober dialog displaying commit history and diffs for a note."""

    def __init__(
        self,
        deck_hash: str,
        guid: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.deck_hash = deck_hash
        self.guid = guid
        self.colors = get_colors()

        self.setWindowTitle("AnkiCollab - Note History")
        self.resize(760, 560)

        # Close on Escape or Ctrl+W
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)
        QShortcut(QKeySequence.StandardKey.Close, self, self.close)

        self._init_ui()
        self._load_history()

    def _init_ui(self) -> None:
        c = self.colors
        self.setStyleSheet(
            f"QDialog {{ background-color: {c['background']}; color: {c['text_primary']}; }}"
            f"QLabel {{ color: {c['text_primary']}; }}"
            f"QPushButton {{ background-color: {c['surface_elevated']}; color: {c['text_primary']}; "
            f"border: 1px solid {c['border']}; border-radius: 4px; padding: 6px 14px; font-size: 13px; }}"
            f"QPushButton:hover {{ background-color: {c['surface_hover']}; border-color: {c['border_focus']}; }}"
            f"QTextBrowser {{ background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 4px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header info
        header_layout = QHBoxLayout()
        title_label = QLabel(f"Note History (GUID: {self.guid[:12]}...)")
        title_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        header_layout.addWidget(title_label)

        header_layout.addStretch()

        self.status_label = QLabel("Loading...")
        self.status_label.setStyleSheet(f"color: {c['text_muted']}; font-size: 12px;")
        header_layout.addWidget(self.status_label)

        layout.addLayout(header_layout)

        # Scroll area for commits list
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(4, 4, 4, 4)
        self.container_layout.setSpacing(10)
        scroll.setWidget(self.container)

        layout.addWidget(scroll)

        # Bottom close button
        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        bottom_layout.addWidget(close_btn)

        layout.addLayout(bottom_layout)

    def _load_history(self) -> None:
        c = self.colors
        payload = {
            "deck_hash": self.deck_hash,
            "guid": self.guid,
            "limit": 100,
        }

        self.status_label.setText("Loading history from server...")

        def _fetch():
            return api_client.post_json("/GetNoteHistory", payload, timeout=15)

        def _on_done(future):
            try:
                response = future.result()
                if not self.isVisible() and not self.isActiveWindow():
                    return
                if response.status_code == 200:
                    data = response.json()
                    self._render_history(data)
                    self.status_label.setText(f"{len(data.get('groups', []))} commit group(s)")
                elif response.status_code == 404:
                    self.status_label.setText("Note not found on server")
                    self._render_empty("This note has not been published or found on AnkiCollab.")
                else:
                    self.status_label.setText("Failed to load")
                    self._render_empty(f"Server returned status {response.status_code}.")
            except Exception as err:
                logger.warning(f"Error fetching note history: {err}")
                self.status_label.setText("Network error")
                self._render_empty("Could not connect to AnkiCollab server to retrieve note history.")

        if mw and hasattr(mw, "taskman") and hasattr(mw.taskman, "run_in_background"):
            mw.taskman.run_in_background(_fetch, _on_done)
        else:
            class MockFuture:
                def __init__(self, val):
                    self._val = val

                def result(self):
                    return self._val

            try:
                _on_done(MockFuture(_fetch()))
            except Exception as err:
                logger.warning(f"Error fetching note history: {err}")
                self.status_label.setText("Network error")
                self._render_empty("Could not connect to AnkiCollab server to retrieve note history.")

    def _render_empty(self, message: str) -> None:
        c = self.colors
        while self.container_layout.count():
            w = self.container_layout.takeAt(0)
            if w.widget():
                w.widget().deleteLater()
        lbl = QLabel(message)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color: {c['text_secondary']}; padding: 30px;")
        self.container_layout.addWidget(lbl)

    def _render_history(self, data: Dict[str, Any]) -> None:
        c = self.colors
        while self.container_layout.count():
            w = self.container_layout.takeAt(0)
            if w.widget():
                w.widget().deleteLater()

        groups = data.get("groups", [])
        if not groups:
            self._render_empty("No history events recorded for this note.")
            return

        for grp in groups:
            group_widget = QWidget()
            group_widget.setStyleSheet(
                f"border: 1px solid {c['border']}; border-radius: 6px; background-color: {c['surface']}; margin-bottom: 8px;"
            )
            gw_layout = QVBoxLayout(group_widget)
            gw_layout.setContentsMargins(12, 10, 12, 10)
            gw_layout.setSpacing(6)

            # Commit metadata header
            commit_id = grp.get("commit_id")
            author = grp.get("commit_author") or "Anonymous"
            timestamp = grp.get("commit_timestamp") or ""
            rationale = grp.get("commit_rationale") or ""
            approved = grp.get("approved", False)
            denied = grp.get("denied", False)

            status_str = "Approved" if approved else ("Denied" if denied else "Pending review")
            status_color = c["success"] if approved else (c["danger"] if denied else c["warning"])

            meta_layout = QHBoxLayout()
            cid_label = QLabel(f"Commit #{commit_id}" if commit_id else "Unassigned commit")
            cid_label.setStyleSheet("font-weight: bold; font-size: 13px;")
            meta_layout.addWidget(cid_label)

            author_label = QLabel(f"by {author}  ({timestamp})")
            author_label.setStyleSheet(f"color: {c['text_secondary']}; font-size: 12px;")
            meta_layout.addWidget(author_label)

            meta_layout.addStretch()

            status_lbl = QLabel(status_str)
            status_lbl.setStyleSheet(f"color: {status_color}; font-weight: 600; font-size: 12px;")
            meta_layout.addWidget(status_lbl)

            gw_layout.addLayout(meta_layout)

            # Commit rationale if present
            if rationale:
                rat_lbl = QLabel(f"Reason: {rationale}")
                rat_lbl.setTextFormat(Qt.TextFormat.PlainText)
                rat_lbl.setStyleSheet(
                    f"font-style: italic; color: {c['text_secondary']}; background-color: {c['surface_elevated']}; "
                    f"padding: 4px 8px; border-radius: 4px;"
                )
                gw_layout.addWidget(rat_lbl)

            # Events in this commit group
            for ev in grp.get("events", []):
                ev_type = ev.get("event_type", "")
                fname = ev.get("field_name")
                diff_html = ev.get("diff_html")
                old_h = ev.get("old_human")
                new_h = ev.get("new_human")

                ev_label_text = f"• {ev_type}"
                if fname:
                    ev_label_text += f" [{fname}]"
                ev_lbl = QLabel(ev_label_text)
                ev_lbl.setStyleSheet(f"font-weight: 500; font-size: 12px; color: {c['primary']};")
                gw_layout.addWidget(ev_lbl)

                if diff_html:
                    diff_view = QTextBrowser()
                    diff_view.setOpenExternalLinks(False)
                    diff_view.setHtml(diff_html)
                    diff_view.setMaximumHeight(100)
                    gw_layout.addWidget(diff_view)
                elif old_h or new_h:
                    summary_text = ""
                    if old_h:
                        summary_text += f"Old: {html.escape(old_h)}<br>"
                    if new_h:
                        summary_text += f"New: {html.escape(new_h)}"
                    detail_lbl = QLabel(summary_text)
                    detail_lbl.setTextFormat(Qt.TextFormat.RichText)
                    detail_lbl.setStyleSheet(f"color: {c['text_secondary']}; font-size: 12px; margin-left: 12px;")
                    gw_layout.addWidget(detail_lbl)

            self.container_layout.addWidget(group_widget)

        self.container_layout.addStretch()


def show_note_history_dialog(deck_hash: str, guid: str, parent: Optional[QWidget] = None) -> None:
    dialog = NoteHistoryDialog(deck_hash=deck_hash, guid=guid, parent=parent or mw)
    dialog.exec()


def open_note_history_for_note_id(note_id: int, editor=None) -> None:
    """Helper to open NoteHistoryDialog from editor or browser."""
    if not mw.col:
        return

    note = mw.col.get_note(note_id)
    if not note or not note.cards():
        aqt.utils.showInfo("Could not find note.", parent=editor.parentWindow if editor else mw)
        return

    from .hooks import get_deck_hash_from_card, get_note_guid_from_id

    card = note.cards()[0]
    deck_hash, error = get_deck_hash_from_card(card)

    if not deck_hash:
        aqt.utils.showInfo(error or "Cannot find the Cloud Deck for this note.", parent=editor.parentWindow if editor else mw)
        return

    guid = get_note_guid_from_id(note_id)
    show_note_history_dialog(deck_hash=deck_hash, guid=guid, parent=editor.parentWindow if editor else mw)
