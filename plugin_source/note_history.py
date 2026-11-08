"""AnkiCollab - Note History Dialog.

Fetches and displays note commit history, author justifications (rationale),
and field diffs directly inside Anki with two-column ergonomics, token-level
diffing, granular field restoration, and visual/rendered/raw display modes.
"""

from __future__ import annotations

import difflib
import html
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import aqt
from aqt import mw
from aqt.qt import (
    QApplication,
    QComboBox,
    QCursor,
    QDialog,
    QFrame,
    QHBoxLayout,
    QKeySequence,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QShortcut,
    QSize,
    QSplitter,
    Qt,
    QTimer,
    QVBoxLayout,
    QWidget,
)

if not isinstance(QFrame, type):
    class _FallbackFrame(QWidget):
        class Shape:
            NoFrame = 0
    QFrame = _FallbackFrame  # Fallback when QFrame is a mock instance in tests

from .api_client import api_client
from .ui.colors import get_colors
from .utils import get_logger

logger = get_logger("ankicollab.note_history")


# ---------------------------------------------------------------------------
# Diff and HTML tokenization helpers (Diff Hunk inspired)
# ---------------------------------------------------------------------------

def normalize_html_tags(text: str) -> str:
    """Standardize self-closing tags, quotes and minor tag spacing to prevent false diffs."""
    if not text:
        return ""

    def norm_tag(m: re.Match) -> str:
        tag = m.group(0)
        # Standardize self-closing tags: <br />, <br/> -> <br>, <hr />, <hr/> -> <hr>
        tag = re.sub(r"^<([a-zA-Z0-9]+)\s*/>$", r"<\1>", tag)
        tag = re.sub(r"^<([a-zA-Z0-9]+)\s+([^>]*?)\s*/>$", r"<\1 \2>", tag)
        # Standardize single quotes to double quotes for tag attributes
        tag = re.sub(r"=\s*'([^']*)'", r'="\1"', tag)
        # Normalize trailing spaces inside tags
        tag = re.sub(r"\s+>$", ">", tag)
        return tag

    return re.sub(r"<[^>]+>", norm_tag, text)


def _tokenize(text: str) -> List[str]:
    """Tokenize text into atomic HTML tags, words with apostrophes, spaces or punctuation."""
    if not text:
        return []
    return re.findall(r"<[^>]+>|[\w\'-]+|\s+|[^\w\s<>]", text)


def _render_diff_sides(old_text: str, new_text: str, colors: dict) -> Tuple[str, str]:
    """Generate HTML formatted for side-by-side comparison:
      - old_side: previous text with removals highlighted in red strikethrough
      - new_side: updated text with additions highlighted in bold green
    """
    is_dark = colors.get("background", "#1E1E1E").lower() in ("#1e1e1e", "#2a2a2a", "#121212")
    rem_bg = colors.get("rem_bg", "#2b0c0c" if is_dark else "#fde8e8")
    rem_fg = colors.get("rem_fg", "#f87171" if is_dark else "#9b1c1c")
    add_bg = colors.get("add_bg", "#032117" if is_dark else "#def7ec")
    add_fg = colors.get("add_fg", "#34d399" if is_dark else "#03543f")

    rem_style = (
        f"background-color: {rem_bg}; color: {rem_fg}; "
        f"text-decoration: line-through; border-radius: 2px; padding: 1px 3px;"
    )
    add_style = (
        f"background-color: {add_bg}; color: {add_fg}; "
        f"font-weight: 600; border-radius: 2px; padding: 1px 3px;"
    )

    norm_old = normalize_html_tags(old_text or "")
    norm_new = normalize_html_tags(new_text or "")

    t1 = _tokenize(norm_old)
    t2 = _tokenize(norm_new)

    sm = difflib.SequenceMatcher(None, t1, t2, autojunk=False)

    old_out: List[str] = []
    new_out: List[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            txt1 = "".join(t1[i1:i2])
            txt2 = "".join(t2[j1:j2])
            old_out.append(txt1)
            new_out.append(txt2)
        elif tag == "delete":
            del_txt = "".join(t1[i1:i2])
            old_out.append(f'<span style="{rem_style}">{del_txt}</span>')
        elif tag == "insert":
            ins_txt = "".join(t2[j1:j2])
            new_out.append(f'<span style="{add_style}">{ins_txt}</span>')
        elif tag == "replace":
            del_txt = "".join(t1[i1:i2])
            ins_txt = "".join(t2[j1:j2])
            old_out.append(f'<span style="{rem_style}">{del_txt}</span>')
            new_out.append(f'<span style="{add_style}">{ins_txt}</span>')

    return "".join(old_out), "".join(new_out)


def _clear_layout(layout: Any) -> None:
    """Safely clear all widgets from a layout, handling mock Qt in test environments."""
    if hasattr(layout, "count"):
        cnt = layout.count()
        if isinstance(cnt, int):
            while layout.count() > 0:
                w = layout.takeAt(0)
                if w and hasattr(w, "widget") and w.widget():
                    w.widget().deleteLater()


def _safe_timer_set_text(btn: QPushButton, text: str, delay_ms: int = 1500) -> None:
    """Safely restore button text after a temporary feedback message."""
    try:
        QTimer.singleShot(delay_ms, lambda: btn.setText(text) if btn and not btn.isHidden() else None)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FieldComparisonCard — Card comparing Before and After values of a field
# ---------------------------------------------------------------------------

class FieldComparisonCard(QFrame):
    """Side-by-side card displaying Avant | Apres for a changed field."""

    def __init__(
        self,
        field_name: str,
        old_val: str,
        new_val: str,
        current_val: str,
        old_title: str = "Avant",
        new_title: str = "Après",
        render_mode: str = "diff",
        on_restore_old: Optional[Callable[[], None]] = None,
        on_restore_new: Optional[Callable[[], None]] = None,
        colors: Optional[dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._c = colors or get_colors()
        self._field_name = field_name
        self._old_val = old_val
        self._new_val = new_val
        self._current_val = current_val
        self._old_title = old_title
        self._new_title = new_title
        self._render_mode = render_mode
        self._on_restore_old = on_restore_old
        self._on_restore_new = on_restore_new

        self.setStyleSheet(
            f"QFrame {{ background-color: {self._c['surface']}; border: 1px solid {self._c['border']}; "
            f"border-radius: 6px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        # Header
        hdr_layout = QHBoxLayout()
        hdr_layout.setContentsMargins(0, 0, 0, 0)
        hdr_layout.setSpacing(6)

        title_lbl = QLabel(f"Field: {field_name}")
        title_lbl.setStyleSheet(
            f"color: {self._c['primary']}; font-size: 12px; font-weight: 700; "
            f"background: transparent; border: none;"
        )
        hdr_layout.addWidget(title_lbl)

        # Badge
        if not old_val.strip() and new_val.strip():
            badge_text = "[Added]"
            badge_color = self._c.get("success", "#34D399")
        elif old_val.strip() and not new_val.strip():
            badge_text = "[Removed]"
            badge_color = self._c.get("danger", "#F87171")
        else:
            badge_text = "[Modified]"
            badge_color = self._c.get("text_secondary", "#a6a6a6")

        badge_lbl = QLabel(badge_text)
        badge_lbl.setStyleSheet(
            f"color: {badge_color}; font-size: 11px; font-weight: 600; "
            f"background: transparent; border: none;"
        )
        hdr_layout.addWidget(badge_lbl)
        hdr_layout.addStretch()

        layout.addLayout(hdr_layout)

        # Comparative Grid (Before | After)
        grid = QHBoxLayout()
        grid.setSpacing(8)

        # Left box: Before
        self._left_box, self._left_lbl = self._create_side_box(
            side_title=old_title,
            value=old_val,
            other_value=new_val,
            is_new_side=False,
            current_val=current_val,
            render_mode=render_mode,
            on_restore=on_restore_old,
            restore_btn_label="Restore Before",
        )
        grid.addWidget(self._left_box, stretch=1)

        # Right box: After
        self._right_box, self._right_lbl = self._create_side_box(
            side_title=new_title,
            value=new_val,
            other_value=old_val,
            is_new_side=True,
            current_val=current_val,
            render_mode=render_mode,
            on_restore=on_restore_new,
            restore_btn_label="Restore After",
        )
        grid.addWidget(self._right_box, stretch=1)

        layout.addLayout(grid)

    def set_render_mode(self, render_mode: str) -> None:
        self._render_mode = render_mode
        self._update_label_content(self._left_lbl, self._old_val, self._new_val, is_new_side=False)
        self._update_label_content(self._right_lbl, self._new_val, self._old_val, is_new_side=True)

    def _create_side_box(
        self,
        side_title: str,
        value: str,
        other_value: str,
        is_new_side: bool,
        current_val: str,
        render_mode: str,
        on_restore: Optional[Callable[[], None]],
        restore_btn_label: str,
    ) -> Tuple[QFrame, QLabel]:
        c = self._c
        box = QFrame()
        box.setStyleSheet(
            f"QFrame {{ background-color: {c['background']}; border: 1px solid {c['border']}; border-radius: 4px; }}"
        )
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(8, 6, 8, 8)
        box_layout.setSpacing(6)

        # Top bar of side box
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        top_bar.setSpacing(6)

        side_lbl = QLabel(side_title)
        side_lbl.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 11px; font-weight: 600; "
            f"background: transparent; border: none;"
        )
        top_bar.addWidget(side_lbl)
        top_bar.addStretch()

        # Copy button
        btn_copy = QPushButton("Copy")
        btn_copy.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_copy.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {c['border']}; "
            f"border-radius: 3px; color: {c['text_secondary']}; font-size: 10px; padding: 2px 6px; }}"
            f"QPushButton:hover {{ color: {c['text_primary']}; background: {c['surface_hover']}; border-color: {c['border_focus']}; }}"
        )

        def _do_copy():
            app = QApplication.instance()
            if app:
                clipboard = app.clipboard()
                if clipboard:
                    clipboard.setText(value)
                    btn_copy.setText("Copied!")
                    _safe_timer_set_text(btn_copy, "Copy", 1500)

        btn_copy.clicked.connect(_do_copy)
        top_bar.addWidget(btn_copy)

        # Restore button
        is_already_active = (value == current_val)
        btn_restore = QPushButton()
        btn_restore.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        if is_already_active:
            btn_restore.setText("Current")
            btn_restore.setEnabled(False)
            btn_restore.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {c['border']}; "
                f"border-radius: 3px; color: {c['text_muted']}; font-size: 10px; padding: 2px 6px; }}"
            )
        else:
            btn_restore.setText(restore_btn_label)
            btn_restore.setStyleSheet(
                f"QPushButton {{ background: {c['surface_elevated']}; border: 1px solid {c['primary']}; "
                f"border-radius: 3px; color: {c['text_primary']}; font-size: 10px; font-weight: 600; padding: 2px 8px; }}"
                f"QPushButton:hover {{ background: {c['primary']}; color: {c['text_on_accent']}; }}"
            )
            if on_restore:
                btn_restore.clicked.connect(on_restore)

        top_bar.addWidget(btn_restore)
        box_layout.addLayout(top_bar)

        # Content label
        content_lbl = QLabel()
        content_lbl.setWordWrap(True)
        content_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        content_lbl.setStyleSheet(
            f"color: {c['text_primary']}; font-size: 12px; background: transparent; border: none; padding: 2px;"
        )

        self._update_label_content(content_lbl, value, other_value, is_new_side)

        box_layout.addWidget(content_lbl)
        return box, content_lbl

    def _update_label_content(self, lbl: QLabel, value: str, other_value: str, is_new_side: bool) -> None:
        c = self._c
        if not value.strip():
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setText(f"<i style='color:{c['text_muted']};'>(Empty)</i>")
            return

        if self._render_mode == "diff":
            old_h, new_h = _render_diff_sides(
                value if not is_new_side else other_value,
                other_value if not is_new_side else value,
                c,
            )
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setText(old_h if not is_new_side else new_h)
            lbl.setStyleSheet(
                f"color: {c['text_primary']}; font-size: 12px; background: transparent; border: none; padding: 2px;"
            )
        elif self._render_mode == "raw":
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            lbl.setText(value)
            lbl.setStyleSheet(
                f"color: {c['text_primary']}; font-family: 'Consolas', monospace; font-size: 11px; "
                f"background: transparent; border: none; padding: 2px;"
            )
        else:  # render
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setText(value)
            lbl.setStyleSheet(
                f"color: {c['text_primary']}; font-size: 12px; background: transparent; border: none; padding: 2px;"
            )


# ---------------------------------------------------------------------------
# TagsComparisonCard — Card comparing Tags
# ---------------------------------------------------------------------------

class TagsComparisonCard(QFrame):
    """Side-by-side card displaying tag changes with chips and restoration buttons."""

    def __init__(
        self,
        old_tags: List[str],
        new_tags: List[str],
        current_tags: List[str],
        old_title: str = "Before",
        new_title: str = "After",
        on_restore_old: Optional[Callable[[], None]] = None,
        on_restore_new: Optional[Callable[[], None]] = None,
        colors: Optional[dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._c = colors or get_colors()
        self.setStyleSheet(
            f"QFrame {{ background-color: {self._c['surface']}; border: 1px solid {self._c['border']}; "
            f"border-radius: 6px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        # Header
        hdr_layout = QHBoxLayout()
        title_lbl = QLabel("Tags")
        title_lbl.setStyleSheet(
            f"color: {self._c['primary']}; font-size: 12px; font-weight: 700; "
            f"background: transparent; border: none;"
        )
        hdr_layout.addWidget(title_lbl)

        badge_lbl = QLabel("[Modified]")
        badge_lbl.setStyleSheet(
            f"color: {self._c['text_secondary']}; font-size: 11px; font-weight: 600; "
            f"background: transparent; border: none;"
        )
        hdr_layout.addWidget(badge_lbl)
        hdr_layout.addStretch()
        layout.addLayout(hdr_layout)

        # Grid
        grid = QHBoxLayout()
        grid.setSpacing(8)

        grid.addWidget(
            self._create_tag_box(
                side_title=old_title,
                tags=old_tags,
                current_tags=current_tags,
                restore_btn_label="Restore Before Tags",
                on_restore=on_restore_old,
            ),
            stretch=1,
        )

        grid.addWidget(
            self._create_tag_box(
                side_title=new_title,
                tags=new_tags,
                current_tags=current_tags,
                restore_btn_label="Restore After Tags",
                on_restore=on_restore_new,
            ),
            stretch=1,
        )

        layout.addLayout(grid)

    def _create_tag_box(
        self,
        side_title: str,
        tags: List[str],
        current_tags: List[str],
        restore_btn_label: str,
        on_restore: Optional[Callable[[], None]],
    ) -> QFrame:
        c = self._c
        box = QFrame()
        box.setStyleSheet(
            f"QFrame {{ background-color: {c['background']}; border: 1px solid {c['border']}; border-radius: 4px; }}"
        )
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(8, 6, 8, 8)
        box_layout.setSpacing(6)

        top_bar = QHBoxLayout()
        side_lbl = QLabel(side_title)
        side_lbl.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 11px; font-weight: 600; background: transparent; border: none;"
        )
        top_bar.addWidget(side_lbl)
        top_bar.addStretch()

        # Copy button
        btn_copy = QPushButton("Copy")
        btn_copy.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_copy.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {c['border']}; "
            f"border-radius: 3px; color: {c['text_secondary']}; font-size: 10px; padding: 2px 6px; }}"
            f"QPushButton:hover {{ color: {c['text_primary']}; background: {c['surface_hover']}; border-color: {c['border_focus']}; }}"
        )

        def _do_copy():
            app = QApplication.instance()
            if app:
                clipboard = app.clipboard()
                if clipboard:
                    clipboard.setText(" ".join(tags))
                    btn_copy.setText("Copied!")
                    _safe_timer_set_text(btn_copy, "Copy", 1500)

        btn_copy.clicked.connect(_do_copy)
        top_bar.addWidget(btn_copy)

        is_already_active = sorted(tags) == sorted(current_tags)
        btn_restore = QPushButton()
        btn_restore.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        if is_already_active:
            btn_restore.setText("Current")
            btn_restore.setEnabled(False)
            btn_restore.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {c['border']}; "
                f"border-radius: 3px; color: {c['text_muted']}; font-size: 10px; padding: 2px 6px; }}"
            )
        else:
            btn_restore.setText(restore_btn_label)
            btn_restore.setStyleSheet(
                f"QPushButton {{ background: {c['surface_elevated']}; border: 1px solid {c['primary']}; "
                f"border-radius: 3px; color: {c['text_primary']}; font-size: 10px; font-weight: 600; padding: 2px 8px; }}"
                f"QPushButton:hover {{ background: {c['primary']}; color: {c['text_on_accent']}; }}"
            )
            if on_restore:
                btn_restore.clicked.connect(on_restore)

        top_bar.addWidget(btn_restore)
        box_layout.addLayout(top_bar)

        content_lbl = QLabel()
        content_lbl.setWordWrap(True)
        content_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content_lbl.setStyleSheet(f"color: {c['text_primary']}; font-size: 12px; background: transparent; border: none;")

        if not tags:
            content_lbl.setText(f"<i style='color:{c['text_muted']};'>No tags</i>")
        else:
            chips = [
                f'<span style="background-color: {c["surface_elevated"]}; border: 1px solid {c["border"]}; '
                f'border-radius: 3px; padding: 1px 5px; font-size: 11px;">#{html.escape(t)}</span>'
                for t in tags
            ]
            content_lbl.setText(" ".join(chips))

        box_layout.addWidget(content_lbl)
        return box


# ---------------------------------------------------------------------------
# UnchangedFieldsCollapsible — Drawer for unchanged fields
# ---------------------------------------------------------------------------

class UnchangedFieldsCollapsible(QWidget):
    """Collapsible drawer to view and restore fields unchanged in this commit."""

    def __init__(
        self,
        unchanged_fields: Dict[str, str],
        current_fields: Dict[str, str],
        on_restore_field: Callable[[str, str], None],
        colors: Optional[dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._c = colors or get_colors()
        self._unchanged = unchanged_fields
        self._current_fields = current_fields
        self._on_restore = on_restore_field
        self._expanded = False

        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(0, 4, 0, 4)
        self.layout_root.setSpacing(6)

        count = len(unchanged_fields)
        self.toggle_btn = QPushButton(f"+ Show {count} unchanged fields for this revision")
        self.toggle_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.toggle_btn.setStyleSheet(
            f"QPushButton {{ background: {self._c['surface']}; border: 1px solid {self._c['border']}; "
            f"border-radius: 4px; color: {self._c['text_secondary']}; font-size: 11px; padding: 6px 12px; text-align: left; }}"
            f"QPushButton:hover {{ background: {self._c['surface_hover']}; color: {self._c['text_primary']}; }}"
        )
        self.toggle_btn.clicked.connect(self._toggle)
        self.layout_root.addWidget(self.toggle_btn)

        self.container = QWidget()
        self.container.setVisible(False)
        self.c_layout = QVBoxLayout(self.container)
        self.c_layout.setContentsMargins(0, 0, 0, 0)
        self.c_layout.setSpacing(6)

        for fname, val in unchanged_fields.items():
            card = QFrame()
            card.setStyleSheet(
                f"QFrame {{ background-color: {self._c['surface']}; border: 1px solid {self._c['border']}; "
                f"border-radius: 4px; padding: 4px; }}"
            )
            cl = QHBoxLayout(card)
            cl.setContentsMargins(8, 4, 8, 4)
            cl.setSpacing(8)

            lbl = QLabel(f"<b>{html.escape(fname)}</b>")
            lbl.setStyleSheet(f"color: {self._c['text_primary']}; font-size: 11px; border: none; background: transparent;")
            cl.addWidget(lbl)

            val_preview = val.strip()[:60] if val.strip() else "(Empty)"
            val_lbl = QLabel(html.escape(val_preview))
            val_lbl.setStyleSheet(f"color: {self._c['text_secondary']}; font-size: 11px; border: none; background: transparent;")
            cl.addWidget(val_lbl, stretch=1)

            btn_rest = QPushButton("Restore this field")
            btn_rest.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            is_active = (self._current_fields.get(fname, "") == val)
            if is_active:
                btn_rest.setText("Current")
                btn_rest.setEnabled(False)
                btn_rest.setStyleSheet(
                    f"QPushButton {{ background: transparent; border: 1px solid {self._c['border']}; "
                    f"border-radius: 3px; color: {self._c['text_muted']}; font-size: 10px; padding: 2px 6px; }}"
                )
            else:
                btn_rest.setStyleSheet(
                    f"QPushButton {{ background: {self._c['surface_elevated']}; border: 1px solid {self._c['primary']}; "
                    f"border-radius: 3px; color: {self._c['text_primary']}; font-size: 10px; padding: 2px 6px; }}"
                    f"QPushButton:hover {{ background: {self._c['primary']}; color: {self._c['text_on_accent']}; }}"
                )

                def make_restore(fn=fname, v=val):
                    return lambda: self._on_restore(fn, v)

                btn_rest.clicked.connect(make_restore())

            cl.addWidget(btn_rest)
            self.c_layout.addWidget(card)

        self.layout_root.addWidget(self.container)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self.container.setVisible(self._expanded)
        count = len(self._unchanged)
        if self._expanded:
            self.toggle_btn.setText(f"- Hide {count} unchanged fields")
        else:
            self.toggle_btn.setText(f"+ Show {count} unchanged fields for this revision")


# ---------------------------------------------------------------------------
# NoteHistoryDialog — Main two-column note history dialog
# ---------------------------------------------------------------------------

class NoteHistoryDialog(QDialog):
    """Two-column dialog displaying commit history and diffs for a note."""

    def __init__(
        self,
        deck_hash: str,
        guid: str,
        note_id: Optional[int] = None,
        editor: Optional[Any] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.deck_hash = deck_hash
        self.guid = guid
        self.note_id = note_id
        self.editor = editor
        self.colors = get_colors()

        # Try to resolve local note_id if not explicitly provided
        if self.note_id is None and mw and getattr(mw, "col", None):
            try:
                nids = mw.col.find_notes(f"guid:{self.guid}")
                if isinstance(nids, (list, tuple)) and nids:
                    self.note_id = nids[0]
            except Exception:
                pass

        # Load local note snapshot if available
        self._current_fields: Dict[str, str] = {}
        self._current_tags: List[str] = []
        if self.note_id and mw and getattr(mw, "col", None):
            try:
                note = mw.col.get_note(self.note_id)
                if note and hasattr(note, "items") and callable(note.items):
                    items = note.items()
                    if isinstance(items, (list, tuple, dict)):
                        self._current_fields = dict(items)
                if note and hasattr(note, "tags") and isinstance(note.tags, (list, tuple)):
                    self._current_tags = list(note.tags)
            except Exception:
                pass

        self._commit_groups: List[Dict[str, Any]] = []
        self._selected_group: Optional[Dict[str, Any]] = None
        self._filter_field: str = "ALL"
        self._render_mode: str = "diff"
        self._comparison_cards: List[FieldComparisonCard] = []

        self.setWindowTitle("AnkiCollab - Note History")
        self.resize(920, 640)

        # Close shortcuts
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
            f"border: 1px solid {c['border']}; border-radius: 4px; padding: 4px 10px; font-size: 12px; }}"
            f"QPushButton:hover {{ background-color: {c['surface_hover']}; border-color: {c['border_focus']}; }}"
            f"QComboBox {{ background-color: {c['surface_elevated']}; color: {c['text_primary']}; "
            f"border: 1px solid {c['border']}; border-radius: 4px; padding: 2px 8px; font-size: 11px; }}"
            f"QComboBox::drop-down {{ border: none; }}"
            f"QSplitter::handle {{ background: {c['border']}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # ── Header bar ────────────────────────────────────────────────────────
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)

        note_title = f"Note History"
        if self.note_id:
            note_title += f" #{self.note_id}"
        title_label = QLabel(note_title)
        title_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        header_layout.addWidget(title_label)

        guid_label = QLabel(f"(GUID: {self.guid[:12]}...)")
        guid_label.setStyleSheet(f"color: {c['text_secondary']}; font-size: 11px;")
        header_layout.addWidget(guid_label)

        header_layout.addStretch()

        self.status_label = QLabel("Loading...")
        self.status_label.setStyleSheet(f"color: {c['text_secondary']}; font-size: 12px;")
        header_layout.addWidget(self.status_label)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        refresh_btn.clicked.connect(self._load_history)
        header_layout.addWidget(refresh_btn)

        root.addLayout(header_layout)

        # ── Secondary Toolbar (Filter & Render mode) ─────────────────────────
        toolbar = QFrame()
        toolbar.setStyleSheet(
            f"background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 4px;"
        )
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(10, 4, 10, 4)
        tb_layout.setSpacing(10)

        filter_lbl = QLabel("Filter:")
        filter_lbl.setStyleSheet(f"color: {c['text_secondary']}; font-size: 11px; background: transparent; border: none;")
        tb_layout.addWidget(filter_lbl)

        self._filter_combo = QComboBox()
        self._filter_combo.addItem("All modified fields", "ALL")
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        tb_layout.addWidget(self._filter_combo)

        tb_layout.addSpacing(12)

        render_lbl = QLabel("View Mode:")
        render_lbl.setStyleSheet(f"color: {c['text_secondary']}; font-size: 11px; background: transparent; border: none;")
        tb_layout.addWidget(render_lbl)

        self._render_combo = QComboBox()
        self._render_combo.addItem("Visual Diff (Before | After)", "diff")
        self._render_combo.addItem("Rendered HTML", "render")
        self._render_combo.addItem("Raw HTML", "raw")
        self._render_combo.currentIndexChanged.connect(self._on_render_mode_changed)
        tb_layout.addWidget(self._render_combo)

        tb_layout.addStretch()
        root.addWidget(toolbar)

        # ── Body Splitter (Left: Commit list | Right: Version comparison) ─────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)

        # Left Column: Commit list
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        self._commit_list = QListWidget()
        self._commit_list.setStyleSheet(
            f"QListWidget {{ background-color: {c['surface']}; border: 1px solid {c['border']}; "
            f"border-radius: 4px; outline: none; padding: 2px 0px; }}"
            f"QListWidget::item {{ padding: 8px 10px; border-bottom: 1px solid {c['border']}; color: {c['text_primary']}; }}"
            f"QListWidget::item:selected {{ background-color: {c['surface_elevated']}; color: {c['text_primary']}; "
            f"border-left: 3px solid {c['primary']}; }}"
            f"QListWidget::item:hover:!selected {{ background-color: {c['surface_hover']}; }}"
        )
        self._commit_list.currentRowChanged.connect(self._on_commit_selected)
        left_layout.addWidget(self._commit_list)
        splitter.addWidget(left_widget)

        # Right Column: Comparison area
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)

        # Version Banner
        self._banner_frame = QFrame()
        self._banner_frame.setStyleSheet(
            f"QFrame {{ background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 4px; padding: 6px; }}"
        )
        banner_layout = QVBoxLayout(self._banner_frame)
        banner_layout.setContentsMargins(8, 6, 8, 6)
        banner_layout.setSpacing(4)

        b_top = QHBoxLayout()
        self._banner_title = QLabel("Select a revision")
        self._banner_title.setStyleSheet("font-size: 13px; font-weight: bold; background: transparent; border: none;")
        b_top.addWidget(self._banner_title)

        self._banner_badge = QLabel("")
        self._banner_badge.setStyleSheet("font-size: 11px; font-weight: 600; background: transparent; border: none;")
        b_top.addWidget(self._banner_badge)

        b_top.addStretch()

        self._btn_restore_all = QPushButton("Restore entire note to this revision")
        self._btn_restore_all.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._btn_restore_all.setStyleSheet(
            f"QPushButton {{ background: {c['surface_elevated']}; border: 1px solid {c['primary']}; "
            f"border-radius: 3px; color: {c['text_primary']}; font-size: 11px; font-weight: 600; padding: 4px 10px; }}"
            f"QPushButton:hover {{ background: {c['primary']}; color: {c['text_on_accent']}; }}"
        )
        self._btn_restore_all.clicked.connect(self._on_restore_full_commit)
        self._btn_restore_all.setEnabled(bool(self.note_id))
        b_top.addWidget(self._btn_restore_all)

        banner_layout.addLayout(b_top)

        self._banner_rationale = QLabel("")
        self._banner_rationale.setWordWrap(True)
        self._banner_rationale.setStyleSheet(
            f"font-style: italic; color: {c['text_secondary']}; background-color: {c['surface_elevated']}; "
            f"padding: 4px 8px; border-radius: 4px; border: none;"
        )
        self._banner_rationale.setVisible(False)
        banner_layout.addWidget(self._banner_rationale)

        right_layout.addWidget(self._banner_frame)

        # Scroll Area for Field Comparison Cards
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(f"background-color: {c['background']}; border: none;")

        self._cards_container = QWidget()
        self._cards_container.setStyleSheet(f"background-color: {c['background']};")
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(10)
        self._cards_layout.addStretch()

        self._scroll.setWidget(self._cards_container)
        right_layout.addWidget(self._scroll, stretch=1)

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 620])

        root.addWidget(splitter, stretch=1)

        # ── Bottom Bar ────────────────────────────────────────────────────────
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        self._feedback_label = QLabel("")
        self._feedback_label.setStyleSheet(f"color: {c.get('success', '#34D399')}; font-size: 11px; font-weight: 500;")
        bottom_layout.addWidget(self._feedback_label)

        bottom_layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.clicked.connect(self.close)
        bottom_layout.addWidget(close_btn)

        root.addLayout(bottom_layout)

    def _load_history(self) -> None:
        payload = {
            "deck_hash": self.deck_hash,
            "guid": self.guid,
            "limit": 100,
        }

        self.status_label.setText("Loading history...")

        def _fetch():
            return api_client.post_json("/GetNoteHistory", payload, timeout=15)

        def _on_done(future):
            try:
                response = future.result()
                if not self.isVisible() and not self.isActiveWindow():
                    return
                if response.status_code == 200:
                    data = response.json()
                    self._render_history_data(data)
                elif response.status_code == 404:
                    self.status_label.setText("Note not found on server")
                    self._render_empty_list("This note was not found on AnkiCollab.")
                else:
                    self.status_label.setText(f"Error {response.status_code}")
                    self._render_empty_list(f"The server returned status {response.status_code}.")
            except Exception as err:
                logger.warning(f"Error fetching note history: {err}")
                self.status_label.setText("Network error")
                self._render_empty_list("Unable to reach the AnkiCollab server.")

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
                self._render_empty_list("Unable to reach the AnkiCollab server.")

    def _render_empty_list(self, message: str) -> None:
        c = self.colors
        self._commit_list.clear()
        item = QListWidgetItem(f"  {message}")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self._commit_list.addItem(item)
        self._clear_detail_view(message)

    def _clear_detail_view(self, message: str = "No revision selected") -> None:
        c = self.colors
        self._comparison_cards.clear()
        _clear_layout(self._cards_layout)

        lbl = QLabel(message)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color: {c['text_secondary']}; padding: 40px;")
        self._cards_layout.addWidget(lbl)
        self._cards_layout.addStretch()

    def _render_history_data(self, data: Dict[str, Any]) -> None:
        self._commit_groups = data.get("groups", [])
        group_count = len(self._commit_groups)
        self.status_label.setText(f"{group_count} revision{'s' if group_count > 1 else ''} recorded")

        # Collect distinct field names across all commits to populate filter combo
        distinct_fields = set()
        for grp in self._commit_groups:
            for ev in grp.get("events", []):
                fname = ev.get("field_name")
                if fname:
                    distinct_fields.add(fname)

        self._filter_combo.blockSignals(True)
        self._filter_combo.clear()
        self._filter_combo.addItem("All modified fields", "ALL")
        for fn in sorted(distinct_fields):
            self._filter_combo.addItem(f"Field: {fn}", fn)
        self._filter_combo.addItem("Tags only", "__tags__")
        self._filter_combo.blockSignals(False)

        self._populate_commit_list()

    def _populate_commit_list(self) -> None:
        self._commit_list.blockSignals(True)
        self._commit_list.clear()

        if not self._commit_groups:
            self._render_empty_list("No history events for this note.")
            self._commit_list.blockSignals(False)
            return

        for idx, grp in enumerate(self._commit_groups):
            cid = grp.get("commit_id")
            author = grp.get("commit_author") or "Anonymous"
            timestamp = grp.get("commit_timestamp") or ""
            approved = grp.get("approved", False)
            denied = grp.get("denied", False)

            if approved:
                status_badge = "[Approved]"
            elif denied:
                status_badge = "[Denied]"
            else:
                status_badge = "[Pending]"

            # Summarize changed fields in this commit
            changed_fields = []
            has_tags = False
            for ev in grp.get("events", []):
                ev_type = ev.get("event_type", "")
                fname = ev.get("field_name")
                if fname and fname not in changed_fields:
                    changed_fields.append(fname)
                if "tag" in ev_type:
                    has_tags = True

            if changed_fields and has_tags:
                summary = f"{len(changed_fields)} field(s) + tags modified"
            elif changed_fields:
                sample = ", ".join(changed_fields[:2])
                if len(changed_fields) > 2:
                    sample += f" (+{len(changed_fields) - 2})"
                summary = f"{len(changed_fields)} field(s): {sample}"
            elif has_tags:
                summary = "Tags modified"
            else:
                summary = "Initial revision"

            title = f"Commit #{cid}" if cid else "Initial revision"
            item_text = f"{title}  {status_badge}\nby {author} · {timestamp}\n{summary}"

            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, idx)
            item.setSizeHint(QSize(240, 56))
            self._commit_list.addItem(item)

        self._commit_list.blockSignals(False)

        # Select first row by default
        cnt = self._commit_list.count()
        if isinstance(cnt, int) and cnt > 0:
            self._commit_list.setCurrentRow(0)
        elif self._commit_groups:
            self._on_commit_selected(0)

    def _on_commit_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._commit_groups):
            self._clear_detail_view()
            return
        self._selected_group = self._commit_groups[row]
        self._render_selected_commit()

    def _on_filter_changed(self) -> None:
        self._filter_field = self._filter_combo.currentData() or "ALL"
        self._render_selected_commit()

    def _on_render_mode_changed(self) -> None:
        self._render_mode = self._render_combo.currentData() or "diff"
        for card in self._comparison_cards:
            card.set_render_mode(self._render_mode)

    def _render_selected_commit(self) -> None:
        c = self.colors
        grp = self._selected_group
        if not grp:
            self._clear_detail_view()
            return

        cid = grp.get("commit_id")
        author = grp.get("commit_author") or "Anonymous"
        timestamp = grp.get("commit_timestamp") or ""
        rationale = grp.get("commit_rationale") or ""
        approved = grp.get("approved", False)
        denied = grp.get("denied", False)

        # Update Banner
        title_str = f"Commit #{cid}" if cid else "Initial revision"
        self._banner_title.setText(f"{title_str} by {author} ({timestamp})")

        if approved:
            self._banner_badge.setText("[Approved]")
            self._banner_badge.setStyleSheet(f"color: {c.get('success', '#34D399')}; font-size: 11px; font-weight: 600; background: transparent; border: none;")
        elif denied:
            self._banner_badge.setText("[Denied]")
            self._banner_badge.setStyleSheet(f"color: {c.get('danger', '#F87171')}; font-size: 11px; font-weight: 600; background: transparent; border: none;")
        else:
            self._banner_badge.setText("[Pending review]")
            self._banner_badge.setStyleSheet(f"color: {c.get('warning', '#F59E0B')}; font-size: 11px; font-weight: 600; background: transparent; border: none;")

        if rationale:
            self._banner_rationale.setText(f"Rationale: {rationale}")
            self._banner_rationale.setVisible(True)
        else:
            self._banner_rationale.setVisible(False)

        # Rebuild Cards Layout
        self._comparison_cards.clear()
        _clear_layout(self._cards_layout)

        # Group events by field and tags
        field_events: Dict[str, Dict[str, Any]] = {}
        old_tags_list: List[str] = []
        new_tags_list: List[str] = []
        has_tag_change = False

        for ev in grp.get("events", []):
            ev_type = ev.get("event_type", "")
            fname = ev.get("field_name")
            if fname:
                field_events[fname] = {
                    "old": ev.get("old_human") or "",
                    "new": ev.get("new_human") or "",
                    "diff_html": ev.get("diff_html"),
                    "type": ev_type,
                }
            elif "tag" in ev_type:
                has_tag_change = True
                old_h = ev.get("old_human") or ""
                new_h = ev.get("new_human") or ""
                if old_h:
                    tag_clean = old_h.lstrip("#").strip()
                    if tag_clean and tag_clean not in old_tags_list:
                        old_tags_list.append(tag_clean)
                if new_h:
                    tag_clean = new_h.lstrip("#").strip()
                    if tag_clean and tag_clean not in new_tags_list:
                        new_tags_list.append(tag_clean)

        rendered_count = 0

        # Render field comparison cards
        for fname, data in field_events.items():
            if self._filter_field != "ALL" and self._filter_field != fname:
                continue

            old_val = data["old"]
            new_val = data["new"]
            current_val = self._current_fields.get(fname, "")

            def make_restore(fn=fname, val=""):
                return lambda: self._restore_field_to_anki(fn, val)

            card = FieldComparisonCard(
                field_name=fname,
                old_val=old_val,
                new_val=new_val,
                current_val=current_val,
                old_title="Before this commit",
                new_title="After this commit",
                render_mode=self._render_mode,
                on_restore_old=make_restore(fname, old_val) if self.note_id else None,
                on_restore_new=make_restore(fname, new_val) if self.note_id else None,
                colors=self.colors,
            )
            self._comparison_cards.append(card)
            self._cards_layout.addWidget(card)
            rendered_count += 1

        # Render tags comparison card
        if has_tag_change and (self._filter_field in ("ALL", "__tags__")):
            def make_restore_tags(tags: List[str]):
                return lambda: self._restore_field_to_anki("__tags__", tags)

            tags_card = TagsComparisonCard(
                old_tags=old_tags_list,
                new_tags=new_tags_list,
                current_tags=self._current_tags,
                old_title="Before this commit",
                new_title="After this commit",
                on_restore_old=make_restore_tags(old_tags_list) if self.note_id else None,
                on_restore_new=make_restore_tags(new_tags_list) if self.note_id else None,
                colors=self.colors,
            )
            self._cards_layout.addWidget(tags_card)
            rendered_count += 1

        # Unchanged fields collapsible drawer
        if self.note_id and self._current_fields and self._filter_field == "ALL":
            unchanged = {
                k: v for k, v in self._current_fields.items()
                if k not in field_events
            }
            if unchanged:
                drawer = UnchangedFieldsCollapsible(
                    unchanged_fields=unchanged,
                    current_fields=self._current_fields,
                    on_restore_field=self._restore_field_to_anki,
                    colors=self.colors,
                )
                self._cards_layout.addWidget(drawer)

        if rendered_count == 0:
            empty_lbl = QLabel("No modified fields match the selected filter.")
            empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_lbl.setStyleSheet(f"color: {c['text_secondary']}; padding: 30px;")
            self._cards_layout.addWidget(empty_lbl)

        self._cards_layout.addStretch()

    def _restore_field_to_anki(self, field_name: str, value: Any) -> None:
        """Granular field restoration directly into the local Anki note."""
        if not mw or not getattr(mw, "col", None) or not self.note_id:
            return

        try:
            note = mw.col.get_note(self.note_id)
            if not note:
                return

            if field_name == "__tags__":
                if isinstance(value, list):
                    note.tags = value
                else:
                    note.tags = [t for t in str(value).strip().split() if t]
                self._current_tags = list(note.tags)
            elif field_name in note:
                note[field_name] = str(value)
                self._current_fields[field_name] = str(value)
            else:
                return

            if hasattr(mw, "checkpoint"):
                mw.checkpoint(f"AnkiCollab: Restore {field_name}")

            mw.col.update_note(note)
            self._refresh_anki_views()

            self._feedback_label.setText(f"Field '{field_name}' restored successfully.")
            _safe_timer_set_text(self._feedback_label, "", 4000)

            if hasattr(aqt.utils, "tooltip"):
                aqt.utils.tooltip(f"Field '{field_name}' restored into note", parent=self)

            # Re-render to update 'Current' states
            self._render_selected_commit()

        except Exception as err:
            logger.warning(f"Error restoring field {field_name}: {err}")
            self._feedback_label.setText(f"Error restoring field: {err}")

    def _on_restore_full_commit(self) -> None:
        """Restore all changed fields in the selected commit into the local Anki note."""
        if not mw or not getattr(mw, "col", None) or not self.note_id or not self._selected_group:
            return

        try:
            note = mw.col.get_note(self.note_id)
            if not note:
                return

            if hasattr(mw, "checkpoint"):
                mw.checkpoint("AnkiCollab: Restore revision")

            restored_count = 0
            for ev in self._selected_group.get("events", []):
                fname = ev.get("field_name")
                if fname and fname in note:
                    val = ev.get("new_human") or ""
                    note[fname] = val
                    self._current_fields[fname] = val
                    restored_count += 1

            mw.col.update_note(note)
            self._refresh_anki_views()

            self._feedback_label.setText(f"Revision restored ({restored_count} field(s) updated).")
            _safe_timer_set_text(self._feedback_label, "", 4000)

            if hasattr(aqt.utils, "tooltip"):
                aqt.utils.tooltip(f"Note restored ({restored_count} fields)", parent=self)

            self._render_selected_commit()

        except Exception as err:
            logger.warning(f"Error restoring full commit: {err}")
            self._feedback_label.setText(f"Error restoring revision: {err}")

    def _refresh_anki_views(self) -> None:
        """Propagate changes to open Editor, Reviewer, and Browser."""
        if not mw or not self.note_id:
            return

        # Editor
        if self.editor and hasattr(self.editor, "loadNote"):
            try:
                self.editor.loadNote()
            except Exception:
                pass

        # Reviewer
        if (
            hasattr(mw, "reviewer")
            and mw.reviewer
            and getattr(mw.reviewer, "card", None)
            and mw.reviewer.card.nid == self.note_id
        ):
            try:
                mw.reviewer.card.load()
                mw.reviewer.show()
            except Exception:
                pass

        # Browser
        if hasattr(mw, "browser") and mw.browser:
            try:
                mw.browser.loadNote()
                mw.browser.on_notes_changed()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def show_note_history_dialog(
    deck_hash: str,
    guid: str,
    note_id: Optional[int] = None,
    editor: Optional[Any] = None,
    parent: Optional[QWidget] = None,
) -> None:
    dialog = NoteHistoryDialog(
        deck_hash=deck_hash,
        guid=guid,
        note_id=note_id,
        editor=editor,
        parent=parent or mw,
    )
    dialog.exec()


def open_note_history_for_note_id(note_id: int, editor=None) -> None:
    """Helper to open NoteHistoryDialog from editor or browser."""
    if not mw or not getattr(mw, "col", None):
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
    show_note_history_dialog(
        deck_hash=deck_hash,
        guid=guid,
        note_id=note_id,
        editor=editor,
        parent=editor.parentWindow if editor else mw,
    )
