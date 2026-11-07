"""AnkiCollab - Update Review Module (Diff Import style).

Allows reviewing note updates one by one and field by field before applying.
Supports filtering unsuspended notes only, keeping local versions transiently,
permanently protecting fields via AnkiCollab_Protect::<Field>, and configuring
policy for suspended cards (auto-accept or keep local).
Includes comprehensive confirmation dialogues before applying or closing.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

import aqt
from aqt import mw
from aqt.qt import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
    Qt,
    QKeySequence,
    QShortcut,
    QCloseEvent,
)
from aqt.theme import theme_manager

from .ui.colors import get_colors
from .utils import get_logger
from .var_defs import PREFIX_PROTECTED_FIELDS

if TYPE_CHECKING:
    from anki.collection import Collection
    from .crowd_anki.importer.import_dialog import ImportConfig
    from .crowd_anki.representation.deck import Deck

logger = get_logger("ankicollab.update_review")


@dataclass
class NoteFieldDiff:
    name: str
    old_val: str
    new_val: str
    changed: bool
    is_protected: bool = False
    choice: str = "remote"  # "remote" or "local"
    permanent_protect: bool = False

    def diff_html(self, colors: dict) -> str:
        """Generate subtle inline diff HTML without external dependencies."""
        if not self.changed:
            return (
                f'<div style="color: {colors["text_muted"]}; font-size: 12px; padding: 6px 8px;">'
                f"{html.escape(self.new_val or '(empty)')}</div>"
            )

        old_clean = html.escape(self.old_val or "")
        new_clean = html.escape(self.new_val or "")

        bg_del = "#4A2020" if theme_manager.night_mode else "#FEE2E2"
        border_del = "#7F1D1D" if theme_manager.night_mode else "#FECACA"
        text_del = "#FFAAAA" if theme_manager.night_mode else "#991B1B"

        bg_ins = "#1F3E28" if theme_manager.night_mode else "#DCFCE7"
        border_ins = "#14532D" if theme_manager.night_mode else "#BBF7D0"
        text_ins = "#9AE6B4" if theme_manager.night_mode else "#166534"

        del_span = (
            f'<div style="margin-bottom: 6px;">'
            f'<div style="font-size: 11px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.5px; color: {text_del}; margin-bottom: 2px;">Local: (Current note)</div>'
            f'<div style="background-color: {bg_del}; border: 1px solid {border_del}; color: {text_del}; '
            f'padding: 8px 10px; border-radius: 4px; font-size: 13px; line-height: 1.45;">'
            f'<strike>{old_clean if old_clean else "(empty)"}</strike></div></div>'
        )
        ins_span = (
            f'<div>'
            f'<div style="font-size: 11px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.5px; color: {text_ins}; margin-bottom: 2px;">AnkiCollab: (Incoming update)</div>'
            f'<div style="background-color: {bg_ins}; border: 1px solid {border_ins}; color: {text_ins}; '
            f'padding: 8px 10px; border-radius: 4px; font-size: 13px; line-height: 1.45;">'
            f'{new_clean if new_clean else "(empty)"}</div></div>'
        )
        return f"{del_span}{ins_span}"


@dataclass
class NoteUpdateItem:
    guid: str
    nid: int
    deck_name: str
    notetype_name: str
    is_suspended: bool
    field_diffs: List[NoteFieldDiff] = field(default_factory=list)
    tags_local: List[str] = field(default_factory=list)
    tags_remote: List[str] = field(default_factory=list)
    tags_added: List[str] = field(default_factory=list)
    tags_removed: List[str] = field(default_factory=list)
    tags_choice: str = "remote"  # "remote" or "local"
    note_choice: str = "remote"  # "remote", "local", or "custom"

    @property
    def tags_changed(self) -> bool:
        return bool(self.tags_added or self.tags_removed)

    @property
    def has_changes(self) -> bool:
        return any(f.changed for f in self.field_diffs) or self.tags_changed

    @property
    def changed_fields_count(self) -> int:
        return sum(1 for f in self.field_diffs if f.changed)

    @property
    def display_title(self) -> str:
        for f in self.field_diffs:
            if f.old_val or f.new_val:
                raw = f.new_val or f.old_val
                clean = re.sub(r"<[^>]+>", " ", raw).strip()
                if clean:
                    return clean[:70] + ("..." if len(clean) > 70 else "")
        return f"Note {self.guid[:8]}"


@dataclass
class UpdatePlan:
    deck_hash: str
    items: List[NoteUpdateItem] = field(default_factory=list)
    new_notes_count: int = 0
    unchanged_notes_count: int = 0
    only_unsuspended: bool = False
    auto_accept_suspended: bool = True  # User toggle: whether to auto-accept or keep local for suspended cards

    @property
    def total_changed(self) -> int:
        return len(self.items)

    @property
    def total_unsuspended(self) -> int:
        return sum(1 for item in self.items if not item.is_suspended)

    @property
    def total_suspended(self) -> int:
        return sum(1 for item in self.items if item.is_suspended)

    def apply_to_config(self, import_config: ImportConfig) -> None:
        """Transfer user decisions into ImportConfig for the actual import."""
        transient: Dict[str, List[str]] = {}
        skip_guids: Set[str] = set()
        permanent: Dict[str, List[str]] = {}

        for item in self.items:
            # Policy for suspended cards when only_unsuspended review mode is active
            if self.only_unsuspended and item.is_suspended:
                if not self.auto_accept_suspended:
                    # User chose to keep suspended cards untouched (local)
                    skip_guids.add(item.guid)
                    continue
                # If auto_accept_suspended is True, proceed with normal remote application

            if item.note_choice == "local":
                # User chose to keep entire note
                skip_guids.add(item.guid)
                continue

            # Field by field decisions
            fields_to_protect_transient = []
            fields_to_protect_permanent = []

            for f in item.field_diffs:
                if f.choice == "local" or (item.note_choice == "remote" and f.is_protected):
                    fields_to_protect_transient.append(f.name)
                if f.permanent_protect:
                    fields_to_protect_permanent.append(f.name)

            if item.tags_choice == "local":
                fields_to_protect_transient.append("Tags")

            if fields_to_protect_transient:
                transient[item.guid] = fields_to_protect_transient
            if fields_to_protect_permanent:
                permanent[item.guid] = fields_to_protect_permanent

        import_config.transient_protections = transient
        import_config.skip_update_guids = skip_guids
        import_config.permanent_protections = permanent


def build_update_plan(
    deck: Deck,
    collection: Collection,
    import_config: ImportConfig,
    only_unsuspended: bool = False,
    auto_accept_suspended: bool = True,
) -> UpdatePlan:
    """Build a dry-run update plan comparing local collection vs incoming deck notes.

    Performs NO write operations to the collection database.
    """
    plan = UpdatePlan(
        deck_hash=import_config.deck_hash or "",
        only_unsuspended=only_unsuspended,
        auto_accept_suspended=auto_accept_suspended,
    )

    all_notes = []
    note_to_deck_map: Dict[str, str] = {}
    deck._collect_all_notes(all_notes, note_to_deck_map, "", import_config.home_deck)

    if not all_notes or not collection or not getattr(collection, "db", None):
        return plan

    note_uuids = [n.get_uuid() for n in all_notes]
    chunk_size = 1000
    existing_notes: List[Tuple[str, int]] = []

    for i in range(0, len(note_uuids), chunk_size):
        chunk = note_uuids[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        try:
            existing_notes.extend(
                collection.db.all(
                    f"SELECT guid, id FROM notes WHERE guid IN ({placeholders})", *chunk
                )
            )
        except Exception as err:
            logger.warning(f"Error querying existing notes for plan: {err}")

    existing_map = {guid: nid for guid, nid in existing_notes}
    new_notes = [n for n in all_notes if n.get_uuid() not in existing_map]
    plan.new_notes_count = len(new_notes)

    # Pre-fetch card suspension status for all existing notes
    existing_nids = list(existing_map.values())
    suspended_nids = set()

    for i in range(0, len(existing_nids), chunk_size):
        chunk = existing_nids[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        try:
            # A note is suspended if ALL its cards are in queue -1
            card_rows = collection.db.all(
                f"SELECT nid, queue FROM cards WHERE nid IN ({placeholders})", *chunk
            )
            nid_cards: Dict[int, List[int]] = {}
            for nid, queue in card_rows:
                nid_cards.setdefault(nid, []).append(queue)
            for nid, queues in nid_cards.items():
                if queues and all(q == -1 for q in queues):
                    suspended_nids.add(nid)
        except Exception as err:
            logger.warning(f"Error querying card suspension for plan: {err}")

    # Pre-fetch raw note fields and tags to avoid slow N+1 collection.get_note calls for unchanged notes
    raw_note_cache: Dict[int, Tuple[str, str]] = {}
    for i in range(0, len(existing_nids), chunk_size):
        chunk = existing_nids[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        try:
            note_rows = collection.db.all(
                f"SELECT id, flds, tags FROM notes WHERE id IN ({placeholders})", *chunk
            )
            for row_id, flds, tags in note_rows:
                raw_note_cache[row_id] = (flds or "", tags or "")
        except Exception as err:
            logger.warning(f"Error querying note fields for plan: {err}")

    # Build NoteUpdateItem for each existing note
    from anki.notes import Note as AnkiNote

    for remote_note in all_notes:
        guid = remote_note.get_uuid()
        if guid not in existing_map:
            continue

        nid = existing_map[guid]
        remote_fields = remote_note.anki_object_dict.get("fields", [])
        remote_tags = remote_note.anki_object_dict.get("tags", [])
        if nid in raw_note_cache:
            raw_flds, raw_tags = raw_note_cache[nid]
            if raw_flds == "\x1f".join(remote_fields):
                local_tag_set = set(raw_tags.strip().split()) if raw_tags.strip() else set()
                remote_tag_set = set(remote_tags)
                if local_tag_set == remote_tag_set:
                    plan.unchanged_notes_count += 1
                    continue
        try:
            if hasattr(collection, "get_note"):
                local_anki_note = collection.get_note(nid)
            else:
                local_anki_note = AnkiNote(collection, id=nid)
        except Exception:
            try:
                local_anki_note = AnkiNote(collection, id=nid)
            except Exception as err:
                logger.warning(f"Could not load local note {nid} ({guid}): {err}")
                continue

        model = (
            local_anki_note.note_type()
            if hasattr(local_anki_note, "note_type")
            else local_anki_note.model()
        )
        model_name = model.get("name", "Unknown") if model else "Unknown"
        flds = model.get("flds", []) if model else []
        field_names = [f.get("name", f"Field {idx}") for idx, f in enumerate(flds)]

        local_fields = getattr(local_anki_note, "fields", [])
        remote_fields = remote_note.anki_object_dict.get("fields", [])

        # Check existing protection tags on local note
        local_tags = list(getattr(local_anki_note, "tags", []))
        remote_tags = list(remote_note.anki_object_dict.get("tags", []))

        protected_tags = {
            t.split("::", 1)[1]
            for t in local_tags
            if t.startswith(PREFIX_PROTECTED_FIELDS) and "::" in t
        }
        all_protected = "All" in protected_tags

        field_diffs: List[NoteFieldDiff] = []
        num_fields = max(len(field_names), len(local_fields), len(remote_fields))

        for idx in range(num_fields):
            fname = field_names[idx] if idx < len(field_names) else f"Field {idx}"
            old_val = local_fields[idx] if idx < len(local_fields) else ""
            new_val = remote_fields[idx] if idx < len(remote_fields) else ""
            changed = old_val != new_val
            is_prot = all_protected or (fname in protected_tags) or import_config.is_personal_field(model_name, fname)

            diff = NoteFieldDiff(
                name=fname,
                old_val=old_val,
                new_val=new_val,
                changed=changed,
                is_protected=is_prot,
                choice="local" if is_prot else "remote",
                permanent_protect=is_prot,
            )
            field_diffs.append(diff)

        tags_added = [t for t in remote_tags if t not in local_tags]
        tags_removed = [t for t in local_tags if t not in remote_tags and not t.startswith(PREFIX_PROTECTED_FIELDS)]

        is_suspended = nid in suspended_nids
        target_deck = note_to_deck_map.get(guid, "")

        item = NoteUpdateItem(
            guid=guid,
            nid=nid,
            deck_name=target_deck,
            notetype_name=model_name,
            is_suspended=is_suspended,
            field_diffs=field_diffs,
            tags_local=local_tags,
            tags_remote=remote_tags,
            tags_added=tags_added,
            tags_removed=tags_removed,
        )

        if item.has_changes:
            plan.items.append(item)
        else:
            plan.unchanged_notes_count += 1

    return plan


class ReviewConfirmationDialog(QDialog):
    """Clean, sober validation dialog summarizing the final update plan before applying."""

    def __init__(
        self,
        plan: UpdatePlan,
        deck_name: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.plan = plan
        self.deck_name = deck_name
        self.colors = get_colors()

        self.setWindowTitle("Confirm Update Plan")
        self.setModal(True)
        self.resize(540, 420)
        self._init_ui()

    def _init_ui(self) -> None:
        c = self.colors
        self.setStyleSheet(
            f"QDialog {{ background-color: {c['background']}; color: {c['text_primary']}; }}"
            f"QLabel {{ color: {c['text_primary']}; }}"
            f"QPushButton {{ background-color: {c['surface_elevated']}; color: {c['text_primary']}; "
            f"border: 1px solid {c['border']}; border-radius: 4px; padding: 7px 16px; font-size: 13px; }}"
            f"QPushButton:hover {{ background-color: {c['surface_hover']}; border-color: {c['border_focus']}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(16)

        # Title & Deck
        title = QLabel("Confirm and Apply Updates")
        title.setStyleSheet("font-size: 17px; font-weight: bold;")
        layout.addWidget(title)

        deck_lbl = QLabel(f"Target Deck: {self.deck_name}")
        deck_lbl.setStyleSheet(f"font-size: 13px; color: {c['text_secondary']};")
        layout.addWidget(deck_lbl)

        # Calculate statistics
        total = len(self.plan.items)
        count_local_notes = sum(1 for it in self.plan.items if it.note_choice == "local")
        count_remote_notes = sum(1 for it in self.plan.items if it.note_choice == "remote")
        count_custom_notes = sum(1 for it in self.plan.items if it.note_choice == "custom")
        count_perm_protect = sum(
            sum(1 for f in it.field_diffs if f.permanent_protect) for it in self.plan.items
        )

        # Summary box
        summary_box = QWidget()
        summary_box.setStyleSheet(
            f"border: 1px solid {c['border']}; border-radius: 6px; background-color: {c['surface']}; padding: 14px;"
        )
        s_layout = QVBoxLayout(summary_box)
        s_layout.setSpacing(8)

        def add_stat_row(label_text: str, value_text: str, highlight: bool = False):
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setStyleSheet(f"font-size: 13px; color: {c['text_primary']};")
            val = QLabel(value_text)
            val.setStyleSheet(
                f"font-size: 13px; font-weight: bold; color: {c['primary'] if highlight else c['text_primary']};"
            )
            row.addWidget(lbl)
            row.addStretch()
            row.addWidget(val)
            s_layout.addLayout(row)

        add_stat_row("Total modified notes to update:", str(total))
        add_stat_row("• Notes accepted from AnkiCollab:", str(count_remote_notes))
        add_stat_row("• Notes kept local (skipped):", str(count_local_notes))
        if count_custom_notes > 0:
            add_stat_row("• Notes with custom field choices:", str(count_custom_notes))
        if count_perm_protect > 0:
            add_stat_row("• Fields permanently protected:", str(count_perm_protect), highlight=True)

        if self.plan.only_unsuspended:
            suspended_count = self.plan.total_suspended
            policy_text = "Auto-accept remote" if self.plan.auto_accept_suspended else "Keep local (skipped)"
            add_stat_row(
                f"Suspended notes policy ({suspended_count} hidden):",
                policy_text,
                highlight=True,
            )

        layout.addWidget(summary_box)

        # Notice
        notice_lbl = QLabel(
            "An automatic collection backup will be created before applying these changes. "
            "You can also inspect revisions in Note History at any time."
        )
        notice_lbl.setWordWrap(True)
        notice_lbl.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        layout.addWidget(notice_lbl)

        layout.addStretch()

        # Action Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        back_btn = QPushButton("Back to Review")
        back_btn.clicked.connect(self.reject)
        btn_layout.addWidget(back_btn)

        confirm_btn = QPushButton("Confirm & Apply Updates")
        confirm_btn.setDefault(True)
        confirm_btn.setStyleSheet(
            f"QPushButton {{ background-color: {c['primary']}; color: #FFFFFF; font-weight: bold; border: none; padding: 8px 18px; }}"
            f"QPushButton:hover {{ background-color: {c['primary_hover']}; }}"
        )
        confirm_btn.clicked.connect(self.accept)
        btn_layout.addWidget(confirm_btn)

        layout.addLayout(btn_layout)


class UpdateReviewDialog(QDialog):
    """Sober, interactive note-by-note and field-by-field update review dialog."""

    def __init__(
        self,
        plan: UpdatePlan,
        deck_name: str,
        parent: Optional[QWidget] = None,
        only_unsuspended_initial: bool = False,
        auto_accept_suspended_initial: bool = True,
    ) -> None:
        super().__init__(parent)
        self.plan = plan
        self.deck_name = deck_name
        self.colors = get_colors()
        self.current_item: Optional[NoteUpdateItem] = None
        self.only_unsuspended = only_unsuspended_initial
        self.plan.only_unsuspended = only_unsuspended_initial
        self.plan.auto_accept_suspended = auto_accept_suspended_initial

        self.setWindowTitle(f"Review Updates - {self.deck_name}")
        self.resize(1120, 780)
        self.setMinimumSize(960, 640)

        # Keyboard shortcuts
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._on_cancel_clicked)
        QShortcut(QKeySequence("Ctrl+Return"), self, self._on_apply_clicked)
        QShortcut(QKeySequence("1"), self, self._shortcut_keep_local)
        QShortcut(QKeySequence("2"), self, self._shortcut_accept_remote)

        self._init_ui()
        self._refresh_list()

    def _init_ui(self) -> None:
        c = self.colors
        self.setStyleSheet(
            f"QDialog {{ background-color: {c['background']}; color: {c['text_primary']}; }}"
            f"QLabel {{ color: {c['text_primary']}; }}"
            f"QPushButton {{ background-color: {c['surface_elevated']}; color: {c['text_primary']}; "
            f"border: 1px solid {c['border']}; border-radius: 4px; padding: 6px 14px; font-size: 13px; }}"
            f"QPushButton:hover {{ background-color: {c['surface_hover']}; border-color: {c['border_focus']}; }}"
            f"QLineEdit {{ background-color: {c['surface']}; color: {c['text_primary']}; "
            f"border: 1px solid {c['border']}; border-radius: 4px; padding: 6px 10px; font-size: 12px; }}"
            f"QListWidget {{ background-color: {c['surface']}; color: {c['text_primary']}; "
            f"border: 1px solid {c['border']}; border-radius: 6px; }}"
            f"QListWidget::item {{ padding: 10px 10px; border-bottom: 1px solid {c['border']}; }}"
            f"QListWidget::item:selected {{ background-color: {c['surface_hover']}; border-left: 4px solid {c['primary']}; }}"
            f"QRadioButton, QCheckBox {{ color: {c['text_primary']}; spacing: 8px; font-size: 13px; }}"
        )

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(18, 16, 18, 16)
        main_layout.setSpacing(12)

        # Header bar
        header_layout = QHBoxLayout()
        header_text_layout = QVBoxLayout()
        header_text_layout.setSpacing(2)

        title_label = QLabel(f"Review Updates: {self.deck_name}")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        header_text_layout.addWidget(title_label)

        sub_label = QLabel("Compare local version against incoming AnkiCollab changes. Arbitrate note-by-note and field-by-field.")
        sub_label.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        header_text_layout.addWidget(sub_label)
        header_layout.addLayout(header_text_layout)

        header_layout.addStretch()

        # Filter segment: All modified vs Unsuspended only
        filter_widget = QWidget()
        filter_widget.setStyleSheet(
            f"background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 4px 8px;"
        )
        fw_layout = QHBoxLayout(filter_widget)
        fw_layout.setContentsMargins(4, 2, 4, 2)
        fw_layout.setSpacing(12)

        self.filter_all_rb = QRadioButton(f"All modified ({self.plan.total_changed})")
        self.filter_unsuspended_rb = QRadioButton(f"Unsuspended only ({self.plan.total_unsuspended})")
        self.filter_btn_group = QButtonGroup(self)
        self.filter_btn_group.addButton(self.filter_all_rb)
        self.filter_btn_group.addButton(self.filter_unsuspended_rb)

        if self.only_unsuspended:
            self.filter_unsuspended_rb.setChecked(True)
        else:
            self.filter_all_rb.setChecked(True)

        self.filter_all_rb.toggled.connect(self._on_filter_changed)
        self.filter_unsuspended_rb.toggled.connect(self._on_filter_changed)
        fw_layout.addWidget(self.filter_all_rb)
        fw_layout.addWidget(self.filter_unsuspended_rb)
        header_layout.addWidget(filter_widget)

        main_layout.addLayout(header_layout)

        # Policy Banner for Suspended Cards (active when Unsuspended only is selected)
        self.suspended_policy_box = QWidget(self)
        self.suspended_policy_box.setStyleSheet(
            f"background-color: {c['surface_elevated']}; border: 1px solid {c['border']}; "
            f"border-radius: 6px; padding: 8px 12px;"
        )
        sp_layout = QHBoxLayout(self.suspended_policy_box)
        sp_layout.setContentsMargins(10, 6, 10, 6)
        sp_layout.setSpacing(12)

        self.auto_accept_suspended_cb = QCheckBox("Auto-accept remote updates for suspended cards")
        self.auto_accept_suspended_cb.setChecked(self.plan.auto_accept_suspended)
        self.auto_accept_suspended_cb.setStyleSheet(f"font-weight: 600; color: {c['primary']};")
        self.auto_accept_suspended_cb.setToolTip(
            "When checked, notes that you have suspended will be updated automatically in the background.\n"
            "When unchecked, suspended cards will remain in their local state and will NOT be modified."
        )
        self.auto_accept_suspended_cb.toggled.connect(self._on_auto_accept_suspended_toggled)
        sp_layout.addWidget(self.auto_accept_suspended_cb)

        self.suspended_policy_hint = QLabel()
        self.suspended_policy_hint.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        self._update_suspended_hint()
        sp_layout.addWidget(self.suspended_policy_hint)
        sp_layout.addStretch()

        main_layout.addWidget(self.suspended_policy_box)
        self.suspended_policy_box.setVisible(self.filter_unsuspended_rb.isChecked())

        # Main Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Left list widget container
        left_widget = QWidget(self)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        left_hdr_layout = QHBoxLayout()
        self.left_label = QLabel("Notes to Review")
        self.left_label.setStyleSheet(f"color: {c['text_secondary']}; font-size: 12px; font-weight: bold;")
        left_hdr_layout.addWidget(self.left_label)
        left_hdr_layout.addStretch()
        left_layout.addLayout(left_hdr_layout)

        # Quick filter / search box
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter notes by title or text...")
        self.search_input.textChanged.connect(self._refresh_list)
        left_layout.addWidget(self.search_input)

        self.list_widget = QListWidget(self)
        self.list_widget.currentRowChanged.connect(self._on_item_selected)
        left_layout.addWidget(self.list_widget)

        left_widget.setMinimumWidth(320)
        splitter.addWidget(left_widget)

        # Right detail pane (ScrollArea)
        right_scroll = QScrollArea(self)
        right_scroll.setWidgetResizable(True)
        right_scroll.setMinimumWidth(560)
        self.right_container = QWidget()
        self.right_layout = QVBoxLayout(self.right_container)
        self.right_layout.setContentsMargins(14, 12, 14, 12)
        self.right_layout.setSpacing(14)
        right_scroll.setWidget(self.right_container)

        splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([340, 780])

        main_layout.addWidget(splitter)

        # Bottom action bar
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(10)

        accept_all_btn = QPushButton("Accept All Remote")
        accept_all_btn.clicked.connect(self._confirm_accept_all)
        bottom_layout.addWidget(accept_all_btn)

        keep_all_btn = QPushButton("Keep All Local")
        keep_all_btn.clicked.connect(self._confirm_keep_all)
        bottom_layout.addWidget(keep_all_btn)

        bottom_layout.addSpacing(16)

        self.summary_status_lbl = QLabel()
        self.summary_status_lbl.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        bottom_layout.addWidget(self.summary_status_lbl)

        bottom_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._on_cancel_clicked)
        bottom_layout.addWidget(cancel_btn)

        apply_btn = QPushButton("Apply Choices")
        apply_btn.setDefault(True)
        apply_btn.setStyleSheet(
            f"QPushButton {{ background-color: {c['primary']}; color: #FFFFFF; font-weight: bold; border: none; padding: 7px 18px; }}"
            f"QPushButton:hover {{ background-color: {c['primary_hover']}; }}"
        )
        apply_btn.clicked.connect(self._on_apply_clicked)
        bottom_layout.addWidget(apply_btn)

        main_layout.addLayout(bottom_layout)
        self._update_summary_status()

    def _update_suspended_hint(self) -> None:
        count = self.plan.total_suspended
        if self.auto_accept_suspended_cb.isChecked():
            self.suspended_policy_hint.setText(
                f"({count} suspended note(s) will be automatically updated from AnkiCollab)"
            )
        else:
            self.suspended_policy_hint.setText(
                f"({count} suspended note(s) will be kept local and skipped)"
            )

    def _on_auto_accept_suspended_toggled(self, checked: bool) -> None:
        self.plan.auto_accept_suspended = bool(checked)
        self._update_suspended_hint()
        self._update_summary_status()

    def _get_visible_items(self) -> List[NoteUpdateItem]:
        query = self.search_input.text().strip().lower() if hasattr(self, "search_input") else ""
        if self.filter_unsuspended_rb.isChecked():
            base = [it for it in self.plan.items if not it.is_suspended]
        else:
            base = self.plan.items

        if not query:
            return base

        filtered = []
        for it in base:
            if query in it.display_title.lower() or query in it.guid.lower():
                filtered.append(it)
                continue
            match_field = any(query in (f.old_val or "").lower() or query in (f.new_val or "").lower() for f in it.field_diffs)
            if match_field:
                filtered.append(it)
        return filtered

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        visible = self._get_visible_items()
        c = self.colors
        self.left_label.setText(f"Notes to Review ({len(visible)})")

        for idx, item in enumerate(visible):
            title = item.display_title
            status_tag = "Suspended" if item.is_suspended else "Active"
            choice_tag = (
                "Keep Local"
                if item.note_choice == "local"
                else ("Custom" if item.note_choice == "custom" else "Accept AC")
            )
            chg_count = f"{item.changed_fields_count} field(s) changed"

            item_text = f"{idx + 1}. {title}\n    [{status_tag}]  [{choice_tag}]  {chg_count}"
            list_item = QListWidgetItem(item_text)

            if item.note_choice == "local":
                list_item.setForeground(Qt.GlobalColor.gray)
            self.list_widget.addItem(list_item)

        if visible:
            self.list_widget.setCurrentRow(0)
        else:
            self._render_empty_detail()
        self._update_summary_status()

    def _on_filter_changed(self) -> None:
        is_unsuspended_only = self.filter_unsuspended_rb.isChecked()
        self.plan.only_unsuspended = is_unsuspended_only
        self.suspended_policy_box.setVisible(is_unsuspended_only)
        self._refresh_list()

    def _update_summary_status(self) -> None:
        total = len(self.plan.items)
        count_local = sum(1 for it in self.plan.items if it.note_choice == "local")
        count_remote = sum(1 for it in self.plan.items if it.note_choice == "remote")
        count_perm = sum(sum(1 for f in it.field_diffs if f.permanent_protect) for it in self.plan.items)
        self.summary_status_lbl.setText(
            f"Total: {total} notes | {count_remote} accepted | {count_local} kept local | {count_perm} permanent protection(s)"
        )

    def _on_item_selected(self, row: int) -> None:
        visible = self._get_visible_items()
        if 0 <= row < len(visible):
            self.current_item = visible[row]
            self._render_detail(self.current_item)
        else:
            self.current_item = None
            self._render_empty_detail()

    def _render_empty_detail(self) -> None:
        while self.right_layout.count():
            item = self.right_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        empty_lbl = QLabel("No modified notes match the current filter or search.")
        empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lbl.setStyleSheet(f"color: {self.colors['text_secondary']}; padding: 40px; font-size: 14px;")
        self.right_layout.addWidget(empty_lbl)

    def _render_detail(self, item: NoteUpdateItem) -> None:
        c = self.colors
        while self.right_layout.count():
            w = self.right_layout.takeAt(0)
            if w.widget():
                w.widget().deleteLater()

        # Top summary info box
        info_box = QWidget()
        info_box.setStyleSheet(
            f"background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 12px;"
        )
        info_layout = QVBoxLayout(info_box)
        info_layout.setContentsMargins(10, 10, 10, 10)
        info_layout.setSpacing(6)

        row_1 = QHBoxLayout()
        deck_lbl = QLabel(f"Target Deck: {item.deck_name or self.deck_name}")
        deck_lbl.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {c['text_primary']};")
        row_1.addWidget(deck_lbl)
        row_1.addStretch()

        susp_color = c["warning"] if item.is_suspended else c["success"]
        susp_text = "Suspended by user" if item.is_suspended else "Active (unsuspended)"
        status_lbl = QLabel(f"Status: {susp_text}")
        status_lbl.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {susp_color};")
        row_1.addWidget(status_lbl)
        info_layout.addLayout(row_1)

        type_lbl = QLabel(f"Notetype: {item.notetype_name}  |  GUID: {item.guid[:16]}...")
        type_lbl.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        info_layout.addWidget(type_lbl)

        self.right_layout.addWidget(info_box)

        # Quick whole-note actions bar
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        keep_note_btn = QPushButton("Keep Entire Note Local (1)")
        keep_note_btn.setToolTip("Preserve all local fields and tags for this note without applying incoming changes")
        keep_note_btn.clicked.connect(lambda: self._set_note_choice("local"))
        btn_row.addWidget(keep_note_btn)

        accept_note_btn = QPushButton("Accept AnkiCollab Note (2)")
        accept_note_btn.setToolTip("Accept all incoming AnkiCollab changes for this note")
        accept_note_btn.clicked.connect(lambda: self._set_note_choice("remote"))
        btn_row.addWidget(accept_note_btn)

        btn_row.addStretch()

        history_btn = QPushButton("View Note History")
        history_btn.setToolTip("View full commit audit log, authors, and rationales for this note")
        history_btn.clicked.connect(lambda: self._open_note_history(item.guid))
        btn_row.addWidget(history_btn)

        self.right_layout.addLayout(btn_row)

        # Fields Section Header
        fields_header = QLabel("Fields (Diff Import Selection)")
        fields_header.setStyleSheet("font-size: 14px; font-weight: bold; margin-top: 6px;")
        self.right_layout.addWidget(fields_header)

        # Field-by-field cards
        for field_diff in item.field_diffs:
            field_container = QWidget()
            field_container.setStyleSheet(
                f"border: 1px solid {c['border']}; border-radius: 6px; background-color: {c['surface']}; margin-bottom: 8px;"
            )
            fc_layout = QVBoxLayout(field_container)
            fc_layout.setContentsMargins(12, 10, 12, 10)
            fc_layout.setSpacing(8)

            # Field header: Name, badge, and segmented buttons
            f_hdr_layout = QHBoxLayout()
            fname_lbl = QLabel(f"Field: {field_diff.name}")
            fname_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
            f_hdr_layout.addWidget(fname_lbl)

            if field_diff.changed:
                chg_badge = QLabel("Modified")
                chg_badge.setStyleSheet(
                    f"background-color: {c['primary']}; color: #FFFFFF; font-size: 10px; "
                    f"font-weight: bold; padding: 2px 6px; border-radius: 3px;"
                )
                f_hdr_layout.addWidget(chg_badge)
            else:
                unchg_badge = QLabel("Unchanged")
                unchg_badge.setStyleSheet(
                    f"background-color: {c['surface_elevated']}; color: {c['text_muted']}; font-size: 10px; "
                    f"padding: 2px 6px; border-radius: 3px;"
                )
                f_hdr_layout.addWidget(unchg_badge)

            f_hdr_layout.addStretch()

            rb_group = QButtonGroup(field_container)
            rb_local = QRadioButton("Keep Local")
            rb_remote = QRadioButton("Accept AC")
            rb_group.addButton(rb_local)
            rb_group.addButton(rb_remote)

            if field_diff.choice == "local":
                rb_local.setChecked(True)
            else:
                rb_remote.setChecked(True)

            def make_handler(diff_obj, is_local):
                def handler(checked):
                    if checked:
                        diff_obj.choice = "local" if is_local else "remote"
                        self._update_note_choice_summary(item)
                        self._update_summary_status()
                return handler

            rb_local.toggled.connect(make_handler(field_diff, True))
            rb_remote.toggled.connect(make_handler(field_diff, False))

            f_hdr_layout.addWidget(rb_local)
            f_hdr_layout.addWidget(rb_remote)
            fc_layout.addLayout(f_hdr_layout)

            # Checkbox: permanent protect
            perm_cb = QCheckBox(f"Always protect field ({PREFIX_PROTECTED_FIELDS}::{field_diff.name})")
            perm_cb.setChecked(field_diff.permanent_protect)
            perm_cb.setStyleSheet(f"color: {c['text_secondary']}; font-size: 12px;")
            perm_cb.setToolTip(
                f"Permanently tag this note with {PREFIX_PROTECTED_FIELDS}::{field_diff.name} "
                "so future updates never overwrite this field."
            )

            def make_perm_handler(diff_obj):
                def handler(checked):
                    diff_obj.permanent_protect = bool(checked)
                    if checked:
                        diff_obj.choice = "local"
                        rb_local.setChecked(True)
                    self._update_note_choice_summary(item)
                    self._update_summary_status()
                return handler

            perm_cb.toggled.connect(make_perm_handler(field_diff))
            fc_layout.addWidget(perm_cb)

            # Diff viewer: full width, clean typography, expanding naturally without inner scrollbar
            diff_lbl = QLabel()
            diff_lbl.setTextFormat(Qt.TextFormat.RichText)
            diff_lbl.setWordWrap(True)
            diff_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            diff_lbl.setText(field_diff.diff_html(c))
            fc_layout.addWidget(diff_lbl)

            self.right_layout.addWidget(field_container)

        # Tags diff section if present
        if item.tags_added or item.tags_removed:
            tags_box = QWidget()
            tags_box.setStyleSheet(
                f"border: 1px solid {c['border']}; border-radius: 6px; background-color: {c['surface']}; margin-bottom: 8px;"
            )
            tb_layout = QVBoxLayout(tags_box)
            tb_layout.setContentsMargins(12, 10, 12, 10)
            tb_layout.setSpacing(6)

            tb_hdr = QHBoxLayout()
            tb_lbl = QLabel("Tags Changes")
            tb_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
            tb_hdr.addWidget(tb_lbl)
            tb_hdr.addStretch()

            rb_tags_group = QButtonGroup(tags_box)
            rb_tags_local = QRadioButton("Keep Local Tags")
            rb_tags_remote = QRadioButton("Accept AC Tags")
            rb_tags_group.addButton(rb_tags_local)
            rb_tags_group.addButton(rb_tags_remote)

            if item.tags_choice == "local":
                rb_tags_local.setChecked(True)
            else:
                rb_tags_remote.setChecked(True)

            def make_tags_handler(is_local):
                def handler(checked):
                    if checked:
                        item.tags_choice = "local" if is_local else "remote"
                return handler

            rb_tags_local.toggled.connect(make_tags_handler(True))
            rb_tags_remote.toggled.connect(make_tags_handler(False))

            tb_hdr.addWidget(rb_tags_local)
            tb_hdr.addWidget(rb_tags_remote)
            tb_layout.addLayout(tb_hdr)

            tags_content = ""
            if item.tags_added:
                added_str = " ".join(item.tags_added)
                tags_content += (
                    f'<div style="color: {c["success"]}; font-size: 12px; margin-bottom: 4px;">'
                    f'<strong>Added:</strong> {html.escape(added_str)}</div>'
                )
            if item.tags_removed:
                removed_str = " ".join(item.tags_removed)
                tags_content += (
                    f'<div style="color: {c["danger"]}; font-size: 12px;">'
                    f'<strong>Removed:</strong> <strike>{html.escape(removed_str)}</strike></div>'
                )

            tags_view = QLabel()
            tags_view.setTextFormat(Qt.TextFormat.RichText)
            tags_view.setText(tags_content)
            tb_layout.addWidget(tags_view)

            self.right_layout.addWidget(tags_box)

        self.right_layout.addStretch()

    def _set_note_choice(self, choice: str) -> None:
        if not self.current_item:
            return
        self.current_item.note_choice = choice
        for f in self.current_item.field_diffs:
            f.choice = choice
        self.current_item.tags_choice = choice

        # Re-render to reflect choices
        self._render_detail(self.current_item)

        # Update row text in list
        curr_row = self.list_widget.currentRow()
        if curr_row >= 0:
            title = self.current_item.display_title
            status_tag = "Suspended" if self.current_item.is_suspended else "Active"
            choice_tag = "Keep Local" if choice == "local" else ("Custom" if choice == "custom" else "Accept AC")
            chg_count = f"{self.current_item.changed_fields_count} field(s) changed"
            item_widget = self.list_widget.item(curr_row)
            if item_widget:
                item_widget.setText(f"{curr_row + 1}. {title}\n    [{status_tag}]  [{choice_tag}]  {chg_count}")
                if choice == "local":
                    item_widget.setForeground(Qt.GlobalColor.gray)
                else:
                    item_widget.setForeground(Qt.GlobalColor.white if theme_manager.night_mode else Qt.GlobalColor.black)
        self._update_summary_status()

    def _update_note_choice_summary(self, item: NoteUpdateItem) -> None:
        all_local = all(f.choice == "local" for f in item.field_diffs) and (item.tags_choice == "local" if item.tags_changed else True)
        all_remote = all(f.choice == "remote" for f in item.field_diffs) and (item.tags_choice == "remote" if item.tags_changed else True)
        if all_local:
            item.note_choice = "local"
        elif all_remote:
            item.note_choice = "remote"
        else:
            item.note_choice = "custom"

        # Update row text in list
        visible = self._get_visible_items()
        if item in visible:
            idx = visible.index(item)
            title = item.display_title
            status_tag = "Suspended" if item.is_suspended else "Active"
            choice_tag = "Keep Local" if item.note_choice == "local" else ("Custom" if item.note_choice == "custom" else "Accept AC")
            chg_count = f"{item.changed_fields_count} field(s) changed"
            item_widget = self.list_widget.item(idx)
            if item_widget:
                item_widget.setText(f"{idx + 1}. {title}\n    [{status_tag}]  [{choice_tag}]  {chg_count}")
        self._update_summary_status()

    def _shortcut_keep_local(self) -> None:
        self._set_note_choice("local")
        self._select_next_item()

    def _shortcut_accept_remote(self) -> None:
        self._set_note_choice("remote")
        self._select_next_item()

    def _select_next_item(self) -> None:
        curr = self.list_widget.currentRow()
        if curr + 1 < self.list_widget.count():
            self.list_widget.setCurrentRow(curr + 1)

    # ------------------------------------------------------------------------
    # Batch actions with explicit confirmation validation
    # ------------------------------------------------------------------------

    def _confirm_accept_all(self) -> None:
        visible = self._get_visible_items()
        reply = QMessageBox.question(
            self,
            "Accept All Remote Updates",
            f"Are you sure you want to accept all incoming AnkiCollab changes for the {len(visible)} note(s) currently displayed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for item in visible:
                item.note_choice = "remote"
                for f in item.field_diffs:
                    f.choice = "remote"
                item.tags_choice = "remote"
            self._refresh_list()

    def _confirm_keep_all(self) -> None:
        visible = self._get_visible_items()
        reply = QMessageBox.question(
            self,
            "Keep All Local Notes",
            f"Are you sure you want to keep your local versions for the {len(visible)} note(s) currently displayed?\n"
            "No remote changes will be applied to them.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for item in visible:
                item.note_choice = "local"
                for f in item.field_diffs:
                    f.choice = "local"
                item.tags_choice = "local"
            self._refresh_list()

    # ------------------------------------------------------------------------
    # Exit / Cancel confirmation validation
    # ------------------------------------------------------------------------

    def _has_unapplied_customizations(self) -> bool:
        """Check if user has made choices differing from pure remote default."""
        for item in self.plan.items:
            if item.note_choice != "remote":
                return True
            if item.tags_changed and item.tags_choice != "remote":
                return True
            for f in item.field_diffs:
                if f.choice != "remote" or f.permanent_protect:
                    return True
        return False

    def _on_cancel_clicked(self) -> None:
        if self._has_unapplied_customizations():
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Discard Unapplied Choices?")
            msg_box.setText(
                "You have unapplied update review choices. If you cancel now, "
                "no changes will be applied to your deck."
            )
            discard_btn = msg_box.addButton("Discard & Cancel", QMessageBox.ButtonRole.DestructiveRole)
            apply_btn = msg_box.addButton("Apply Choices", QMessageBox.ButtonRole.AcceptRole)
            continue_btn = msg_box.addButton("Continue Reviewing", QMessageBox.ButtonRole.RejectRole)
            msg_box.setDefaultButton(continue_btn)

            msg_box.exec()
            clicked = msg_box.clickedButton()
            if clicked == discard_btn:
                self.reject()
            elif clicked == apply_btn:
                self._on_apply_clicked()
            # If continue_btn, do nothing and stay in review
        else:
            self.reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._has_unapplied_customizations():
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Discard Unapplied Choices?")
            msg_box.setText(
                "You have unapplied update review choices. If you close now, "
                "no changes will be applied to your deck."
            )
            discard_btn = msg_box.addButton("Discard & Close", QMessageBox.ButtonRole.DestructiveRole)
            apply_btn = msg_box.addButton("Apply Choices", QMessageBox.ButtonRole.AcceptRole)
            continue_btn = msg_box.addButton("Continue Reviewing", QMessageBox.ButtonRole.RejectRole)
            msg_box.setDefaultButton(continue_btn)

            msg_box.exec()
            clicked = msg_box.clickedButton()
            if clicked == discard_btn:
                event.accept()
            elif clicked == apply_btn:
                event.ignore()
                self._on_apply_clicked()
            else:
                event.ignore()
        else:
            event.accept()

    # ------------------------------------------------------------------------
    # Apply Choices with final confirmation summary modal
    # ------------------------------------------------------------------------

    def _on_apply_clicked(self) -> None:
        confirm_dialog = ReviewConfirmationDialog(self.plan, self.deck_name, parent=self)
        if confirm_dialog.exec() == QDialog.DialogCode.Accepted:
            self.accept()

    def _open_note_history(self, guid: str) -> None:
        from .note_history import show_note_history_dialog
        show_note_history_dialog(deck_hash=self.plan.deck_hash, guid=guid, parent=self)
